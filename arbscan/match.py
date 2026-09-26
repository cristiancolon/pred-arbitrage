"""Suggest Kalshi <-> Polymarket US market pairs.

This is a heuristic candidate generator, not a verdict: two markets can read alike
and still resolve differently. Every suggestion goes through ``arbscan review`` (or an
explicit auto-approve rule) before the scanner watches it.

Hard filters (any mismatch rejects the candidate):
  * strikes: thresholds like 7.5, $130k, 58%, "2+" (== 1.5) must be identical;
  * years and months, when both sides mention them;
  * qualifiers: game segment (1st half, Q3, 5th inning, map 2), placement (2nd, top
    20), stage (qualify, finalist, make the cut), draws/ties, exact scores, district
    codes (MI-04), and for sports the bet type (spread / total / team total);
  * stat words (passing touchdowns vs touchdowns vs completions);
  * game start times within 12 hours;
  * outcome labels: Kalshi's YES subject must line up with one Polymarket side. That
    side decides the relation: "same" (Polymarket YES == Kalshi YES) or "inverse"
    (Polymarket YES == Kalshi NO, e.g. Kalshi "Atlanta wins" vs a Reds/Braves
    moneyline whose long side is the Reds). Spread signs must agree too.
Soft score: IDF-weighted cosine similarity of the words in titles, outcome labels
and the first sentence of the rules, scaled down for timing distance.
"""

import logging
import math
import re
import sqlite3
import sys
import time
import unicodedata
from array import array
from collections import Counter, defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, replace

from .config import Config
from .pairs import append_pair
from .review import record

log = logging.getLogger(__name__)

STOP = frozenset("""
a an the of in on at to by be is are was for and or vs v versus who what which will
win wins winner won market markets event events game games match matches upcoming
scheduled resolve resolves resolved yes no than more less before after during between
from with into this that it its end their there does do team teams how many much if
then otherwise settle settles settled originally professional official
""".split())

MONTHS = {
    "january": "jan", "february": "feb", "march": "mar", "april": "apr", "june": "jun",
    "july": "jul", "august": "aug", "september": "sep", "sept": "sep", "october": "oct",
    "november": "nov", "december": "dec",
}
MONTH_ABBRS = frozenset(MONTHS.values())  # "may" is left out: too often a verb

STAT_WORDS = frozenset("""
touchdowns touchdown completions yards receptions interceptions sacks hits strikeouts
bases homers rbis rebounds assists threes goals saves shots aces kills passing rushing
receiving doubles triples average era stolen steals war ops whip
""".split())
_STAT_CANON = {"touchdown": "touchdowns"}

# Filler in outcome labels ("Cincinnati wins by over 1.5 runs") that says nothing
# about *who*; ignored when lining labels up across venues.
LABEL_NOISE = frozenset("over points point runs run goals goal scored pts".split())
_K_CODE = re.compile(r"([a-z]{2,4})\d*")  # ticker suffix "CIN" / "CIN2" / "WAKE11"

_WORD = re.compile(r"[a-z][a-z0-9']*")
_NUM = re.compile(r"(\$)?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\s*(k|m|bn|b|%|\+)?(?![a-z0-9])", re.I)
_MULT = {"k": 1e3, "m": 1e6, "b": 1e9, "bn": 1e9}

_ORD = {"1st": 1, "first": 1, "2nd": 2, "second": 2, "3rd": 3, "third": 3, "4th": 4, "fourth": 4,
        "5th": 5, "fifth": 5, "6th": 6, "sixth": 6, "7th": 7, "seventh": 7, "8th": 8, "eighth": 8,
        "9th": 9, "ninth": 9}
_SEGMENTS = {"half": "half", "quarter": "quarter", "inning": "inning", "period": "period",
             "round": "round", "set": "set", "map": "map", "game": "game", "week": "week", "wk": "week"}
