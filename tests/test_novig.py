"""Novig: YES/NO orientation, labels, book conversion, and matching against the other venues."""

import time

import pytest

from arbscan import match, novig
from arbscan.catalog import ROW_SQL
from arbscan.config import Config
from arbscan.store import connect

START_MS = 1790528400000  # 2026-09-27 17:00 UTC
EVENT = {"eventId": "e-1", "description": "New York Jets @ Detroit Lions", "league": "NFL",
         "status": "OPEN_PREGAME", "startsTs": START_MS}
GAME_FEE = {"coefficient": "0.03", "makerCredit": "0.5", "charged": "WHEN_LIVE"}


def market(mid, kind, description, outcomes, strike="0"):
    return {"marketId": mid, "eventId": "e-1", "marketType": kind, "description": description, "strike": strike,
            "status": "OPEN", "voids": "FMV", "startsTs": START_MS, "fee": GAME_FEE,
            "outcomes": [{"outcomeId": f"{mid}-{i}", "name": n, "status": "TBD"} for i, n in enumerate(outcomes)]}


def test_yes_is_what_the_market_is_about():
    assert novig.orient(market("m", "SPREAD", "DET -7.5", ["NYJ +7.5", "DET -7.5"]))[0]["name"] == "DET -7.5"
    assert novig.orient(market("m", "TOTAL", "NYJ @ DET t45.5", ["Under 45.5", "Over 45.5"]))[0]["name"] == "Over 45.5"
    assert novig.orient(market("m", "MONEY", "OAK", ["HOU", "OAK"]))[0]["name"] == "OAK"
    assert novig.orient(market("m", "VICTORY_BY_DECISION", "S. Dumas VICTORY_BY_DECISION", ["No", "Yes"]))[0]["name"] \
        == "Yes"
    assert novig.orient(market("m", "MONEY", "?", ["A", "B", "C"])) is None


def test_team_codes_and_initials_find_their_names():
    names = ("New York Jets", "Detroit Lions")
    assert novig.name_for("NYJ", names) == "New York Jets"
    assert novig.name_for("DET", names) == "Detroit Lions"
    assert novig.name_for("LAD", ("Los Angeles Dodgers", "Los Angeles Angels")) == "Los Angeles Dodgers"
    assert novig.name_for("XYZ", names) is None
    assert novig._label("H. Gaston", ("Andrey Rublev", "Hugo Gaston")) == "H. Gaston · Hugo Gaston"
    assert novig._label("Over 43.5", names, "Adonai Mitchell") == "Over 43.5 · Adonai Mitchell"


def test_rows_skip_futures_and_many_way_markets():
    now = time.time()
    rec, outcomes = novig.row(EVENT, market("m-1", "RECEIVING_YARDS", "Adonai Mitchell 43.5 RECEIVING_YARDS",
                                            ["Over 43.5", "Under 43.5"], "43.5"), now)
    assert rec[0] == "N" and rec[3] == "NFL" and rec[9] == START_MS / 1000
    assert rec[5] == "New York Jets @ Detroit Lions (NFL, Sep 27) | receiving yards | " \
                     "Adonai Mitchell: receiving yards over 43.5"
    assert (rec[6], rec[7]) == ("Over 43.5 · Adonai Mitchell", "Under 43.5 · Adonai Mitchell")
    assert outcomes == ("m-1", "e-1", "m-1-0", "m-1-1", 1)
    future = {**EVENT, "description": "MVP Winner"}
    assert novig.row(future, market("m-2", "MVP_WINNER", "Jared Goff MVP_WINNER", ["Yes", "No"]), now) is None
    assert novig.row(EVENT, market("m-3", "FIRST_TOUCHDOWN_SCORER", "x", ["Yes", "No"]), now) is None


def test_ladders_are_the_other_outcomes_bids_in_dollar_contracts():
    book = {"orders": {"yes": [{"price": "0.44", "qty": 32870}, {"price": "0.43", "qty": 1000}],
                       "no": [{"price": "0.54", "qty": 1800}, {"price": "0.54", "qty": 200}]}}
    yes_asks, no_asks = novig.ladders(book, "yes", "no")
    assert yes_asks == [(0.46, 20.0)]  # NO bids at 54c are YES offers at 46c; 2,000 cent-contracts = $20
    assert no_asks == [(0.56, 328.7), (0.57, 10.0)]


