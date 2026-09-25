import time

import pytest

from arbscan import match
from arbscan.catalog import ROW_SQL, kalshi_start_ts
from arbscan.config import Config
from arbscan.store import connect
from arbscan.venues import pm_sides


@pytest.mark.parametrize(
    "text,strikes,years",
    [
        ("Will Bitcoin be above $130,000 by Oct 1, 2026 at 10:00 AM ET?", {130000}, {2026}),
        ("When will Bitcoin hit $150k?", {150000}, set()),
        ("Kirk Cousins: 4+ passing touchdowns", {3.5}, set()),
        ("Over 7.5 runs scored", {7.5}, set()),
        ("Above 1.93M", {1930000}, set()),
        ("at least 58% of the vote", {58}, set()),
        ("scheduled for September 24, 2026 at 11:15 PM UTC", set(), {2026}),
        ("Hollywood Casino 400 Winner", {400}, set()),
    ],
)
def test_numbers(text, strikes, years):
    s, y = match.numbers(text)
    assert s == frozenset(strikes) and y == frozenset(years)


@pytest.mark.parametrize(
    "text,sports,expected",
    [
        ("LV Raiders vs NO Saints: 1st Half Total", True, {"half1", "total"}),
        ("Karmine Corp wins map 2", True, {"map2"}),
        ("Big Brother Season 28: 3rd Place", False, {"#3"}),
        ("#2 Global Netflix Show this week?", False, {"#2"}),
        ("Men's Pro Tennis No. 1 on Dec 31", False, {"#1"}),
        ("MI-04 House Election Margin of Victory", False, {"mi04"}),
        ("Cincinnati vs Atlanta: Team Total", True, {"teamtotal"}),
        ("Will the 5th inning of Rockies vs Diamondbacks end in a tie?", True, {"inning5", "draw"}),
        ("mlb sd lad 2026 09 24 i5 sd", True, {"inning5"}),
        ("Seattle wins first 7 innings", True, {"first7"}),
        ("mlb laa sea 2026 09 24 f5 sea", True, {"first5"}),
        ("Will the Wake Forest cover -10.5 vs the Louisville", True, {"spread"}),
        ("Wake Forest wins by over 10.5 points", True, {"spread"}),
        ("College Football Playoff Qualifiers", True, {"qualify"}),
        ("Final score Draw 2-2?", True, {"draw", "score2-2"}),
        ("San Diego vs Los Angeles D | San Diego wins", True, set()),
        ("totals/football_team_points_full_game_total", True, {"teamtotal"}),
    ],
)
def test_qualifiers(text, sports, expected):
    assert match.qualifiers(text, sports) == frozenset(expected)


def test_words_normalize_accents_and_months():
    assert match.words("Luiz Inácio Lula da Silva in September") == {"luiz", "inacio", "lula", "da", "silva", "sep"}


def test_line_sign():
    assert match.line_sign("-2.50 · Cincinnati Reds (cin)") == "neg"
    assert match.line_sign("+2.50 · Atlanta Braves (atl)") == "pos"
    assert match.line_sign("Pittsburgh -1.5 first 5 innings") == "neg"
    assert match.line_sign("Wake Forest wins by over 10.5 points") == "neg"
    assert match.line_sign("Over") == ""
    assert match.line_sign("Cincinnati Reds") == ""


def test_dates():
    assert match.dates("Before Dec 1, 2026") == {"dec1"}
    assert match.dates("By December 31, 2026") == {"dec31"}
    assert match.dates("Before November 2026") == frozenset()


def test_kalshi_start_from_ticker():
    assert kalshi_start_ts("KXMLBGAME-26SEP241915CINATL") == pytest.approx(1790291700)  # 23:15 UTC
    assert kalshi_start_ts("KXNCAAFGAME-26SEP26NDPUR") is None


def test_pm_sides_labels():
    m = {
        "slug": "aec-cfb-jmad-old-2026-09-26",
        "marketSides": [
            {"long": True, "description": "Dukes", "team": {"name": "Dukes", "safeName": "James Madison", "abbreviation": "jmad"}},
            {"long": False, "description": "Monarchs", "team": {"name": "Monarchs", "safeName": "Old Dominion", "abbreviation": "old"}},
        ],
    }
    assert pm_sides(m) == ("Dukes · James Madison (jmad)", "Monarchs · Old Dominion (old)")
    plain = {"slug": "aachc-cfb-sec-x-arcman", "title": "Arch Manning", "question": "Arch Manning",
             "marketSides": [{"long": True, "description": "Yes"}, {"long": False, "description": "No"}]}
    # Title equal to the question is not a usable subject; fall back to slug code.
    assert pm_sides(plain) == ("Yes · arcman", "No")


# --- end-to-end matching on a tiny synthetic catalog -------------------------------

START = 1790291700.0  # 2026-09-24 23:15 UTC


def _k(ticker, title, yes, rules, start=START, category="Sports"):
    series = ticker.split("-", 1)[0]
    return ("K", ticker, ticker.rsplit("-", 1)[0], series, category, title, yes, yes, None,
            start, start + 3 * 3600, rules, 0.07, 0.4, 0.42, 100.0, time.time())


def _p(slug, title, yes, no, rules, market_type, start=START, category="sports"):
    return ("P", slug, None, slug.split("-", 1)[0], category, title, yes, no, market_type,
            start, start + 14 * 86400, rules, 0.0695, 0.4, 0.41, None, time.time())


