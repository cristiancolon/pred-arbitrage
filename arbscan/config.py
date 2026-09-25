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

    # Polling (no keys): start a new sweep this long after the previous one started.
    poll_interval_s: float = 1.0
    # Refresh market status / close times / fee params this often.
    meta_refresh_s: float = 300.0
    # Fetch Polymarket US depth for a pair once its top-of-book net edge exceeds this ($).
    depth_trigger_edge: float = 0.0
    # Only take contract pairs whose marginal profit after fees exceeds this ($).
    min_edge: float = 0.0
    # Polymarket US volume rebate on taker fees (0.10 = 10%), if you qualify.
    pmus_taker_rebate: float = 0.0
    book_levels_stored: int = 10

    # `arbscan serve`: dashboard address, optional shared-secret token, and how
    # often the background job refreshes the catalog and match suggestions.
    web_host: str = "0.0.0.0"
    web_port: int = 8787
    web_token: str = ""
    refresh_interval_h: float = 1.0

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