_Q_ORD_SEGMENT = re.compile(r"\b(" + "|".join(_ORD) + r")\s+(half|quarter|inning|period|round|set|map|game)\b")
_Q_SHORT = re.compile(r"\b([1-4])(h|q)\b")
_Q_INNING = re.compile(r"\bi([1-9])\b")  # Polymarket US slug code, e.g. "...-i5-sd"
_Q_FIRST_N = re.compile(r"\bfirst ([1-9]) innings\b|\bf([1-9])\b")  # "first 5 innings" / slug "f5"
_Q_SEGMENT_N = re.compile(r"\b(map|game|set|round|inning|period|week|wk)\s*(\d{1,2})\b")
_Q_TOP = re.compile(r"\btop\s*(\d+)\b")
_Q_ORDINAL = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)\b|#\s*(\d{1,2})\b|\b(?:no\.|number)\s*(\d{1,2})\b")
_Q_DISTRICT = re.compile(r"\b([a-z]{2})-(\d{2})\b")
_Q_SCORE = re.compile(r"\b(?:draw|score[sd]?|wins?|by)\s+(\d{1,2})-(\d{1,2})\b")
_Q_WORDS = [
    (re.compile(r"\b(draw|draws|tie|tied|ties)\b"), "draw"),
    (re.compile(r"\bqualif"), "qualify"),
    (re.compile(r"\bfinalists?\b"), "finalist"),
    (re.compile(r"\bnominat"), "nominee"),
    (re.compile(r"\bmakes? the cut\b|\bmake cut\b"), "makecut"),
    (re.compile(r"\bmatchup\b"), "matchup"),
    (re.compile(r"\brunner-?up\b"), "#2"),
]
# In a rule's first sentence: "1+ touchdowns (excluding passing touchdowns)" is a
# different bet from "1+ passing touchdowns" even though the words overlap.
_EXCLUSION = re.compile(r"\bexclud|\bnot includ|\bother than\b")
_Q_TEAM_TOTAL = re.compile(r"\bteam[ _](?:points?[ _]|runs?[ _]|goals?[ _])?total\b|\btt\b|team_points")
_Q_SPREAD = re.compile(r"\bspread\b|\bcover\b|\bwins? (?:\w+ )?by\b")
_Q_TOTAL = re.compile(r"\btotal\b|_total\b")
_K_SPREAD_SIGN = re.compile(r"\bwins?\b.*\bby (?:over|more than) \d")
_SIGNED_LINE = re.compile(r"(?:^|\s)([+-])\d")  # "-1.50 · Reds" / "Pittsburgh -1.5 first 5"
_DATE = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})\b")

START_WINDOW_S = 12 * 3600
# Words in more markets than this ("sep", "football", "st") say nothing about who is
# playing, so they don't count as the opponent.
OPPONENT_MAX_DF = 10000
# Games starting within this window of each other with agreeing teams, lines and
# qualifiers are strong evidence even when the wording differs (mascot vs city).
STRUCTURED_WINDOW_S = 2 * 3600
CLOSE_WINDOW_DAYS = 120
LABEL_MIN_COVERAGE = 0.6
MAX_BLOCK_DF = 400  # words in more Polymarket markets than this are too common to block on
MAX_CANDIDATES = 40
KEEP_PER_MARKET = 3
RULE_WORDS = 40


EMPTY: frozenset = frozenset()


def _fs(items) -> frozenset:
    """frozenset, sharing one object for the (very common) empty case."""
    return frozenset(items) if items else EMPTY


