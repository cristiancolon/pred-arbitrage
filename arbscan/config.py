"""Settings, loaded from an optional TOML file on top of built-in defaults."""

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path


@dataclass(frozen=True)
class AutoApproveRule:
    """Auto-approve matcher candidates between a Kalshi series and a Polymarket US
    slug prefix, for templated markets (e.g. MLB game winners) whose rules you have
    already compared once by hand."""

    kalshi_series: str
    pm_slug_prefix: str
    min_score: float = 0.6
    allow_inverse: bool = True


@dataclass(frozen=True)
class Config:
    db_path: str = "data/arbscan.db"
    pairs_path: str = "pairs.csv"

    kalshi_base: str = "https://external-api.kalshi.com/trade-api/v2"
    pmus_base: str = "https://gateway.polymarket.us"
    # Novig (novig.py): a sports exchange, read through its public catalog. Off: it
    # added ~50 s to each hourly refresh (catalog 23 s, matching 26 s), and API keys
    # aren't self-serve. True puts it back in the refresh; its public routes are
    # throttled per IP address, so they're read gently.
    novig: bool = False
    novig_base: str = "https://api.novig.com"
    novig_rps: float = 2.0
    # REST requests per second (metadata, and polling when not streaming). Both public
    # APIs sustained 20/s in testing; Polymarket US documents 20/s per IP.
    kalshi_rps: float = 15.0
    pmus_rps: float = 15.0

    # Streaming (live.py): with API keys for both venues the scanner prices pairs from
    # WebSocket order books the moment they change instead of polling. The Kalshi key
    # is a Key ID plus a private key file; the Polymarket US key is a Key ID plus the
    # base64 secret from polymarket.us/developer.
    kalshi_key_id: str = ""
    kalshi_private_key_path: str = ""
    pmus_key_id: str = ""
    pmus_secret_key: str = ""
    kalshi_ws_url: str = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
    pmus_ws_url: str = "wss://api.polymarket.us/v1/ws/markets"
    # Polymarket US takes orders on this host; pmus_base is its public, read-only API.
    pmus_trade_base: str = "https://api.polymarket.us"

    # Polling (no keys): start a new sweep this long after the previous one started.
    poll_interval_s: float = 1.0
    # Refresh market status / close times / fee params this often.
    meta_refresh_s: float = 300.0
    # Fetch Polymarket US depth for a pair once its top-of-book net edge exceeds this ($).
    depth_trigger_edge: float = 0.0
    # Only take contract pairs whose marginal profit after fees exceeds this ($).
    min_edge: float = 0.0
    # Your total bankroll ($), held half on each venue since each leg is paid for there.
    # Every window is sized to what that buys; 0 means unlimited (full book depth).
    bankroll_usd: float = 300.0
    # Which windows are worth taking ("picks"; see arbscan/bankroll.py). Money is tied
    # up until a market resolves, so windows are ranked by return per year of lock-up,
    # and a pick must be open at least pick_min_window_s, resolve within pick_max_days,
    # return at least pick_min_annualized_return a year (1.0 = 100%), and have an edge
    # no bigger than pick_max_edge per $1 pair (bigger usually means a rules mismatch
    # or a stale quote). 0 turns off the days or edge limit.
    pick_min_window_s: float = 1.0
    pick_max_days: float = 7.0
    pick_min_annualized_return: float = 1.0
    pick_max_edge: float = 0.05
    # One pick may use at most this share of the money on a venue if it resolves within
    # a day, and proportionally less the longer it ties the money up (a quarter of it at
    # 2 days), so a multi-day pick can't starve the quick ones.
    pick_max_stake: float = 0.5
    # Paper trading (streaming mode): act on picks as a live bot on this machine would,
    # filling each leg against the live book when its order would have arrived, with
    # fees and latency, but never placing an order (see arbscan/paper.py). Skips picks
    # expected to make less than paper_min_profit_usd. Order latency is re-measured
    # every latency_probe_s with read-only requests (see arbscan/latency.py).
    paper_trading: bool = True
    paper_min_profit_usd: float = 0.01
    # Send one leg first and the other only for what it filled: "auto" leads with the
    # leg whose price moved most recently (the likelier to move again, where a miss
    # costs nothing), "P" or "K" always lead with that venue, "" sends both at once.
    paper_lead_venue: str = "auto"
    # Trade only once both legs' best prices have held still this long: a quote that
    # just moved tends to keep moving.
    paper_quiet_s: float = 2.0
    latency_probe_s: float = 15.0
    # Dry run (dryrun.py): a second paper trader with the limits of a first, capped live
    # run, in its own account of live_bankroll_usd (half on each venue). Every leg it
    # simulates is also written out as the real order a live trader would send
    # (live_orders), and Polymarket US checks each buy with its order preview. Nothing
    # is sent. A pick must come from a series with at least live_series_min_settled
    # settled pairs, none conflicting and at most live_series_max_void voided; one
    # trade costs at most live_max_stake_frac of the account's money (cash plus open
    # trades, both venues together, so it grows with the account) and must make
    # live_min_profit_usd; no new trade once today's trades have lost
    # live_daily_loss_usd, net of today's gains, across both venues together.
    dry_run: bool = True
    live_bankroll_usd: float = 300.0
    live_max_stake_frac: float = 0.10
    live_min_profit_usd: float = 0.05
    live_daily_loss_usd: float = 10.0
    live_series_min_settled: int = 20
    live_series_max_void: float = 0.02
    # Live trading (livetrade.py): the dry run's picks traded with real orders, under the
    # same live_* limits, from a separate account (``live_trades``). Off unless switched on
    # here; it needs paper_trading and both venues' API keys. One trade at a time, at most
    # live_max_trades_per_day a day. A leg left over is sold back no lower than
    # live_unwind_max_loss under what it cost; if that can't be done, or an order's outcome
    # can't be read back, trading stops until `arbscan live-resume`.
    live_trading: bool = False
    live_unwind_max_loss: float = 0.10
    live_max_trades_per_day: int = 50
    # Polymarket US volume rebate on taker fees (0.10 = 10%), if you qualify.
    pmus_taker_rebate: float = 0.0
    book_levels_stored: int = 10

    # `arbscan serve`: dashboard address, optional shared-secret token, and how
    # often the background job refreshes the catalog and match suggestions.
    web_host: str = "0.0.0.0"
    web_port: int = 8787
    web_token: str = ""
    refresh_interval_h: float = 1.0

    # Storage (retention.py): every hour `arbscan serve` deletes raw streaming history
    # (quotes, opportunities, sweeps) older than keep_raw_days, and older hours too if the
    # database would otherwise pass max_db_gb (never the last day). Windows, trades,
    # orders, results and the catalog are always kept. A week of raw history was ~20 GB
    # in late September 2026. 0 turns either limit off.
    keep_raw_days: float = 7.0
    max_db_gb: float = 25.0

    # Live discovery (discover.py): between full refreshes, check both venues for newly
    # listed markets this often and match, review and pair them straight away.
    discovery: bool = True
    kalshi_discovery_s: float = 15.0
    pmus_discovery_s: float = 30.0

    catalog_horizon_days: int = 120
    match_min_score: float = 0.45
    auto_approve: tuple[AutoApproveRule, ...] = field(default_factory=tuple)

    # Automatic review with TypeSafe's Jev model (jev.py). Enabled when an API key is
    # set here or in the TYPESAFE_API_KEY environment variable. The model is pinned
    # because jev.py's thresholds were tuned against it.
    jev_api_key: str = ""
    jev_model: str = "jev-1.13.0"
    jev_rps: float = 8.0
    jev_max_per_run: int = 10000

    @property
    def leg_budget(self) -> float | None:
        """Cash available per venue, for sizing each leg."""
        return self.bankroll_usd / 2 if self.bankroll_usd > 0 else None

    @property
    def can_stream(self) -> bool:
        return bool(self.kalshi_key_id and self.kalshi_private_key_path and self.pmus_key_id and self.pmus_secret_key)


def load(path: str | None) -> Config:
    if path is None:
        default = Path("config.toml")
        if not default.exists():
            return Config()
        path = str(default)
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    known = {f.name for f in fields(Config)}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"unknown config keys in {path}: {', '.join(sorted(unknown))}")
    rule_keys = {f.name for f in fields(AutoApproveRule)}
    rules = []
    for r in raw.pop("auto_approve", []):
        stray = set(r) - rule_keys
        if stray & known:
            raise ValueError(f"{path}: {', '.join(sorted(stray & known))} must come before the first "
                             "[[auto_approve]] table (TOML puts keys after a table header inside that table)")
        if stray:
            raise ValueError(f"unknown auto_approve keys in {path}: {', '.join(sorted(stray))}")
        rules.append(AutoApproveRule(**r))
    return Config(**raw, auto_approve=tuple(rules))
