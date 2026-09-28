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


# --- signing (NOVIG-V3), against Novig's published vectors -------------------------

def test_signer_matches_every_published_vector():
    # tests/fixtures/novig-signing-vectors.json is https://docs.novig.com/api-reference/spec-files/signing-vectors.json
    import base64
    import json
    from pathlib import Path

    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    from arbscan.auth import NovigSigner, novig_string

    spec = json.loads((Path(__file__).parent / "fixtures" / "novig-signing-vectors.json").read_text())
    assert len(spec["vectors"]) == 30
    for v in spec["vectors"]:
        i, pair = v["input"], spec["keypairs"][v["keypair_id"]]
        text = novig_string(str(i["timestamp"]), i["method"], i["path"], i["query"], i["body"].encode())
        assert text == v["string_to_sign"], v["id"]
        signer = NovigSigner("id", pair["private_key_pkcs8_pem"].encode())
        if v["algorithm"] == "ed25519":
            assert signer.sign(text) == v["signature"], v["id"]  # Ed25519 is deterministic
        else:  # ECDSA isn't: check both ours and theirs verify
            public = serialization.load_pem_public_key(pair["public_key_spki_pem"].encode())
            for sig in (signer.sign(text), v["signature"]):
                public.verify(base64.b64decode(sig), text.encode(), ec.ECDSA(hashes.SHA256()))


def test_issuing_a_read_key(tmp_path):
    import asyncio
    import base64
    import json

    import httpx
    from cryptography.hazmat.primitives import serialization

    from arbscan.auth import NovigSigner, novig_string

    mgmt_pem, mgmt_public = novig.new_keypair()
    read_pem, read_public = novig.new_keypair()
    verify = serialization.load_pem_public_key(mgmt_public.encode())
    subaccounts, calls = [], []

    def server(request: httpx.Request) -> httpx.Response:
        h, body = request.headers, request.content
        text = novig_string(h["Novig-Timestamp"], request.method, request.url.raw_path.decode().split("?")[0],
                            request.url.query.decode(), body)
        verify.verify(base64.b64decode(h["Novig-Signature"]), text.encode())  # raises if it doesn't
        assert h["Novig-Key-Id"] == "mgmt-1"
        calls.append((request.method, request.url.path))
        if request.url.path == "/v3/echo":
            return httpx.Response(200, content=body)
        if request.url.path == "/v3/account/subaccounts" and request.method == "GET":
            return httpx.Response(200, json=subaccounts)
        if request.url.path == "/v3/account/subaccounts":
            opened = json.loads(body)
            assert (tmp_path / "trading.pem").stat().st_mode & 0o777 == 0o600  # saved before opening
            subaccounts.append({"keyId": "sub-1", "label": opened["label"], "balance": "0.00000"})
            return httpx.Response(201, json={"keyId": "sub-1", "label": opened["label"], "balance": "0.00000"})
        made = json.loads(body)
        assert request.url.path == "/v3/account/subaccounts/sub-1/keys"
        assert made == {"name": "arbscan-read", "publicKey": read_public, "algorithm": "Ed25519",
                        "scope": "trading::read"}
        return httpx.Response(201, json={"keyId": f"read-{len(calls)}", "fingerprint": "sha256:x"})

    async def issue():
        async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
            account = novig.NovigAccount(client, "https://api.novig.test", NovigSigner("mgmt-1", mgmt_pem))
            return await novig.issue_read_key(account, read_public, "arbscan", tmp_path / "trading.pem", log=lambda _: None)

    assert asyncio.run(issue()) == {"subaccount": "sub-1", "key_id": "read-4", "fingerprint": "sha256:x"}
    assert calls == [("POST", "/v3/echo"), ("GET", "/v3/account/subaccounts"), ("POST", "/v3/account/subaccounts"),
                     ("POST", "/v3/account/subaccounts/sub-1/keys")]
    # A second run reuses the subaccount and leaves its trading key alone.
    before = (tmp_path / "trading.pem").read_bytes()
    assert asyncio.run(issue())["subaccount"] == "sub-1"
    assert calls[4:] == [("POST", "/v3/echo"), ("GET", "/v3/account/subaccounts"),
                         ("POST", "/v3/account/subaccounts/sub-1/keys")]
    assert (tmp_path / "trading.pem").read_bytes() == before


# --- the live stream ----------------------------------------------------------------

def _book_snapshot():
    return {"seq": 10, "orders": {
        "yes": [{"order": "a", "price": "0.44", "qty": 1000}],
        "no": [{"order": "b", "price": "0.54", "qty": 2000}, {"order": "c", "price": "0.53", "qty": 500}]}}


def test_a_novig_book_follows_its_snapshot_and_deltas():
    from arbscan.feeds import NovigBook

    b = NovigBook()
    b.snapshot(_book_snapshot(), {"seq": 3, "status": "OPEN"}, 1.0, 0.9)
    assert b.ready and b.open and not b.live
    # YES is offered at 1 - the NO bids, in $1 contracts (100 of Novig's 1-cent ones)
    assert b.ladders("yes", "no") == ([(0.46, 20.0), (0.47, 5.0)], [(0.56, 10.0)])
    # A partial fill arrives as a remove, then an add of what's left at the same price.
    assert b.delta({"seq": 11, "deltas": [{"kind": "remove", "order": "b", "reason": "fill"},
                                          {"kind": "add", "order": "b", "outcome": "no", "price": "0.54", "qty": 1200}]},
                   None, 2.0, 1.9)
    assert b.ladders("yes", "no")[0] == [(0.46, 12.0), (0.47, 5.0)]
    assert b.delta(None, {"seq": 4, "deltas": ["GOLIVE"]}, 3.0, 2.9) and b.live  # taker fees on
    assert b.delta(None, {"seq": 5, "deltas": ["UNLIVE", "CLOSE"]}, 3.1, 3.0) and not b.live and not b.open
    # A skipped seq: the book can't be trusted until a fresh snapshot.
    assert not b.delta({"seq": 13, "deltas": []}, None, 4.0, 3.9) and not b.ready