def build_catalog(path: str):
    d = connect(path)
    rows = [
        _k("KXMLBGAME-26SEP241915CINATL-CIN", "Cincinnati vs Atlanta | CIN vs ATL (Sep 24) | Cincinnati wins",
           "Cincinnati", "If Cincinnati wins the Cincinnati vs Atlanta professional baseball game, then the market resolves to Yes."),
        _k("KXMLBGAME-26SEP241915CINATL-ATL", "Cincinnati vs Atlanta | CIN vs ATL (Sep 24) | Atlanta wins",
           "Atlanta", "If Atlanta wins the Cincinnati vs Atlanta professional baseball game, then the market resolves to Yes."),
        _k("KXMLBSPREAD-26SEP241915CINATL-CIN2", "Cincinnati vs Atlanta: Spread | CIN vs ATL (Sep 24)",
           "Cincinnati wins by over 1.5 runs", "If Cincinnati wins by more than 1.5 runs, then the market resolves to Yes."),
        _k("KXNFLPASSTDS-26SEP241915CINPIT-CINJBURROW9-1", "Cincinnati vs Pittsburgh: Passing Touchdowns | Joe Burrow: 1+",
           "Joe Burrow: 1+", "If Joe Burrow records 1+ passing touchdowns in the game, then the market resolves to Yes."),
        _k("KXCONCACAFNLGAME-26SEP241915SURMTQ-SUR", "Suriname vs Martinique | SUR vs MTQ (Sep 24) | Suriname wins",
           "Suriname", "If Suriname wins the match, then the market resolves to Yes."),
    ]
    rows += [
        _p("aec-mlb-cin-atl-2026-09-24",
           "Who will win in the upcoming baseball event Cincinnati Reds vs Atlanta Braves scheduled for September 24, 2026?",
           "Cincinnati Reds (cin)", "Atlanta Braves (atl)",
           "This market will settle to the winner of the Cincinnati Reds vs Atlanta Braves MLB game.",
           "moneyline/baseball_team_full_game_winner"),
        _p("asc-mlb-cin-atl-2026-09-24-neg-1pt5",
           "Will the Cincinnati Reds cover -1.5 vs the Atlanta Braves in Cincinnati Reds vs Atlanta Braves?",
           "-1.50 · Cincinnati Reds (cin)", "+1.50 · Atlanta Braves (atl)",
           "Spread settles Yes if the Cincinnati Reds win by more than 1.5 runs.", "spreads/baseball_team_spread"),
        _p("astatc-nfl-cin-pit-2026-09-24-td-joebur-gte1", "Will Joe Burrow record 1+ touchdowns? | Joe Burrow 1+ touchdowns",
           "Yes · Joe Burrow 1+ touchdowns", "No",
           "This market will settle to Yes if Joe Burrow records at least 1 touchdowns (excluding passing touchdowns) in the game.",
           "props/football_player_touchdowns"),
        _p("atc-cnl-sur-mtq-2026-09-24-draw",
           "Will the CONCACAF Nations League match Suriname vs Martinique scheduled for Sep 24, 2026 end in a draw?",
           "Yes · draw", "No", "This market will settle to Yes if the match ends in a draw.",
           "drawable_outcome/soccer_draw"),
    ]
    d.executemany(ROW_SQL, rows)
    d.commit()
    return d


@pytest.fixture
def db(tmp_path):
    return build_catalog(str(tmp_path / "t.db"))


def test_match_end_to_end(db, tmp_path):
    cfg = Config(pairs_path=str(tmp_path / "pairs.csv"), match_min_score=0.2)
    got = {(c.kalshi, c.pm): c.relation for c in match.run(cfg, db)}
    assert got[("KXMLBGAME-26SEP241915CINATL-CIN", "aec-mlb-cin-atl-2026-09-24")] == "same"
    assert got[("KXMLBGAME-26SEP241915CINATL-ATL", "aec-mlb-cin-atl-2026-09-24")] == "inverse"
    assert got[("KXMLBSPREAD-26SEP241915CINATL-CIN2", "asc-mlb-cin-atl-2026-09-24-neg-1pt5")] == "same"
    # A moneyline is never paired with a spread, and vice versa.
    assert ("KXMLBGAME-26SEP241915CINATL-CIN", "asc-mlb-cin-atl-2026-09-24-neg-1pt5") not in got
    assert ("KXMLBSPREAD-26SEP241915CINATL-CIN2", "aec-mlb-cin-atl-2026-09-24") not in got
    # "passing TDs" vs "TDs excluding passing" look alike but are different bets.
    assert not any(k.startswith("KXNFLPASSTDS") for k, _ in got)
    # A team-win market is not a draw market.
    assert not any(p.endswith("-draw") for _, p in got)


def test_auto_approve(db, tmp_path):
    from arbscan.config import AutoApproveRule
    from arbscan.pairs import load_pairs

    pairs = tmp_path / "pairs.csv"
    rule = AutoApproveRule(kalshi_series="KXMLBGAME", pm_slug_prefix="aec-mlb-", min_score=0.2, allow_inverse=False)
    cfg = Config(pairs_path=str(pairs), match_min_score=0.2, auto_approve=(rule,))
    match.run(cfg, db)
    got = {(p.kalshi, p.pm, p.relation) for p in load_pairs(str(pairs))}
    assert got == {("KXMLBGAME-26SEP241915CINATL-CIN", "aec-mlb-cin-atl-2026-09-24", "same")}
    # Decisions are remembered, so a second run adds nothing.
    match.run(cfg, db)
    assert len(load_pairs(str(pairs))) == 1