def ascii_lower(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()


def words(text: str) -> set[str]:
    out = set()
    for w in _WORD.findall(ascii_lower(text)):
        w = w.replace("'", "")
        w = MONTHS.get(w, w)
        if len(w) > 1 and w not in STOP:
            out.add(sys.intern(w))
    return out


def numbers(text: str) -> tuple[frozenset[float], frozenset[int]]:
    """(strikes, years) mentioned in ``text``. Small bare integers (days, times,
    counts) are ignored as noise; "N+" means "over N - 0.5"."""
    strikes, years = set(), set()
    for dollar, whole, frac, suffix in _NUM.findall(text):
        v = float(whole.replace(",", "") + (frac or ""))
        suffix = (suffix or "").lower()
        if suffix in _MULT:
            strikes.add(round(v * _MULT[suffix], 6))
        elif suffix == "+" and not frac:
            strikes.add(v - 0.5)
        elif frac or suffix in ("%", "+") or dollar:
            strikes.add(round(v, 6))
        elif 2000 <= v <= 2100:
            years.add(int(v))
        elif v >= 100:
            strikes.add(v)
    return _fs(strikes), _fs(years)


def months(text: str) -> frozenset[str]:
    return _fs(words(text) & MONTH_ABBRS)


def dates(text: str) -> frozenset[str]:
    """Month-day mentions like "Dec 1" or "December 31" -> {"dec1"}, {"dec31"}."""
    return _fs({f"{m}{int(d)}" for m, d in _DATE.findall(ascii_lower(text))})


def qualifiers(text: str, sports: bool) -> frozenset[str]:
    """Tokens that change *which* outcome a market is about even when the rest of
    the wording matches."""
    t = ascii_lower(text)
    q = set()
    for m in _Q_ORD_SEGMENT.finditer(t):
        q.add(f"{_SEGMENTS[m.group(2)]}{_ORD[m.group(1)]}")
    t = _Q_ORD_SEGMENT.sub(" ", t)  # so "1st half" doesn't also count as ordinal "1st"
    for m in _Q_SHORT.finditer(t):
        q.add(("half" if m.group(2) == "h" else "quarter") + m.group(1))
    for m in _Q_INNING.finditer(t):
        q.add(f"inning{m.group(1)}")
    for m in _Q_FIRST_N.finditer(t):
        q.add(f"first{m.group(1) or m.group(2)}")
    for m in _Q_SEGMENT_N.finditer(t):
        q.add(f"{_SEGMENTS[m.group(1)]}{int(m.group(2))}")
    for m in _Q_TOP.finditer(t):
        q.add(f"top{int(m.group(1))}")
    for m in _Q_ORDINAL.finditer(t):
        q.add(f"#{int(next(g for g in m.groups() if g))}")
    for m in _Q_DISTRICT.finditer(t):
        q.add(f"{m.group(1)}{m.group(2)}")
    for m in _Q_SCORE.finditer(t):
        q.add(f"score{m.group(1)}-{m.group(2)}")
    for pattern, token in _Q_WORDS:
        if pattern.search(t):
            q.add(token)
    if sports:
        if _Q_TEAM_TOTAL.search(t):
            q.add("teamtotal")
        elif _Q_TOTAL.search(t):
            q.add("total")
        if _Q_SPREAD.search(t):
            q.add("spread")
    for sub, token in (("first_half", "half1"), ("second_half", "half2")):
        if sub in t:
            q.add(token)
    return _fs(q)


def stats(ws: set[str]) -> frozenset[str]:
    return _fs({_STAT_CANON.get(w, w) for w in ws if w in STAT_WORDS})


def first_sentence(text: str | None) -> str:
    text = (text or "").strip()
    end = re.search(r"[.!?](\s|$)", text)
    return " ".join((text[: end.start()] if end else text).split()[:RULE_WORDS])


def line_sign(label: str) -> str:
    """'neg' for a favourite's line (-1.5, "wins by over 1.5"), 'pos' for +1.5."""
    t = ascii_lower(label or "")
    if _K_SPREAD_SIGN.search(t):
        return "neg"
    m = _SIGNED_LINE.search(t)
    return "" if not m else ("neg" if m.group(1) == "-" else "pos")


class Vocab:
    """Maps words to small ints so documents can be stored as compact arrays."""

    def __init__(self) -> None:
        self.ids: dict[str, int] = {}
        self.df = array("I")

    def ids_for(self, ws: set[str], count: bool) -> set[int]:
        out = set()
        for w in ws:
            i = self.ids.get(w)
            if i is None:
                i = self.ids[w] = len(self.ids)
                self.df.append(0)
            if count:
                self.df[i] += 1
            out.add(i)
        return out


@dataclass(slots=True)
class Doc:
    id: str
    series: str
    words: set[int] | array  # set while scoring a Kalshi doc; array for stored PM docs
    yes: set[int] | array
    no: set[int] | array
    yes_sign: str
    no_sign: str
    code: int | None  # Kalshi ticker team code (e.g. "lad"), when there is one
    opp: set[int] | None  # Kalshi event-title words other than the YES subject
    strikes: frozenset[float]
    years: frozenset[int]
    months: frozenset[str]
    dates: frozenset[str]
    quals: frozenset[str]
    stats: frozenset[str]
    start: float | None
    close: float | None
    drawable: bool
    norm: float = 0.0


def kalshi_doc(r: sqlite3.Row, vocab: Vocab, count: bool) -> Doc:
    yes_label = r["yes_label"] or ""
    m = _K_CODE.fullmatch(r["id"].rsplit("-", 1)[-1].lower())
    suffix = sys.intern(m.group(1)) if m else None
    head = f"{r['title']} | {yes_label}"
    rule = first_sentence(r["rules"])
    s, y = numbers(f"{head} | {rule}")
    yes = words(yes_label) - LABEL_NOISE
    text = words(f"{head} | {rule}") | yes | ({suffix} if suffix else set())
    text_ids = vocab.ids_for(text, count)
    code = vocab.ids[suffix] if suffix else None
    # "Milwaukee vs Philadelphia | MIL vs PHI (Sep 24) | ..." minus the YES team
    # leaves the opponent, which must also appear on the Polymarket side of a game.
    event = " | ".join((r["title"] or "").split(" | ")[:2])
    opp = words(event) - yes - MONTH_ABBRS - STAT_WORDS - {suffix}
    sports = (r["category"] or "").lower() == "sports"
    q = qualifiers(head, sports)
    if _EXCLUSION.search(ascii_lower(rule)):
        q = q | {"excludes"}
    return Doc(r["id"], sys.intern(r["series"] or ""), text_ids, vocab.ids_for(yes, False), (),
               line_sign(yes_label), "", code, vocab.ids_for(opp, False), s, y, months(head), dates(head), q, stats(text),
               r["start_ts"], r["close_ts"], False)


def pm_doc(r: sqlite3.Row, vocab: Vocab) -> Doc:
    yes_label, no_label = r["yes_label"] or "", r["no_label"] or ""
    yes, no = words(yes_label) - LABEL_NOISE, words(no_label) - LABEL_NOISE
    slug = r["id"]
    slug_text = " ".join(slug.split("-")[1:])  # drop the product-code prefix
    market_type = r["market_type"] or ""
    rule = first_sentence(r["rules"])
    # Numbers come from the question, YES label and rule: the NO label of a spread
    # ("+2.50") restates the same line with the other sign.
    s, y = numbers(f"{r['title']} | {yes_label} | {rule}")
    text = words(f"{r['title']} | {rule} | {slug_text}") | yes | no
    sports = (r["category"] or "").lower() == "sports"
    q = qualifiers(f"{r['title']} | {yes_label} | {slug_text} | {market_type}", sports)
    if _EXCLUSION.search(ascii_lower(rule)):
        q |= {"excludes"}

    def arr(ws: set[str], count: bool = False) -> array:
        return array("I", sorted(vocab.ids_for(ws, count)))

    head = f"{r['title']} | {yes_label}"
    return Doc(slug, slug.split("-", 1)[0], arr(text, True), arr(yes), arr(no), line_sign(yes_label),
               line_sign(no_label), None, None, s, y, months(head), dates(head), q, stats(text),
               r["start_ts"], r["close_ts"], market_type.startswith("drawable_outcome"))


# Bet types that read alike but settle differently, told apart when matching Novig
# (whose titles are built from its structured types) against the other venues.
_EXTRA_QUALS = [
    (re.compile(r"(?<!full[ _])games?[ _]spread|total[ _]games|more games|games? won|over [\d.]+ games"), "games"),
    (re.compile(r"sets?[ _]spread|total[ _]sets|more sets|sets? won"), "sets"),
    (re.compile(r"first[ _]team[ _]to[ _]score|first goal|score first|first[ _]to[ _]score"), "firstscore"),
    (re.compile(r"both[ _]teams[ _]to[ _]score|btts"), "btts"),
    (re.compile(r"submission|knock ?out|\bk\.?o\b|\btko\b|decision|method[ _]of|go(?:es)?[ _]the[ _]distance"
                r"|round[ _]of[ _](?:victory|finish)"), "method"),
]


def extra_quals(text: str) -> frozenset[str]:
    t = ascii_lower(text)
    return _fs({token for pattern, token in _EXTRA_QUALS if pattern.search(t)})


def novig_doc(r: sqlite3.Row, vocab: Vocab) -> Doc:
    """A Novig market in Polymarket's place (indexed), to match Kalshi markets against."""
    yes_label, no_label = r["yes_label"] or "", r["no_label"] or ""
    yes, no = words(yes_label) - LABEL_NOISE, words(no_label) - LABEL_NOISE
    rule = first_sentence(r["rules"])
    head = f"{r['title']} | {yes_label}"
    s, y = numbers(f"{head} | {rule}")
    text = words(f"{r['title']} | {rule}") | yes | no

    def arr(ws: set[str], count: bool = False) -> array:
        return array("I", sorted(vocab.ids_for(ws, count)))

    return Doc(r["id"], sys.intern(r["series"] or ""), arr(text, True), arr(yes), arr(no), line_sign(yes_label),
               line_sign(no_label), None, None, s, y, months(head), dates(head),
               qualifiers(head, True) | extra_quals(head), stats(text), r["start_ts"], r["close_ts"], False)


_NOVIG_CODE = re.compile(r"\(([a-z]{2,5})\)$")  # "DET -7.5 · Detroit Lions (det)"
_NOVIG_EVENT = re.compile(r"\s*\([^)]*\)$")  # "... @ Detroit Lions (NFL, Sep 27)"


def novig_query_doc(r: sqlite3.Row, vocab: Vocab) -> Doc:
    """A Novig market in Kalshi's place (streamed past an index), to match it against
    Polymarket US. Its words must already be in ``vocab`` (from ``novig_doc``)."""
    yes_label = r["yes_label"] or ""
    m = _NOVIG_CODE.search(yes_label)
    code = m.group(1) if m else None
    head = f"{r['title']} | {yes_label}"
    rule = first_sentence(r["rules"])
    s, y = numbers(f"{head} | {rule}")
    yes = words(yes_label) - LABEL_NOISE
    text = words(f"{head} | {rule}") | yes
    event = _NOVIG_EVENT.sub("", (r["title"] or "").split(" | ")[0])
    opp = words(event) - yes - MONTH_ABBRS - STAT_WORDS - ({code} if code else set())
    return Doc(r["id"], sys.intern(r["series"] or ""), vocab.ids_for(text, False), vocab.ids_for(yes, False), (),
               line_sign(yes_label), "", vocab.ids.get(code) if code else None, vocab.ids_for(opp, False), s, y,
               months(head), dates(head), qualifiers(head, True) | extra_quals(head), stats(text), r["start_ts"],
               r["close_ts"], False)


ROW_COLS = "id, series, category, title, yes_label, no_label, market_type, start_ts, close_ts, rules"


def _rows(db: sqlite3.Connection, venue: str, quoted: bool = True) -> Iterator[sqlite3.Row]:
    # Batch matching skips markets with an empty book on both sides; nothing to arb
    # there. Live discovery keeps them: a market just listed has no quotes yet.
    sql = f"SELECT {ROW_COLS} FROM markets WHERE venue = ?"
    if quoted:
        sql += " AND ((yes_bid IS NOT NULL AND yes_bid > 0) OR (yes_ask IS NOT NULL AND yes_ask < 1))"
    return db.execute(sql, (venue,))


@dataclass(slots=True)
class Candidate:
    kalshi: str
    pm: str
    score: float
    relation: str
    confident: bool


class Matcher:
    """Indexes Polymarket US docs; Kalshi docs are streamed past it one at a time."""

    def __init__(self, pm: list[Doc], vocab: Vocab, n_docs: int):
        self.p = pm
        self.df = vocab.df
        self.idf = array("d", (math.log(n_docs / c) if c else 0.0 for c in vocab.df))
        p_df: Counter[int] = Counter()
        for d in pm:
            p_df.update(d.words)
            d.norm = self.w(d.words)
        self.index: dict[int, array] = defaultdict(lambda: array("I"))
        for i, d in enumerate(pm):
            for t in d.words:
                if p_df[t] <= MAX_BLOCK_DF:
                    self.index[t].append(i)

    def w(self, toks) -> float:
        idf = self.idf
        return sum(idf[t] for t in toks)

    def coverage(self, a, b: set[int]) -> float:
        """Share of ``a``'s (IDF-weighted) words that also appear in ``b``."""
        total = self.w(a)
        return sum(self.idf[t] for t in a if t in b) / total if total else 0.0

    def side_match(self, k: Doc, side: array, sign: str) -> float:
        """How well Kalshi's YES label lines up with one Polymarket side, 0..1."""
        if not side or (k.yes_sign and k.yes_sign != sign):
            return 0.0
        s = set(side)
        if k.code is not None and k.code in s:
            return 1.0
        return max(self.coverage(k.yes, s), self.coverage(side, k.yes))

    def opponent_agrees(self, k: Doc, p: Doc) -> bool:
        """In a game, the rest of the Kalshi event title (the other team, or both
        teams for a player prop) must also appear on the Polymarket side."""
        df = self.df
        opp = {t for t in k.opp if df[t] <= OPPONENT_MAX_DF}
        return not opp or any(t in opp for t in p.words)

    def score(self, k: Doc, p: Doc) -> Candidate | None:
        """``k`` holds Python sets (fast membership), ``p`` holds arrays."""
        if k.strikes != p.strikes or k.quals != p.quals or k.stats != p.stats:
            return None
        if k.years and p.years and not (k.years & p.years):
            return None
        if k.months and p.months and not (k.months & p.months):
            return None
        game = k.start is not None and p.start is not None
        time_factor = 1.0
        if game:
            dt = abs(k.start - p.start)
            if dt > START_WINDOW_S:
                return None
            time_factor = 1.0 - 0.5 * dt / START_WINDOW_S
        elif k.dates and p.dates and not (k.dates & p.dates):
            # Games are checked by start time instead: a late ET game has a
            # different UTC date on one venue.
            return None
        elif k.close is not None and p.close is not None:
            days = abs(k.close - p.close) / 86400
            if days > CLOSE_WINDOW_DAYS:
                return None
            time_factor = 1.0 - 0.5 * max(0.0, days - 14) / CLOSE_WINDOW_DAYS

        if (k.yes or k.code is not None) and (p.yes or p.no):
            m_same = self.side_match(k, p.yes, p.yes_sign)
            m_inv = self.side_match(k, p.no, p.no_sign)
            if max(m_same, m_inv) < LABEL_MIN_COVERAGE:
                return None  # labels name different things (e.g. different teams)
            if m_inv > m_same:
                if p.drawable:
                    return None  # with a draw possible, "A loses" is not "B wins"
                relation = "inverse"
            else:
                relation = "same"
            if game and not self.opponent_agrees(k, p):
                return None
            confident = True
        else:
            # Polymarket gives no subject; Kalshi's YES subject must be in its text.
            if k.yes and self.coverage(k.yes, set(p.words)) < LABEL_MIN_COVERAGE:
                return None
            relation, confident = "same", False

        shared = sum(self.idf[t] for t in p.words if t in k.words)
        denom = math.sqrt(k.norm * p.norm) or 1.0
        score = shared / denom * time_factor
        if confident and k.start is not None and p.start is not None and abs(k.start - p.start) <= STRUCTURED_WINDOW_S:
            score += (1.0 - score) * 0.4
        return Candidate(k.id, p.id, score, relation, confident)

    def candidates_for(self, k: Doc, min_score: float) -> list[Candidate]:
        k.norm = self.w(k.words)
        hits: Counter[int] = Counter()
        idf = self.idf
        for t in k.words:
            postings = self.index.get(t)
            if postings:
                wt = idf[t]
                for i in postings:
                    hits[i] += wt
        out = []
        for i, _ in hits.most_common(MAX_CANDIDATES):
            c = self.score(k, self.p[i])
            if c and c.score >= min_score:
                out.append(c)
        out.sort(key=lambda c: -c.score)
        return out[:KEEP_PER_MARKET]


def _compact(d: Doc) -> Doc:
    d.words, d.yes, d.opp = array("I", sorted(d.words)), array("I", sorted(d.yes)), array("I", sorted(d.opp or ()))
    return d


def _expanded(d: Doc) -> Doc:
    """A stored Kalshi doc in the set form ``Matcher.score`` expects for its first argument."""
    return replace(d, words=set(d.words), yes=set(d.yes), opp=set(d.opp), no=())


class LiveIndex:
    """Both venues in memory, for matching markets one at a time as they are listed
    (discover.py). Built like ``run``, but it keeps the Kalshi docs and indexes them
    too, so a new Polymarket market can be matched against Kalshi as well as the
    other way round. Words first seen after the build get IDF weights on the fly;
    the rebuild after each full catalog refresh re-weights everything."""

    def __init__(self, db: sqlite3.Connection):
        t0 = time.monotonic()
        self.vocab = Vocab()
        pm = [pm_doc(r, self.vocab) for r in _rows(db, "P", quoted=False)]
        kdocs = [_compact(kalshi_doc(r, self.vocab, count=True)) for r in _rows(db, "K", quoted=False)]
        self.n_docs = max(1, len(kdocs) + len(pm))
        self.m = Matcher(pm, self.vocab, self.n_docs)
        self.p_ids = {d.id for d in pm}
        self.p_df: Counter[int] = Counter()
        for d in pm:
            self.p_df.update(d.words)
        self.k: list[Doc] = kdocs
        self.k_ids = {d.id for d in kdocs}
        self.k_df: Counter[int] = Counter()
        for d in kdocs:
            self.k_df.update(d.words)
            d.norm = self.m.w(d.words)
        self.k_index: dict[int, array] = defaultdict(lambda: array("I"))
        for i, d in enumerate(kdocs):
            for t in d.words:
                if self.k_df[t] <= MAX_BLOCK_DF:
                    self.k_index[t].append(i)
        log.info("live index: %d Kalshi + %d Polymarket US markets in %.0fs", len(kdocs), len(pm),
                 time.monotonic() - t0)

    def _grow_idf(self) -> None:
        idf, df = self.m.idf, self.vocab.df
        while len(idf) < len(df):
            idf.append(math.log(self.n_docs / max(1, df[len(idf)])))

    def add_kalshi(self, row, min_score: float) -> list[Candidate]:
        if row["id"] in self.k_ids:
            return []
        d = kalshi_doc(row, self.vocab, count=True)
        self._grow_idf()
        found = self.m.candidates_for(d, min_score)
        _compact(d)
        i = len(self.k)
        self.k.append(d)
        self.k_ids.add(d.id)
        for t in d.words:
            self.k_df[t] += 1
            if self.k_df[t] <= MAX_BLOCK_DF:
                self.k_index[t].append(i)
        return found

    def add_pm(self, row, min_score: float) -> list[Candidate]:
        if row["id"] in self.p_ids:
            return []
        p = pm_doc(row, self.vocab)
        self._grow_idf()
        p.norm = self.m.w(p.words)
        hits: Counter[int] = Counter()
        idf = self.m.idf
        for t in p.words:
            for i in self.k_index.get(t, ()):
                hits[i] += idf[t]
        found = []
        for i, _ in hits.most_common(MAX_CANDIDATES):
            c = self.m.score(_expanded(self.k[i]), p)
            if c and c.score >= min_score:
                found.append(c)
        found.sort(key=lambda c: -c.score)
        j = len(self.m.p)
        self.m.p.append(p)
        self.p_ids.add(p.id)
        for t in p.words:
            self.p_df[t] += 1
            if self.p_df[t] <= MAX_BLOCK_DF:
                self.m.index[t].append(j)
        return found[:KEEP_PER_MARKET]


def run(cfg: Config, db: sqlite3.Connection) -> list[Candidate]:
    t0 = time.monotonic()
    vocab = Vocab()
    pm = [pm_doc(r, vocab) for r in _rows(db, "P")]
    # Tokenize Kalshi once; IDF needs both venues' word counts before scoring, so keep
    # compact (int-array) copies until then (~60 MB for ~90k markets).
    kdocs: list[Doc | None] = []
    for r in _rows(db, "K"):
        d = kalshi_doc(r, vocab, count=True)
        d.words, d.yes, d.opp = array("I", d.words), array("I", d.yes), array("I", d.opp or ())
        kdocs.append(d)
    if not kdocs or not pm:
        raise SystemExit("catalog is empty; run `arbscan catalog` first")
    log.info("matching %d Kalshi x %d Polymarket US markets with quotes", len(kdocs), len(pm))
    m = Matcher(pm, vocab, len(kdocs) + len(pm))
    cands: list[Candidate] = []
    k_series: dict[str, str] = {}
    for i in range(len(kdocs)):
        d, kdocs[i] = kdocs[i], None  # free each doc once scored
        d.words, d.yes, d.opp = set(d.words), set(d.yes), set(d.opp)
        d.no = ()
        found = m.candidates_for(d, cfg.match_min_score)
        if found:
            cands.extend(found)
            k_series[d.id] = d.series
    del m, pm, kdocs

    # Keep at most KEEP_PER_MARKET per Polymarket market too.
    by_p: dict[str, list[Candidate]] = defaultdict(list)
    for c in cands:
        by_p[c.pm].append(c)
    cands = [c for cs in by_p.values() for c in sorted(cs, key=lambda c: -c.score)[:KEEP_PER_MARKET]]
    cands.sort(key=lambda c: -c.score)

    now = time.time()
    db.execute("DELETE FROM candidates")
    db.executemany(
        "INSERT INTO candidates VALUES (?,?,?,?,?,?)",
        [(c.kalshi, c.pm, round(c.score, 4), c.relation, int(c.confident), now) for c in cands],
    )
    db.commit()
    log.info("%d candidates in %.0fs", len(cands), time.monotonic() - t0)

    if cfg.auto_approve:
        auto_approve(cfg, db, cands, k_series)
    return cands


def run_novig(cfg: Config, db: sqlite3.Connection) -> list[tuple[str, Candidate]]:
    """Suggest pairs between Novig and each of the other venues' sports markets:
    Kalshi against an index of Novig (like Kalshi against Polymarket US), and Novig
    against an index of Polymarket US. Returns (other venue, candidate); in a
    candidate, ``kalshi`` is the streamed side (Kalshi, or Novig) and ``relation``
    says whether the indexed side's YES is the streamed side's YES."""
    t0 = time.monotonic()
    vocab = Vocab()
    nrows = [r for r in _rows(db, "N", quoted=False)]
    if not nrows:
        return []
    ndocs = [novig_doc(r, vocab) for r in nrows]
    queries = [novig_query_doc(r, vocab) for r in nrows]
    pdocs = []
    for r in _rows(db, "P"):
        if (r["category"] or "").lower() == "sports":
            d = pm_doc(r, vocab)
            d.quals |= extra_quals(f"{r['title']} | {r['yes_label']} | {r['market_type']}")
            pdocs.append(d)
    kdocs = []
    for r in _rows(db, "K"):
        if (r["category"] or "").lower() == "sports":
            d = kalshi_doc(r, vocab, count=True)
            d.quals |= extra_quals(f"{r['title']} | {r['yes_label']}")
            kdocs.append(_compact(d))
    n_docs = len(ndocs) + len(pdocs) + len(kdocs)
    log.info("matching Novig (%d markets) with %d Kalshi and %d Polymarket US sports markets",
             len(ndocs), len(kdocs), len(pdocs))

    def best(cands: list[Candidate], key) -> list[Candidate]:
        by: dict[str, list[Candidate]] = defaultdict(list)
        for c in cands:
            by[key(c)].append(c)
        return [c for cs in by.values() for c in sorted(cs, key=lambda c: -c.score)[:KEEP_PER_MARKET]]

    out: list[tuple[str, Candidate]] = []
    m = Matcher(ndocs, vocab, n_docs)
    found = [c for d in kdocs for c in m.candidates_for(_expanded(d), cfg.match_min_score)]
    out += [("K", c) for c in best(found, lambda c: c.pm)]
    m = Matcher(pdocs, vocab, n_docs)
    found = [c for q in queries for c in m.candidates_for(q, cfg.match_min_score)]
    out += [("P", c) for c in best(found, lambda c: c.pm)]

    now = time.time()
    db.execute("DELETE FROM novig_candidates")
    db.executemany("INSERT INTO novig_candidates VALUES (?,?,?,?,?,?,?)", [
        (v, c.kalshi if v == "K" else c.pm, c.pm if v == "K" else c.kalshi, round(c.score, 4), c.relation,
         int(c.confident), now) for v, c in out])
    db.commit()
    log.info("%d Novig candidates (%d with Kalshi) in %.0fs", len(out), sum(v == "K" for v, _ in out),
             time.monotonic() - t0)
    return out


def auto_approve(cfg: Config, db: sqlite3.Connection, cands: list[Candidate], k_series: dict[str, str]) -> int:
    """Approve candidates matched by an auto_approve rule, but only mutual best
    matches with a confidently inferred relation: the Polymarket market is the
    Kalshi market's top candidate, and the Kalshi market is the Polymarket market's
    top candidate for that relation (a moneyline pairs "same" with one team's
    Kalshi market and "inverse" with the other's)."""
    best_k: dict[str, Candidate] = {}
    best_p: dict[tuple[str, str], Candidate] = {}
    for c in cands:  # sorted by score desc
        best_k.setdefault(c.kalshi, c)
        best_p.setdefault((c.pm, c.relation), c)
    decided = {(r[0], r[1]) for r in db.execute("SELECT kalshi, pm FROM decisions")}
    n = 0
    for c in cands:
        if (c.kalshi, c.pm) in decided or not c.confident:
            continue
        if best_k[c.kalshi] is not c or best_p[(c.pm, c.relation)] is not c:
            continue
        for rule in cfg.auto_approve:
            if (k_series.get(c.kalshi) == rule.kalshi_series and c.pm.startswith(rule.pm_slug_prefix)
                    and c.score >= rule.min_score and (c.relation == "same" or rule.allow_inverse)):
                append_pair(cfg.pairs_path, c.kalshi, c.pm, c.relation, f"auto:{rule.kalshi_series}")
                record(db, c.kalshi, c.pm, c.relation, "rule")
                n += 1
                break
    db.commit()
    if n:
        log.info("auto-approved %d pairs into %s", n, cfg.pairs_path)
    return n
