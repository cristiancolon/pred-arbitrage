"""Interactive review of matcher candidates: read both rulebooks, then approve or reject.

Approved pairs are appended to pairs.csv (which the scanner watches); every decision
is remembered so a candidate is only shown once.
"""

import shutil
import sqlite3
import sys
import textwrap
import time
from datetime import datetime, timezone

from .config import Config
from .pairs import append_pair, load_pairs

INVERSE_WARNING = (
    "INVERSE pair: Polymarket YES is treated as Kalshi NO. That only hedges if the "
    "event cannot end any other way: no draw/tie, and both venues handle "
    "postponement or cancellation the same way. Check both rulebooks."
)


def _fmt_ts(ts: float | None) -> str:
    if ts is None:
        return "?"
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _fmt_px(v: float | None) -> str:
    return "--" if v is None else f"{v:.3f}".rstrip("0").rstrip(".")


class _Style:
    def __init__(self, enabled: bool):
        self.on = enabled

    def __call__(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.on else text


def _market_block(style: _Style, venue: str, m: sqlite3.Row, width: int) -> list[str]:
    ind = "    "
    if venue == "K":
        head = f"KALSHI   {m['id']}"
        sides = f"YES = {m['yes_label'] or '(see title)'}"
    else:
        head = f"POLY US  {m['id']}"
        sides = f"YES (buy) = {m['yes_label']}    NO (sell) = {m['no_label']}"
    meta = (f"bid/ask {_fmt_px(m['yes_bid'])}/{_fmt_px(m['yes_ask'])}   taker fee coef {m['fee_coef']:.4f}   "
            f"starts {_fmt_ts(m['start_ts'])}   resolves ~{_fmt_ts(m['close_ts'])}")
    lines = [style("1", head), ind + m["title"], ind + style("1", sides), ind + meta, ind + "rules:"]
    for para in (m["rules"] or "(none)").split("\n"):
        if para.strip():
            lines += textwrap.wrap(para.strip(), width=width - 8, initial_indent=ind + "  ",
                                   subsequent_indent=ind + "  ")
    return lines


def record(db: sqlite3.Connection, kalshi: str, pm: str, decision: str, source: str) -> None:
    db.execute("INSERT OR REPLACE INTO decisions (kalshi, pm, decision, ts, source) VALUES (?,?,?,?,?)",
               (kalshi, pm, decision, time.time(), source))


def decide(cfg: Config, db: sqlite3.Connection, kalshi: str, pm: str, decision: str,
           source: str = "human", note: str | None = None, commit: bool = True) -> None:
    """Record a review decision; approvals are appended to pairs.csv."""
    if decision not in ("same", "inverse", "reject"):
        raise ValueError(decision)
    record(db, kalshi, pm, decision, source)
    if commit:
        db.commit()
    if decision != "reject":
        if note is None:
            k = db.execute("SELECT title FROM markets WHERE venue = 'K' AND id = ?", (kalshi,)).fetchone()
            note = (k["title"] if k else "")[:80]
        append_pair(cfg.pairs_path, kalshi, pm, decision, note)


def _pending(db: sqlite3.Connection, min_score: float, pairs_path: str) -> list[sqlite3.Row]:
    existing = {(p.kalshi, p.pm) for p in load_pairs(pairs_path)}
    rows = db.execute(
        "SELECT c.* FROM candidates c LEFT JOIN decisions d ON d.kalshi = c.kalshi AND d.pm = c.pm "
        "WHERE d.kalshi IS NULL AND c.score >= ? ORDER BY c.confident DESC, c.score DESC",
        (min_score,),
    ).fetchall()
    return [r for r in rows if (r["kalshi"], r["pm"]) not in existing]


def list_candidates(db: sqlite3.Connection, min_score: float, limit: int, pairs_path: str) -> None:
    rows = _pending(db, min_score, pairs_path)[:limit]
    print(f"{'score':>5}  {'relation':8} {'kalshi_ticker':44} pm_slug")
    for r in rows:
        flag = "" if r["confident"] else " (?)"
        print(f"{r['score']:5.2f}  {r['relation'] + flag:8} {r['kalshi']:44} {r['pm']}")


def run(cfg: Config, db: sqlite3.Connection, min_score: float) -> None:
    pending = _pending(db, min_score, cfg.pairs_path)
    if not pending:
        print("Nothing to review. Run `arbscan match` (or lower --min-score).")
        return
    style = _Style(sys.stdout.isatty())
    width = min(shutil.get_terminal_size((110, 40)).columns, 140)
    approved = rejected = 0
    print(f"{len(pending)} candidates to review, best first. Read both rulebooks before approving.\n")
    for i, c in enumerate(pending, start=1):
        k = db.execute("SELECT * FROM markets WHERE venue = 'K' AND id = ?", (c["kalshi"],)).fetchone()
        p = db.execute("SELECT * FROM markets WHERE venue = 'P' AND id = ?", (c["pm"],)).fetchone()
        if k is None or p is None:
            continue  # catalog refreshed since matching; market gone
        relation = c["relation"]
        meaning = "Polymarket YES = Kalshi YES" if relation == "same" else "Polymarket YES = Kalshi NO"
        confidence = "" if c["confident"] else "  (relation guessed: no outcome labels to compare)"
        print(style("2", "─" * width))
        print(f"[{i}/{len(pending)}]  score {c['score']:.2f}   proposed: "
              f"{style('1;36', relation.upper())} ({meaning}){confidence}")
        print()
        print("\n".join(_market_block(style, "K", k, width)))
        print()
        print("\n".join(_market_block(style, "P", p, width)))
        if relation == "inverse":
            print()
            print(style("1;33", textwrap.fill(INVERSE_WARNING, width=width)))
        print()
        while True:
            try:
                ans = input("[s]ame  [i]nverse  [r]eject  [Enter] skip  [q]uit > ").strip().lower()
            except EOFError:
                ans = "q"
            if ans in ("s", "i", "r", "", "q"):
                break
        if ans == "q":
            break
        if ans == "":
            continue
        decision = {"s": "same", "i": "inverse", "r": "reject"}[ans]
        decide(cfg, db, c["kalshi"], c["pm"], decision)
        if decision == "reject":
            rejected += 1
        else:
            approved += 1
            print(style("32", f"added to {cfg.pairs_path}"))
    print(f"\n{approved} approved, {rejected} rejected. The scanner picks up pairs.csv changes automatically.")