def test_extra_qualifiers_tell_look_alike_bets_apart():
    assert match.extra_quals("spreads/tennis_match_games_spread") == {"games"}
    assert match.extra_quals("Halys vs Safiullin: Game Spread") == {"games"}
    assert match.extra_quals("spreads/football_team_full_game_spread") == frozenset()
    assert match.extra_quals("Rublev @ Gaston | set spread") == {"sets"}
    assert match.extra_quals("S. Dumas: victory by submission") == {"method"}
    assert match.extra_quals("props/soccer_game_first_team_to_score") == {"firstscore"}
    assert match.extra_quals("both teams to score") == {"btts"}


def _k(ticker, title, yes, rules, start):
    return ("K", ticker, ticker.rsplit("-", 1)[0], ticker.split("-", 1)[0], "Sports", title, yes, yes, None,
            start, start + 3 * 3600, rules, 0.07, 0.4, 0.42, 100.0, time.time())


def _p(slug, title, yes, no, rules, market_type, start):
    return ("P", slug, None, slug.split("-", 1)[0], "sports", title, yes, no, market_type,
            start, start + 14 * 86400, rules, 0.0695, 0.4, 0.41, None, time.time())


@pytest.fixture
def db(tmp_path):
    d = connect(str(tmp_path / "n.db"))
    now, start = time.time(), START_MS / 1000
    markets = [
        market("n-ml", "MONEY", "DET", ["NYJ", "DET"]),
        market("n-sp", "SPREAD", "DET -7.5", ["DET -7.5", "NYJ +7.5"], "-7.5"),
        market("n-rec", "RECEIVING_YARDS", "Adonai Mitchell 49.5 RECEIVING_YARDS", ["Over 49.5", "Under 49.5"], "49.5"),
    ]
    for m in markets:
        rec, outcomes = novig.row(EVENT, m, now)
        d.execute(ROW_SQL, rec)
        d.execute("INSERT INTO novig_outcomes VALUES (?,?,?,?,?)", outcomes)
    d.executemany(ROW_SQL, [
        _k("KXNFLGAME-26SEP27NYJDET-NYJ", "New York J vs Detroit | NYJ vs DET (Sep 27) | New York J wins", "New York J",
           "If New York J wins the New York J vs Detroit professional football game, then the market resolves to Yes.",
           start),
        _k("KXNFLSPREAD-26SEP27NYJDET-DET8", "NY Jets vs DET Lions: Spread | NYJ vs DET (Sep 27) | "
           "DET Lions wins by over 7.5 points?", "DET Lions wins by over 7.5 points",
           "If DET Lions wins by over 7.5 points, then the market resolves to Yes.", start),
        _k("KXNFLRECYDS-26SEP27NYJDET-NYJAMITCHELL-50", "New York J vs Detroit: Receiving Yards | NYJ vs DET (Sep 27) | "
           "Adonai Mitchell: 50+ receiving yards", "Adonai Mitchell: 50+",
           "If Adonai Mitchell records 50+ receiving yards, then the market resolves to Yes.", start),
        _k("KXNFLRECYDS-26SEP27NYJDET-NYJAMITCHELL-40", "New York J vs Detroit: Receiving Yards | NYJ vs DET (Sep 27) | "
           "Adonai Mitchell: 40+ receiving yards", "Adonai Mitchell: 40+",
           "If Adonai Mitchell records 40+ receiving yards, then the market resolves to Yes.", start),
        _p("aec-nfl-nyj-det-2026-09-27", "Who will win in the upcoming football event New York Jets vs Detroit Lions?",
           "Jets · New York Jets · NY Jets (nyj)", "Lions · Detroit Lions · DET Lions (det)",
           "This market will settle to the winner of the New York Jets vs Detroit Lions NFL game.",
           "moneyline/football_team_full_game_winner", start),
    ])
    d.commit()
    return d


def test_matching_novig_against_both_venues(db):
    found = {(v, c.kalshi if v == "K" else c.pm, c.pm if v == "K" else c.kalshi): c.relation
             for v, c in match.run_novig(Config(), db)}
    assert found[("K", "KXNFLGAME-26SEP27NYJDET-NYJ", "n-ml")] == "inverse"  # Novig's YES is Detroit
    assert found[("K", "KXNFLSPREAD-26SEP27NYJDET-DET8", "n-sp")] == "same"
    assert found[("K", "KXNFLRECYDS-26SEP27NYJDET-NYJAMITCHELL-50", "n-rec")] == "same"  # 50+ is over 49.5
    assert ("K", "KXNFLRECYDS-26SEP27NYJDET-NYJAMITCHELL-40", "n-rec") not in found  # 40+ is another line
    assert found[("P", "aec-nfl-nyj-det-2026-09-27", "n-ml")] == "inverse"  # Polymarket's YES is the Jets
    assert db.execute("SELECT COUNT(*) FROM novig_candidates").fetchone()[0] == len(found)
