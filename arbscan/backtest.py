"""Replaying the recorded trade decisions under different ways of sending the orders.

``arbscan backtest`` takes every decision the live, dry-run and paper traders recorded
(what they planned to buy, at what limits, and the books they saw) and replays it
against what the two books went on to show, as the scanner recorded them (``quotes``:
every change of the best prices; ``opportunities``: the ladders, about once a second),
under each policy below. It answers one question: how often do both legs fill?

**The model.** An immediate-or-cancel order that reaches a venue at time ``A`` meets
the book our feed showed at ``A + lag`` (the feed runs ``LAG`` behind the exchange). It
fills what that book offers at its limit or better. Order timings are drawn from the
live orders' own (decision to exchange, and round trip, per venue). Three things come
from the live trades' real orders rather than from the feed:

- an order that really came up short caps what an order at its limit or less can get
  at that moment or later;
- what our own order really took is put back, so a leg sent later than it really was
  still finds it (nobody else's order is known to have wanted it);
- a **frozen book**: the Polymarket US order found nothing while our feed went on
  showing the offer. Then the feed was wrong, and no order there fills at any time.

Given the live trades' real timings and order, the model reproduces their real
outcomes (``agreement``); that is its check.

**Outcomes.** *filled*: both legs filled the same number of contracts and nothing was
sold back (a trade smaller than planned counts). *missed*: the first leg found nothing;
no money moved. *sold back*: the legs filled unequally and the extra was sold back at a
loss. *skipped*: no order was sent (the pick didn't survive a wait, or the book check
found the book wrong); skipped decisions aren't trades and count in neither rate.
"Filled %" is filled / (filled + missed + sold back); "orders %" is contracts bought /
contracts ordered over every buy order, as the dashboard's Filled figure counts it.

**What it can't see.** Books frozen at a paper or dry-run decision (no real order
tested them), and size changes at an unchanged best price on Polymarket US between the
once-a-second ladders. Both make the policies without a book check look better than
they were; the live trades show by how much. And for a live trade, what a leg we
really bought would have done had we waited longer: it is taken to have stayed, which
flatters a policy that waits.
"""

import bisect
import json
import math
import random
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime

LAG = {"K": 0.04, "P": 0.12}  # seconds each feed runs behind its exchange (the feeds' measured medians)
DEFAULT_TIMING = {"K": (0.05, 0.09), "P": (0.125, 0.185)}  # (decision -> exchange, round trip) without live orders
SAME_S = 5.0  # two traders' decisions on one window this close together are one decision
MIN_PROFIT = 0.05  # live_min_profit_usd: a pick sized down to less than this isn't taken
EPS = 1e-9
RANK = {"live": 0, "dry": 1, "paper": 2}


@dataclass(frozen=True)
class Policy:
    """How a trade's orders go out.

    ``lead``: the leg sent first ("K", "P", "auto": the one whose price moved last);
    the other follows for what it filled. ``check``: the Polymarket US book is checked
    against the exchange first, at no cost in time; ``check_sees_frozen``: and that
    check does catch a frozen book (otherwise the order there finds nothing).
    ``settle_s``: the older of the two prices must have held still this long, and
    ``quiet_s``: the newer one this long (the recorded decisions waited 2 s).
    ``breakeven``: the second leg offers up to break-even in one order, instead of its
    planned limit and then a second order at break-even (which, live, found nothing 6
    times out of 6)."""
    name: str
    lead: str = "auto"
    check: bool = False
    check_sees_frozen: bool = True
    settle_s: float = 0.0
    quiet_s: float = 0.0
    breakeven: bool = False


BEFORE = Policy("as it was: last mover first")
NOW = Policy("as it is: checked, Polymarket first", lead="P", check=True, settle_s=3.0, breakeven=True)
POLICIES = (
    BEFORE,
    Policy("  + Polymarket book checked", check=True),
    Policy("  + Polymarket first", lead="P", check=True),
    Policy("  + both prices settled 3 s", lead="P", check=True, settle_s=3.0),
    NOW,
    Policy("as it is, if the check misses a frozen book", lead="P", check=True, check_sees_frozen=False,
           settle_s=3.0, breakeven=True),
    Policy("the same with Kalshi first", lead="K", check=True, settle_s=3.0, breakeven=True),
)