def test_the_novig_feed_resyncs_a_gap_and_paces_its_subscriptions():
    import asyncio

    from arbscan.auth import NovigSigner
    from arbscan.feeds import NovigFeed, TokenBucket

    updates, frames = [], []
    feed = NovigFeed("wss://api.novig.test/v3/ws", NovigSigner("read-1", novig.new_keypair()[0]), updates.append)

    class FakeWS:
        async def send(self, text):
            frames.append(__import__("json").loads(text))

    async def run():
        feed.ws, feed.bucket = FakeWS(), TokenBucket(512, 1e9)  # an instant refill for the test
        feed.set_markets(["m2", "m1"])
        await feed._sync()
        feed._handle({"ts": 1757894400123, "nonce": 1, "snapshot": {
            "m1": {"eventId": "e", "book": _book_snapshot(), "lifecycle": {"seq": 1, "status": "OPEN"}}}}, 1757894400.2)
        feed._handle({"ts": 1757894400456, "delta": {"m1": {"eventId": "e", "book": {"seq": 12, "deltas": []}}}},
                     1757894400.5)  # 11 went missing
        assert feed.resnap == {"m1"} and not feed.books["m1"].ready
        await feed._sync()
        feed.set_markets(["m2"])
        await feed._sync()

    asyncio.run(run())
    assert updates == ["m1", "m1"]
    assert frames == [{"nonce": 1, "subscribe": {"markets": {"m1": "book", "m2": "book"}}},
                      {"nonce": 2, "snapshot": {"markets": {"m1": "book"}}},
                      {"nonce": 3, "unsubscribe": ["market:m1"]}]
    feed._handle({"code": "SUBSCRIPTION_LIMIT_EXCEEDED", "message": "too many", "nonce": 4}, 1.0)
    assert feed.errors[-1]["code"] == "SUBSCRIPTION_LIMIT_EXCEEDED"
    # A request over the bucket's capacity costs the capacity, and waits for a full bucket.
    bucket = TokenBucket(512, 4)
    bucket.spend(16 * 1000)
    assert bucket.tokens == 0 and bucket.wait_s(16 * 1000) == pytest.approx(128, rel=0.01)


def test_a_novig_pair_is_priced_against_the_other_venues_live_book(tmp_path):
    from types import SimpleNamespace

    from arbscan.auth import NovigSigner
    from arbscan.feeds import KalshiBook, NovigBook, NovigFeed
    from arbscan.novig_live import NovigLink

    db = connect(str(tmp_path / "l.db"))
    kb = KalshiBook()
    kb.snapshot({"yes_dollars_fp": [["0.50", "100"]], "no_dollars_fp": [["0.40", "100"]]}, 1.0)  # Kalshi NO at 50c
    km = SimpleNamespace(status="active", fee_coef=0.07, resolve_ts=None)
    scanner = SimpleNamespace(cfg=Config(bankroll_usd=300), kmeta={"K-1": km}, kfeed=SimpleNamespace(books={"K-1": kb}),
                              pfeed=SimpleNamespace(books={}), pm_coef={}, pm_closed=set(), out=db, db=db)
    feed = NovigFeed("wss://x", NovigSigner("read-1", novig.new_keypair()[0]), lambda _: None)
    link = NovigLink(scanner, feed)
    pair = {"id": "K:K-1|n-1", "venue": "K", "other": "K-1", "novig": "n-1", "relation": "same", "yes_outcome": "yes",
            "no_outcome": "no", "n_coef": 0.03, "fee_when_live": 1, "start_ts": time.time() + 3600, "close_ts": None}
    link.by_novig["n-1"], link.by_other[("K", "K-1")] = [pair], [pair]
    nb = feed.books["n-1"] = NovigBook()
    nb.snapshot(_book_snapshot(), {"seq": 1, "status": "OPEN"}, 1.0, 0.9)

    link.on_novig("n-1")  # Novig YES at 46c (free before the game) + Kalshi NO at 50c (1.75c fee)
    ep = link.windows.open[("K:K-1|n-1", "N:YES+O:NO")]
    assert ep.max_top_edge == pytest.approx(1 - 0.46 - 0.50 - 0.07 * 0.25)
    assert ("K:K-1|n-1", "N:NO+O:YES") not in link.windows.open
    nb.live = True  # the game went live: Novig's taker fee is on
    link.on_other("K", "K-1")
    assert link.windows.open[("K:K-1|n-1", "N:YES+O:NO")].edge == pytest.approx(0.0225 - 0.03 * 0.46 * 0.54)
    km.status = "inactive"  # Kalshi paused it: the window closes and is saved
    link.on_other("K", "K-1")
    assert not link.windows.open
    assert [tuple(r) for r in db.execute("SELECT pair, direction, n_obs FROM novig_windows")] == \
        [("K:K-1|n-1", "N:YES+O:NO", 2)]
