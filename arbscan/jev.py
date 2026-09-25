"""Automatic pair review with TypeSafe's Jev model (https://docs.typesafe.ai).

For each matcher candidate that no rule or person has decided yet, Jev reads both
markets (titles, outcome labels, rules) and answers three questions in one call.
Code turns the answers into a verdict:

- approve: Jev picks the Polymarket side the matcher proposed, the two markets count
  the same competition and scope, and Polymarket's rules agree with its own title.
- reject: Jev is confident that neither Polymarket side is the same bet.
- unsure: anything else. Treated as a rejection: a pair is only watched when Jev is
  sure it is the same bet. Jev's reason is kept in jev_reviews.

The questions and thresholds below are the part to review and tune. They were tuned
against jev-1.13.0 on ~200 hand-labelled pairs (every equivalent pair approved, every
wrong game, flipped side and different competition kept out), so the model is pinned
in config: an alias moving to a new version could shift the probabilities.

Cost: ~900 input tokens per pair at $0.042 per million, so a full queue of ~5,000
pairs is about $0.20, and later refreshes only send pairs Jev hasn't seen.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass

import httpx

from .config import Config
from .http import Api, ApiError
from .pairs import load_pairs
from .review import decide

log = logging.getLogger(__name__)

BASE = "https://api.typesafe.ai"
PRICE_PER_TOKEN = 0.042 / 1e6
CONCURRENCY = 4

# --- Questions -----------------------------------------------------------------
# Each is one literal judgment (Jev reads questions at face value). `kalshi_market`
# and `polymarket_market` name the two halves of the state built in `request_body`.

SIDE = (
    "Buying YES on `kalshi_market` pays out when this happens: `kalshi_market.yes_means`. "
    "Which Polymarket bet pays out in exactly the same real-world situations?"
)
NEITHER = (
    "Neither Polymarket bet: the markets are about a different event, team, player, statistic, "
    "period of play, line, threshold, or time window."
)
SAME_SCOPE = (
    "Do `kalshi_market` and `polymarket_market` count results from the same competition and scope, "
    "such as the same game, tournament, league or conference, award, or office?"
)
# Catches Polymarket US spread markets whose rules name the other team as the winner
# (about half of the college and NFL "+X" spreads at the time of writing).
CONTRADICTION = (
    "Do `polymarket_market.rules` describe a different winning outcome for YES than "
    "`polymarket_market.title` and `polymarket_market.yes_side` do?"
)

# --- Thresholds ----------------------------------------------------------------
APPROVE_MIN_SIDE = 0.6  # probability of the proposed side (equivalent pairs scored >= 0.67)
APPROVE_MIN_SCOPE = 0.6  # same-scope probability (equivalent pairs >= 0.7, conference vs national 0.4)
APPROVE_MAX_CONTRADICTION = 0.5  # equivalent pairs <= 0.46, contradictory spreads >= 0.8
REJECT_MIN_NEITHER = 0.7  # probability that neither side is the same bet

_BOILERPLATE = re.compile(
    r"(Kalshi is not affiliated|This market and these products have not been endorsed|Any references to)[^\n]*",
    re.I,
)


def api_key(cfg: Config) -> str:
    return cfg.jev_api_key or os.environ.get("TYPESAFE_API_KEY", "")


def clean_rules(text: str | None) -> str:
    """Drop trademark disclaimers: they carry no settlement information."""
    t = _BOILERPLATE.sub("", text or "")
    return re.sub(r"\n\s*\n+", "\n", t).strip()


def request_body(k: sqlite3.Row | dict, p: sqlite3.Row | dict, model: str) -> dict:
    state = {
        "kalshi_market": {"title": k["title"], "yes_means": k["yes_label"] or k["title"],
                          "rules": clean_rules(k["rules"])},
        "polymarket_market": {"title": p["title"], "yes_side": p["yes_label"], "no_side": p["no_label"],
                              "rules": clean_rules(p["rules"])},
    }
    questions = {
        "side": {"type": "choice", "instructions": SIDE, "criteria": {
            "yes_side": f"Buying YES on Polymarket, the side labelled \"{p['yes_label']}\".",
            "no_side": f"Buying NO on Polymarket, the side labelled \"{p['no_label']}\".",
            "neither": NEITHER,
        }},
        "same_scope": {"type": "noul", "instructions": SAME_SCOPE},
        "contradiction": {"type": "noul", "instructions": CONTRADICTION},
    }
    return {"state": state, "model": model, "questions": questions}


def input_hash(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]


@dataclass
class Verdict:
    verdict: str  # approve | reject | unsure
    reason: str
    side: dict[str, float]
    scope: float
    contradiction: float

    def answers(self) -> dict:
        return {"side": self.side, "scope": self.scope, "contradiction": self.contradiction}


def judge(answers: dict, relation: str) -> Verdict:
    side = answers["side"]["probabilities"]
    scope = answers["same_scope"]["noul"]
    contra = answers["contradiction"]["noul"]
    want = "yes_side" if relation == "same" else "no_side"
    other = "no_side" if want == "yes_side" else "yes_side"

    def v(verdict: str, reason: str) -> Verdict:
        return Verdict(verdict, reason, side, scope, contra)

    if side["neither"] >= REJECT_MIN_NEITHER:
        return v("reject", "Not the same bet")
    if contra >= APPROVE_MAX_CONTRADICTION:
        return v("unsure", "Polymarket's rules contradict its own title or YES label")
    if scope < APPROVE_MIN_SCOPE:
        return v("unsure", "May count a different competition or scope")
    if side[other] > side[want]:
        return v("unsure", f"Jev reads this as {'inverse' if relation == 'same' else 'same'}, "
                           f"not {relation} as proposed")
    if side[want] < APPROVE_MIN_SIDE:
        return v("unsure", "Not confident these are the same bet")
    return v("approve", "Same bet")


def _pending(cfg: Config, db: sqlite3.Connection) -> list[sqlite3.Row]:
    paired = {(p.kalshi, p.pm) for p in load_pairs(cfg.pairs_path)}
    rows = db.execute(
        "SELECT c.kalshi, c.pm, c.relation, c.score, "
        "k.title AS k_title, k.yes_label AS k_yes, k.rules AS k_rules, "
        "p.title AS p_title, p.yes_label AS p_yes, p.no_label AS p_no, p.rules AS p_rules, "
        "j.relation AS j_relation, j.input_hash AS j_hash, j.verdict AS j_verdict "
        "FROM candidates c "
        "JOIN markets k ON k.venue = 'K' AND k.id = c.kalshi "
        "JOIN markets p ON p.venue = 'P' AND p.id = c.pm "
        "LEFT JOIN decisions d ON d.kalshi = c.kalshi AND d.pm = c.pm "
        "LEFT JOIN jev_reviews j ON j.kalshi = c.kalshi AND j.pm = c.pm "
        "WHERE d.kalshi IS NULL ORDER BY c.score DESC").fetchall()
    return [r for r in rows if (r["kalshi"], r["pm"]) not in paired]


def _markets(r: sqlite3.Row) -> tuple[dict, dict]:
    k = {"title": r["k_title"], "yes_label": r["k_yes"], "rules": r["k_rules"]}
    p = {"title": r["p_title"], "yes_label": r["p_yes"], "no_label": r["p_no"], "rules": r["p_rules"]}
    return k, p


def _decide(cfg: Config, db: sqlite3.Connection, r: sqlite3.Row, verdict: str, note: str | None = None) -> None:
    """Only an approval pairs the markets; a rejection or an unsure verdict both reject."""
    decision = r["relation"] if verdict == "approve" else "reject"
    decide(cfg, db, r["kalshi"], r["pm"], decision, source="jev", note=note, commit=False)


def _apply(cfg: Config, db: sqlite3.Connection, r: sqlite3.Row, h: str, resp: dict, dry_run: bool) -> Verdict:
    v = judge(resp["answers"], r["relation"])
    if dry_run:
        return v
    db.execute(
        "INSERT OR REPLACE INTO jev_reviews (kalshi, pm, relation, input_hash, model, verdict, reason, answers, "
        "tokens, ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (r["kalshi"], r["pm"], r["relation"], h, resp.get("model"), v.verdict, v.reason, json.dumps(v.answers()),
         (resp.get("usage") or {}).get("input_tokens"), time.time()))
    p_side = v.side["yes_side" if r["relation"] == "same" else "no_side"]
    _decide(cfg, db, r, v.verdict, note=f"jev:{p_side:.2f}")
    return v


async def review(cfg: Config, db: sqlite3.Connection, limit: int | None = None, dry_run: bool = False,
                 api: Api | None = None, only: set[tuple[str, str]] | None = None) -> dict:
    """Send every undecided candidate Jev hasn't already read (or whose text changed);
    with ``only``, just those (kalshi, pm) pairs. A candidate Jev already read, and
    whose text hasn't changed, gets its stored verdict without another call."""
    key = api_key(cfg)
    if not key and api is None:
        raise SystemExit("no Jev API key: set jev_api_key in config.toml or TYPESAFE_API_KEY")
    todo, known = [], []
    for r in _pending(cfg, db):
        if only is not None and (r["kalshi"], r["pm"]) not in only:
            continue
        k, p = _markets(r)
        body = request_body(k, p, cfg.jev_model)
        h = input_hash(body)
        if r["j_hash"] == h and r["j_relation"] == r["relation"]:
            known.append(r)  # already read this exact pair
        else:
            todo.append((r, body, h))
    todo = todo[: limit if limit is not None else cfg.jev_max_per_run]
    stats = {"sent": 0, "approve": 0, "reject": 0, "unsure": 0, "failed": 0, "tokens": 0, "known": 0}
    if known and not dry_run:
        for r in known:
            _decide(cfg, db, r, r["j_verdict"])
        db.commit()
        stats["known"] = len(known)
        log.info("applied Jev's earlier verdicts to %d candidates (unsure counts as a rejection)", len(known))
    if not todo:
        log.info("nothing new for Jev to review")
        return stats
    log.info("Jev (%s) reviewing %d candidates%s", cfg.jev_model, len(todo), " (dry run)" if dry_run else "")

    client = None
    if api is None:
        client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0),
                                   headers={"Authorization": f"Bearer {key}", "User-Agent": "arbscan/0.1"})
        api = Api(client, BASE, cfg.jev_rps, "jev")
    sem = asyncio.Semaphore(CONCURRENCY)
    t0 = time.monotonic()
    fatal: list[str] = []

    async def one(item):
        r, body, h = item
        async with sem:
            if fatal:
                return
            try:
                resp = await api.post("/v1/systemone", body)
            except ApiError as e:
                if e.status in (401, 403):
                    fatal.append(f"TypeSafe rejected the API key (HTTP {e.status}); check jev_api_key")
                else:
                    log.warning("%s | %s: %s", r["kalshi"], r["pm"], e)
                    stats["failed"] += 1
                return
        try:
            v = _apply(cfg, db, r, h, resp, dry_run)
        except (KeyError, TypeError) as e:  # an answer missing from the response
            log.warning("%s | %s: unexpected response (%r)", r["kalshi"], r["pm"], e)
            stats["failed"] += 1
            return
        stats["sent"] += 1
        stats[v.verdict] += 1
        stats["tokens"] += (resp.get("usage") or {}).get("input_tokens") or 0
        if dry_run:
            log.info("%-7s %s | %s (%s): %s", v.verdict, r["kalshi"], r["pm"], r["relation"], v.reason)
        if stats["sent"] % 50 == 0:
            db.commit()  # progress shows up on the dashboard as it goes
        if stats["sent"] % 250 == 0:
            log.info("%d/%d reviewed: %d approved, %d rejected, %d rejected as unsure", stats["sent"], len(todo),
                     stats["approve"], stats["reject"], stats["unsure"])

    try:
        await asyncio.gather(*(one(item) for item in todo))
    finally:
        db.commit()
        if client is not None:
            await client.aclose()
    if fatal:
        raise SystemExit(fatal[0])
    log.info("Jev reviewed %d in %.0fs: %d approved, %d rejected, %d rejected as unsure%s; %d tokens (~$%.3f)",
             stats["sent"], time.monotonic() - t0, stats["approve"], stats["reject"], stats["unsure"],
             f", {stats['failed']} failed" if stats["failed"] else "", stats["tokens"],
             stats["tokens"] * PRICE_PER_TOKEN)
    return stats