class Decision:
    """One recorded decision and what each leg's book showed around it, on our feed's clock."""

    def __init__(self, row: dict, quotes: list, opps: list, orders: list):
        self.row, self.t0, self.src = row, row["ts"], row["src"]
        books = row["books"]
        self.limit = {"K": row["k_limit"], "P": row["p_limit"]}
        self.n = row["planned_size"]
        self.steady = books["steady"]
        self.seen = {v: [tuple(lv) for lv in books["seen"][v]] for v in "KP"}
        self.k: list[tuple] = []  # (ts, best ask of the Kalshi leg, its size)
        self.p: list[tuple] = []  # (ts, best ask of the Polymarket US leg)
        for ts, kya, kys, kna, kns, pyb, pya in quotes:
            k = (kya, kys) if row["k_side"] == "yes" else (kna, kns)
            pa = pya if row["p_side"] == "yes" else (None if pyb is None else round(1 - pyb, 4))
            if not self.k or self.k[-1][1:] != k:
                self.k.append((ts, *k))
            if not self.p or self.p[-1][1] != pa:
                self.p.append((ts, pa))
        self.opps = [(ts, json.loads(kb), json.loads(pb)) for ts, d, kb, pb in opps if d == row["direction"]]
        self.orders = orders
        self.own: dict[str, float] = {}  # venue -> when our own real order first filled there
        # venue -> [(exchange time, limit, filled)]: real orders that came up short. One
        # at the same limit or less, at that moment or later, gets no more than they did.
        self.short: dict[str, list[tuple]] = {"K": [], "P": []}
        for o in orders:
            if o["action"] != "buy" or not o["exch_ts"]:
                continue
            v = o["venue"]
            if o["filled"] > EPS and v not in self.own:
                self.own[v] = o["exch_ts"]  # the feed can show our fill no sooner than this
            if o["filled"] < o["qty"] - EPS:
                self.short[v].append((o["exch_ts"], o["limit_price"], o["filled"]))
        # Frozen: the Polymarket US order found nothing, and half a second later our feed still showed the offer.
        miss = next((m for m in self.short["P"] if abs(m[1] - self.limit["P"]) < 1e-6), None)
        self.frozen = bool(miss and miss[2] <= EPS and "P" not in self.own
                           and self._ask("P", miss[0] + LAG["P"] + 0.5) is not None
                           and self._ask("P", miss[0] + LAG["P"] + 0.5) <= self.limit["P"] + EPS)

    def _at(self, rows: list, t: float):
        i = bisect.bisect_right(rows, (t, math.inf)) - 1
        return rows[i] if i >= 0 else None

    def _ask(self, v: str, t: float) -> float | None:
        r = self._at(self.k if v == "K" else self.p, t)
        return r[1] if r else None

    def ladder(self, v: str, t: float) -> list[tuple]:
        """The ask ladder of leg ``v`` as our feed showed it at ``t``."""
        own = self.own.get(v)
        if own is not None and t >= own:
            t = own - 0.001  # what our own fill took would still have been there
        base = self.seen[v]
        for ts, kb, pb in self.opps:
            if self.t0 < ts <= t:
                base = [tuple(lv) for lv in (kb if v == "K" else pb)]
        if v == "K":
            r = self._at(self.k, t)
            if r is None or r[1] is None:
                return []
            return [(r[1], r[2])] + [(a, q) for a, q in base if a > r[1] + EPS]
        top = self._ask("P", t)
        if top is None:
            return []
        lad = [(a, q) for a, q in base if a >= top - EPS]
        if not lad or lad[0][0] > top + EPS:
            lad.insert(0, (top, 1.0))  # a level whose size we never saw: one contract
        return lad

    def avail(self, v: str, t: float, limit: float | None = None) -> int:
        limit = self.limit[v] if limit is None else limit
        return math.floor(sum(q for a, q in self.ladder(v, t) if a <= limit + EPS) + EPS)

    def moved(self, v: str, wait: float) -> bool:
        """This leg's best ask changed within ``wait`` seconds after the decision."""
        rows = self.k if v == "K" else self.p
        own = self.own.get(v)
        end = self.t0 + wait if own is None else min(self.t0 + wait, own - 0.001)
        a0 = self._ask(v, self.t0)
        return any(self.t0 < r[0] <= end and r[1] != a0 for r in rows)

    def room(self) -> float:
        """Whole cents the second leg may cost over its planned limit and still break
        even. The live trader works this out from the first leg's real cost and fees;
        here it is the planned profit per contract, less a cent, which is never more
        (the planned limit is the worst price of the leg, fees round up, and its cap
        sits on whole cents)."""
        return max(0.0, math.floor(100 * self.row["planned_profit"] / self.n + 1e-6) - 1) / 100

    def fill(self, v: str, qty: float, arrive: float, limit: float | None = None) -> int:
        """Contracts an immediate-or-cancel buy reaching the exchange at ``arrive`` gets."""
        if qty < 1 or (v == "P" and self.frozen):
            return 0
        got = min(math.floor(qty + EPS), self.avail(v, arrive + LAG[v], limit))
        offered = self.limit[v] if limit is None else limit
        for at, real_limit, filled in self.short[v]:
            if arrive >= at - 0.02 and offered <= real_limit + EPS:
                got = min(got, math.floor(filled + EPS))
        return got

    def real(self) -> str:
        if self.row["status"] == "missed":
            return "missed"
        return "sold back" if (self.row["unwind_qty"] or 0) > 0 else "filled"


def simulate(d: Decision, pol: Policy, lat: dict) -> tuple[str, list[tuple]]:
    """One decision under one policy: (outcome, [(venue, contracts ordered, filled)]).
    ``lat``: {"look": {venue: decision -> exchange}, "rtt": {venue: round trip}}, seconds."""
    t, n, orders = d.t0, d.n, []
    if d.frozen and pol.check and pol.check_sees_frozen:
        return "skipped", orders
    wait = max(pol.settle_s - max(d.steady.values()), pol.quiet_s - min(d.steady.values()))
    if wait > 0 and not d.frozen:  # hold off; the pick must still be there, its prices unmoved
        if d.moved("K", wait) or d.moved("P", wait):
            return "skipped", orders
        t += wait
    if (pol.check or wait > 0) and not d.frozen:  # sized on the books as they stand when the orders go
        n = min(n, d.avail("P", t), d.avail("K", t))
        if n < 1 or (n < d.n and d.row["planned_profit"] * n / d.n < min(MIN_PROFIT, d.row["planned_profit"]) - EPS):
            return "skipped", orders  # nothing there, or what's left isn't worth a trade
    lead = pol.lead if pol.lead in ("K", "P") else ("K" if d.steady["K"] < d.steady["P"] else "P")
    follow = "P" if lead == "K" else "K"
    got = {lead: d.fill(lead, n, t + lat["look"][lead]), follow: 0}
    orders.append((lead, n, got[lead]))
    t += lat["rtt"][lead]
    if got[lead] >= 1:
        limit = d.limit[follow] + d.room() if pol.breakeven else None
        got[follow] = d.fill(follow, got[lead], t + lat["look"][follow], limit)
        orders.append((follow, got[lead], got[follow]))
        if got[follow] < got[lead] and not pol.breakeven:
            orders.append((follow, got[lead] - got[follow], 0))  # the second try at break-even
    if got[lead] < 1:
        return "missed", orders
    return ("filled" if got[lead] == got[follow] else "sold back"), orders


# --- loading ---------------------------------------------------------------------------

def load(db, before: float = 600.0, after: float = 20.0) -> list[Decision]:
    """Every recorded decision whose quotes are still stored, one per window."""
    rows = []
    for src in ("live", "dry", "paper"):
        for r in db.execute(f"SELECT * FROM {src}_trades ORDER BY ts"):
            r = dict(r)
            r["src"], r["books"] = src, json.loads(r["books"] or "{}")
            if r["books"].get("seen") and r["books"].get("steady") and r["planned_size"]:
                rows.append(r)
    rows.sort(key=lambda r: (RANK[r["src"]], r["ts"]))
    kept: list[dict] = []
    for r in rows:  # a window the live trader took is its trade, not also the paper trader's
        if not any(k["pair"] == r["pair"] and k["direction"] == r["direction"] and abs(k["ts"] - r["ts"]) < SAME_S
                   for k in kept):
            kept.append(r)
    out = []
    cols = "ts, k_yes_ask, k_yes_sz, k_no_ask, k_no_sz, p_yes_bid, p_yes_ask"
    for r in sorted(kept, key=lambda r: r["ts"]):
        quotes = [tuple(q) for q in db.execute(
            f"SELECT {cols} FROM quotes WHERE pair = ? AND ts BETWEEN ? AND ? ORDER BY ts",
            (r["pair"], r["ts"] - before, r["ts"] + after))]
        if not any(q[0] <= r["ts"] for q in quotes):
            continue  # older than the quotes kept
        opps = [tuple(o) for o in db.execute(
            "SELECT ts, direction, k_book, p_book FROM opportunities WHERE pair = ? AND ts BETWEEN ? AND ? ORDER BY ts",
            (r["pair"], r["ts"] - 5, r["ts"] + 5))]
        orders = [dict(o) for o in db.execute(
            "SELECT ts, venue, action, qty, limit_price, filled, rtt_ms, exch_ts FROM live_orders "
            "WHERE trade = ? AND mode = 'live' ORDER BY ts", (r["id"],))] if r["src"] == "live" else []
        out.append(Decision(r, quotes, opps, orders))
    return out


def timings(decisions: list[Decision]) -> dict[str, list[tuple[float, float]]]:
    """The live orders' own (decision -> exchange, round trip) per venue, to draw from."""
    out: dict[str, list] = defaultdict(list)
    for d in decisions:
        for i, o in enumerate(d.orders):
            if o["exch_ts"] and o["rtt_ms"]:
                start = d.t0 if i == 0 else o["ts"]
                out[o["venue"]].append((max(0.0, o["exch_ts"] - start), o["rtt_ms"] / 1000 + o["ts"] - start))
    return {v: out.get(v) or [DEFAULT_TIMING[v]] for v in "KP"}


def draw(pool: dict, rng: random.Random) -> dict:
    k, p = rng.choice(pool["K"]), rng.choice(pool["P"])
    return {"look": {"K": k[0], "P": p[0]}, "rtt": {"K": k[1], "P": p[1]}}


def agreement(decisions: list[Decision]) -> tuple[int, int]:
    """(live trades whose real outcome the model reproduces from their real timings and order, live trades)."""
    same = total = 0
    for d in decisions:
        buys = [o for o in d.orders if o["action"] == "buy" and o["exch_ts"]]
        if d.src != "live" or not buys:
            continue
        total += 1
        lead = buys[0]["venue"]
        follow = "P" if lead == "K" else "K"
        back = buys[0]["ts"] + buys[0]["rtt_ms"] / 1000 - d.t0
        second = next((o for o in buys[1:] if o["venue"] == follow), None)
        lat = {"look": {lead: buys[0]["exch_ts"] - d.t0, follow: second["exch_ts"] - (d.t0 + back) if second else 0.1},
               "rtt": {lead: back, follow: 0.2}}
        same += simulate(d, Policy("real", lead=lead), lat)[0] == d.real()
    return same, total


def replay(decisions: list[Decision], pol: Policy, draws: int, rng: random.Random, pool: dict | None = None) -> dict:
    """Average outcome counts over ``draws`` timings per decision."""
    pool = pool or timings(decisions)
    c: Counter[str] = Counter()
    asked = bought = 0.0
    for d in decisions:
        for _ in range(draws):
            out, orders = simulate(d, pol, draw(pool, rng))
            c[out] += 1
            asked += sum(o[1] for o in orders)
            bought += sum(o[2] for o in orders)
    r = {k: c[k] / draws for k in ("filled", "missed", "sold back", "skipped")}
    r["trades"] = r["filled"] + r["missed"] + r["sold back"]
    r["fill_rate"] = r["filled"] / r["trades"] if r["trades"] else None
    r["order_rate"] = bought / asked if asked else None
    return r


def interval(decisions: list[Decision], pol: Policy, draws: int, rng: random.Random, pool: dict,
             resamples: int = 1000) -> tuple[float, float] | None:
    """A 95% interval for the fill rate: the decisions resampled with replacement, so it
    says how far the rate could be off for having seen only these."""
    per = []  # per decision: (trades, filled), averaged over the timings
    for d in decisions:
        c: Counter[str] = Counter(simulate(d, pol, draw(pool, rng))[0] for _ in range(draws))
        per.append(((c["filled"] + c["missed"] + c["sold back"]) / draws, c["filled"] / draws))
    rates = []
    for _ in range(resamples):
        pick = rng.choices(per, k=len(per))
        trades = sum(t for t, _ in pick)
        if trades:
            rates.append(sum(f for _, f in pick) / trades)
    rates.sort()
    return (rates[int(0.025 * len(rates))], rates[int(0.975 * len(rates)) - 1]) if rates else None


def run(db, draws: int = 100, seed: int = 7) -> None:
    decisions = load(db)
    if not decisions:
        print("no recorded decisions with quotes still stored")
        return
    by_src = Counter(d.src for d in decisions)
    day = lambda ts: datetime.fromtimestamp(ts).strftime("%Y-%m-%d")  # noqa: E731
    print(f"{len(decisions)} recorded decisions ({by_src['live']} live, {by_src['dry']} dry run, "
          f"{by_src['paper']} paper), {day(decisions[0].t0)} to {day(decisions[-1].t0)}, "
          f"{draws} order timings each")
    same, total = agreement(decisions)
    frozen = sum(d.frozen for d in decisions)
    print(f"model check: reproduces {same} of the {total} live trades' real outcomes from their real timings; "
          f"{frozen} of them met a frozen Polymarket US book")
    pool = timings(decisions)
    groups = [("all decisions", decisions), ("live trades (real orders)", [d for d in decisions if d.src == "live"]),
              ("paper and dry-run decisions", [d for d in decisions if d.src != "live"])]
    pct = lambda x: "    -" if x is None else f"{100 * x:5.1f}%"  # noqa: E731
    for title, group in groups:
        if not group:
            continue
        print(f"\n{title}: {len(group)}")
        print(f"  {'':44s} {'trades':>7s} {'filled':>7s} {'missed':>7s} {'sold back':>10s} {'skipped':>8s} "
              f"{'filled %':>9s} {'orders %':>9s}")
        for pol in POLICIES:
            r = replay(group, pol, draws, random.Random(seed), pool)
            print(f"  {pol.name:44s} {r['trades']:7.1f} {r['filled']:7.1f} {r['missed']:7.1f} {r['sold back']:10.1f} "
                  f"{r['skipped']:8.1f} {pct(r['fill_rate']):>9s} {pct(r['order_rate']):>9s}")
    print("\nfilled %, with the range it could lie in for having seen only these decisions (95%):")
    for pol in (BEFORE, NOW, POLICIES[5]):
        lo_hi = interval(decisions, pol, max(10, draws // 5), random.Random(seed), pool)
        r = replay(decisions, pol, draws, random.Random(seed), pool)
        print(f"  {pol.name:44s} {pct(r['fill_rate'])}   {pct(lo_hi[0])} to {pct(lo_hi[1])}" if lo_hi else "")
    print("\nby day (filled % as it was -> as it is; trades as it is):")
    days: dict[str, list[Decision]] = defaultdict(list)
    for d in decisions:
        days[day(d.t0)].append(d)
    for name, group in sorted(days.items()):
        a = replay(group, BEFORE, draws, random.Random(seed), pool)
        b = replay(group, NOW, draws, random.Random(seed), pool)
        print(f"  {name}  {len(group):3d} decisions   {pct(a['fill_rate'])} -> {pct(b['fill_rate'])}   "
              f"{b['trades']:5.1f} trades, {b['missed']:.1f} missed, {b['sold back']:.1f} sold back")
