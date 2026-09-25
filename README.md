# arbscan

A read-only scanner that measures how much cross-venue arbitrage actually exists
between **Kalshi** and **Polymarket US**. It never places orders and needs no exchange
API keys: both venues publish market data publicly. An optional TypeSafe API key lets
the Jev model review suggested pairs for you.

The point is to answer "is there money here?" with data before writing any trading code.

## How it works

```
catalog  ->  match  ->  review  ->  scan  ->  report
```

1. **catalog** downloads every open market on both venues (~90k Kalshi, ~60k
   Polymarket US) into SQLite. It takes about 1.5 minutes and peaks around 200 MB.
2. **match** suggests equivalent market pairs. It scores word similarity, then applies
   hard filters. Strikes, dates, game start times and teams must agree. So must the
   qualifiers: 1st half vs full game, map 2, top 20, qualify vs win, draw markets, and
   spread signs. It also works out whether Polymarket's YES is Kalshi's YES (**same**)
   or Kalshi's NO (**inverse**, e.g. Kalshi "Braves win" vs a Reds/Braves moneyline
   whose long side is the Reds).
3. **review** decides which suggestions are really the same bet. With a TypeSafe key,
   Jev reads both rulebooks for each one: it approves clear matches, rejects clear
   mismatches, and leaves the rest in a queue that shows both rulebooks side by side
   for you to decide. Approved pairs go to `pairs.csv`.
4. **scan** watches the approved pairs. With API keys for both venues it streams
   their order books over WebSockets and re-prices a pair the moment either side
   changes; without keys it polls them every second or so (see [Latency](#latency)).
   For each pair it checks both directions (e.g. buy Kalshi YES + Polymarket NO) and
   computes the edge after both taker fees. When that edge is positive it walks both
   order books. The result is how many contracts are profitable and for how many
   dollars.
5. **report** summarizes profitable windows: how often, how big, how long they last,
   and the return on the capital they tie up.

### Fees modelled

Both venues charge takers `coef × contracts × p × (1 − p)`:

- **Kalshi:** `0.07 × series multiplier`. The multiplier is read per series; open
  series currently use 1, 0.5 or 0.
- **Polymarket US:** the per-market `feeCoefficient` (0.0695 today). Optionally
  reduced by your volume rebate via `pmus_taker_rebate`.

At 50¢ that is ~1.75¢ per contract on each venue, so a pair needs a gap of ~3.5¢ just
to break even.

### Sizing to your bankroll

Opportunities are sized to `bankroll_usd` ($500 by default), assumed split evenly
across the two venues because each leg is paid for on its own venue. A window's size,
capital and profit are what $250 on each side could buy at that moment. Summing
windows would still assume a fresh bankroll for each one, so the Overview also
**simulates one bankroll**: windows are taken in the order they appeared, each stake
stays tied up until its market resolves, and windows are skipped when they closed
within `sim_min_window_s` (1 s), return less than `sim_min_annualized_return` (10%
a year), or show an edge over `sim_max_edge` (5¢ per $1 pair, which in practice has
meant a rules mismatch or a stale quote, e.g. a suspended in-game market). Changing the bankroll re-sizes the recorded history at the next start, from
the order books stored with each observation (`arbscan rescale` does it by hand).

### What the numbers do *not* include

- **Execution risk.** The two legs can't be filled atomically. By the time you act,
  prices may have moved.
- **Windows shorter than the polling interval**, when polling (~1–3 s). Most of those belong to
  faster bots anyway.
- **Capital lockup.** The report shows days until resolution and an annualized return
  so you can compare it with just holding cash.
- **Settlement mismatch.** If two paired markets resolve differently, you lose both
  legs. Review is the defence. Treat any gap that's large (>5¢) or lasts for hours as
  a sign the markets aren't really equivalent, not as free money. The report flags these.

## The dashboard

`arbscan serve` runs everything in one process: the scanner, the scheduled refresh
(catalog, match and Jev review, every hour), and a web dashboard at `http://<pi>:8787/`.
Nothing needs a button press: new markets flow through to the scanner on their own.

- **Overview.** The five pipeline stages with live status. A chart of how close the
  best watched pair got to breakeven over time. Open opportunities and a
  closest-to-breakeven leaderboard, both updated after every sweep. Best-case profit
  by hour.
- **Pairs.** Every watched pair with live prices and the net edge in both
  directions. Click a pair for its edge history, past windows and both rulebooks,
  or to stop watching it.
- **Review.** The review queue in the browser. Each candidate shows a diagram of
  which outcome on one venue matches which on the other, plus both rulebooks side by
  side with numbers, dates and settlement wording (draws, postponement, exclusions)
  highlighted. Keys: `S` same, `I` inverse, `R` reject, `J`/`K` next/previous. With
  Jev on, each pair it couldn't decide shows why, and a "Rejected by Jev" view lets
  you spot-check its rejections and overrule one by approving it.
- **Opportunities.** Every profitable window: how long it lasted, how big the edge
  got, the capital it needed, and a flag on the ones that look like a rules
  mismatch.
- **Refresh job.** Run the refresh on demand and watch its log stream live.

Updates are pushed over server-sent events, so the page stays current without
reloading. It works on a phone, and has light and dark themes (it follows your
system setting by default). Every chart has a table view.

The dashboard listens on all interfaces so you can open it from other devices on
your network. Set `web_host = "127.0.0.1"` to keep it local to the Pi, or set
`web_token` to require opening it once with `?token=...`. It never places orders,
but anyone who can reach it can approve or remove pairs.

## Setup on the Raspberry Pi

Needs Python 3.11+ (Raspberry Pi OS Bookworm ships 3.11). Clone the repo anywhere
on the Pi, then:

```sh
git clone https://github.com/cristiancolon/pred-arbitrage
cd pred-arbitrage
cp config.example.toml config.toml     # optional; defaults are fine
sh deploy/install.sh                   # venv + systemd user service + starts it
```

The service points at wherever the repo lives. If you move it, re-run
`deploy/install.sh`.

Then open `http://<pi-address>:8787/`. On first start the service downloads the
market catalog and runs matching by itself; this takes about two minutes, and you
can watch it on the **Refresh job** page. With a Jev key (see below), pairs are then
approved automatically; without one, approve them in **Review**. The scanner picks
them up on its next sweep.

```sh
journalctl --user -u arbscan -f        # live log, including OPEN/CLOSE lines per opportunity
systemctl --user restart arbscan       # after changing config.toml
```

### Without the dashboard

Each stage is also a CLI command:

```sh
.venv/bin/arbscan catalog              # ~1.5 min
.venv/bin/arbscan match                # ~1 min on a Pi 4
.venv/bin/arbscan discover             # live discovery; --once for a single check
.venv/bin/arbscan autoreview           # Jev review; --dry-run to only print verdicts
.venv/bin/arbscan review               # terminal review; --list to just print
.venv/bin/arbscan scan                 # scanner only; Ctrl-C to stop
.venv/bin/arbscan report --hours 24
```

`pairs.csv` can also be edited by hand; the scanner reloads it when it changes:

```csv
kalshi_ticker,pm_slug,relation,added,note
KXMLBGAME-26SEP241915CINATL-CIN,aec-mlb-cin-atl-2026-09-24,same,2026-09-24,
KXMLBGAME-26SEP241915CINATL-ATL,aec-mlb-cin-atl-2026-09-24,inverse,2026-09-24,
```

Pairs whose markets have closed stop being polled. The Pairs page removes them from
the file in one click.

### Auto-approve

Games are listed daily, so you'd otherwise review every one by hand. Once you've
compared a series' rulebooks yourself (say, Kalshi MLB game winners vs Polymarket US
MLB moneylines), add an `[[auto_approve]]` rule to `config.toml`; see
`config.example.toml`. Each refresh then approves confident, mutual-best matches for
that series automatically.

### Live discovery

The full refresh runs hourly, but new markets don't wait for it. `arbscan serve` also
keeps a low-priority `arbscan discover` process running, restarting it if it exits.
It holds both venues' markets in an in-memory index and asks each venue for anything
listed since its last check: Kalshi every 15 s (`min_created_ts`) and Polymarket US
every 30 s (`startDateMin`). With a Kalshi API key it also listens to Kalshi's
lifecycle feed, which announces each market as it is created, and checks Kalshi
straight away when one arrives. Each new market is added to the catalog and matched
against the other venue on the spot. Its suggestions go through the auto-approve rules
and Jev, and approved pairs reach the scanner within a second. So a pair is usually
being priced within 15–30 s of the second venue listing it, instead of up to an hour
later.

In testing, the two venues listed ~500 (Kalshi) and ~900 (Polymarket US) markets an
hour, mostly hourly index/commodity markets and player props with no counterpart, so
only a few become suggestions. The index takes ~300 MB and is rebuilt after each full
refresh. Its log shows up with the refresh job's on the **Refresh job** page. Set
`discovery = false` to turn it off.

### Automatic review with Jev

[Jev](https://docs.typesafe.ai) is TypeSafe's decision model: it answers typed
questions about some text with calibrated probabilities instead of generating text.
For each undecided suggestion, `arbscan/jev.py` sends both markets' titles, outcome
labels and rules, and asks three questions in one call:

- Which Polymarket bet (YES, NO, or neither) pays out in exactly the same situations
  as Kalshi YES?
- Do both markets count the same competition and scope? (This catches conference vs
  national stat leaders, and Hank Aaron Award vs MVP.)
- Do Polymarket's rules contradict its own title? About half of Polymarket US's
  college and NFL "+X" spread markets say YES is the underdog covering in the title
  but the favourite winning by more than X in the rules.

A pair is approved only if Jev picks the side the matcher proposed (P ≥ 0.6), the
scope matches and the rules agree. It is rejected if Jev is confident neither side is
the same bet. Everything else stays in the Review queue, with Jev's reason. The
questions and thresholds live together at the top of `jev.py`.

Validation (jev-1.13.0): on ~140 hand-labelled pairs it approved all 76 equivalent ones
and none of the 43 wrong games, flipped sides or different competitions. One subtle
case got through: Kalshi counts a #1 album any time in 2026, while Polymarket only
counts charts after its market opened. On 70 further pairs it hadn't been tuned on,
all 50 approvals were correct. Its mistakes lean safe: a few valid pairs get rejected
or left unsure. Because the scanner never trades, a wrong approval shows up as a
suspicious opportunity, not a loss. Still, read both rulebooks before trading on one.

Cost is ~900 input tokens per pair at $0.042 per million: about $0.20 for a first
pass over ~5,000 suggestions (~10 minutes at the default 8 requests/s). After that
only new pairs, or pairs whose text changed, are sent.

To turn it on, put your key at the top of `config.toml` (which git ignores) and
restart. It has to go above any `[[auto_approve]]` table, or TOML reads it as part of
that table:

```sh
{ echo 'jev_api_key = "apikey_..."'; cat config.toml 2>/dev/null || true; } > config.new
mv config.new config.toml
chmod 600 config.toml
systemctl --user restart arbscan
```

`TYPESAFE_API_KEY` in the environment works too. `jev_model` is pinned to
`jev-1.13.0` because the thresholds were tuned on it.

### Latency

How quickly the scanner sees a price change depends on the mode, which the Overview
page shows:

| Mode | How | Staleness of a price |
|---|---|---|
| **Streaming** (both venues' API keys) | WebSocket order books: Kalshi `orderbook_delta` (snapshot, then sequenced deltas) and Polymarket US `MARKET_DATA` (full book and trading state per update). Each update re-prices only the pairs using that market, in tens of microseconds. | Measured on a Pi with ~4,400 pairs, from the exchange's own timestamp to the computed edge: Kalshi ~50 ms median (~55 ms p90), Polymarket US ~125 ms median (~190 ms p90). Polymarket occasionally delivers a burst of updates 1–4 s late (~1–2% of them); that delay is upstream. |
| **Polling** (no keys) | Each sweep fetches both venues for ~100 pairs at a time, together, at 15 requests/s per venue, and walks depth for the pairs that look profitable. | ~0.5 s with a few hundred pairs, ~3 s with ~4,000. |

Things learned from the live feeds that the code relies on:

- Kalshi merges every orderbook subscribe on a connection into one subscription (one
  held 6,000 markets, all snapshots in ~1 s), and its acknowledgements share the
  sequence numbers. On a real gap the feed reconnects for fresh snapshots.
- Polymarket US allows 10 subscriptions of 100 markets per connection, so the feed
  opens one connection per ~1,000 markets.
- Recordings (quotes, opportunities, per-second summaries) are written by a
  background thread. Under streaming volume, SQLite's WAL checkpoints on the SD card
  otherwise froze every feed for ~2 s every ~9 s.

Streaming also fixes a source of phantom opportunities. When polling, two prices a
few seconds apart can look like a 30¢ gap during a live game. Polymarket US also
reports when a market is suspended or halted, and those pairs are paused instead of
compared against a frozen quote.

To stream, create keys on both venues and put them in `config.toml` (above any
`[[auto_approve]]` table), then restart:

- **Kalshi:** Account → Profile → API Keys → Create New API Key. Keep the downloaded
  private key file outside the repo, e.g. `~/.config/arbscan/kalshi.key`
  (`chmod 600`), and set `kalshi_key_id` and `kalshi_private_key_path`.
- **Polymarket US:** [polymarket.us/developer](https://polymarket.us/developer) →
  create a key; set `pmus_key_id` and `pmus_secret_key` (shown once).

These keys can place orders, even though arbscan never does. Keep `config.toml` and
the key file readable only by you.

### Resource use

| | |
|---|---|
| Service (scanner + dashboard) | ~170 MB RAM and ~20% of one core while streaming ~4,400 pairs (~1,000 Kalshi and ~100 Polymarket updates a second). Runs at normal CPU priority so feed messages are handled promptly. |
| Refresh (hourly) | A separate low-priority process for ~2 minutes (plus the Jev review, which only sends new pairs). It peaks around 200 MB during catalog and match, then exits. |
| Limits | The systemd unit caps the whole service at 3 GB (soft limit 2 GB). |
| Disk | Top-of-book quotes are written only when they change. Depth snapshots are stored only for profitable observations. Expect tens of MB/day for a few hundred pairs. |

SQLite runs in WAL mode with `synchronous=NORMAL` and commits once per sweep (once a
second when streaming), which
keeps SD-card writes low. Dashboard queries use their own short-lived read
connections on worker threads, so a slow page never stalls the scanner.

## Data

Everything is in `data/arbscan.db`, so you can query it directly:

| table | contents |
|---|---|
| `markets` | the catalog, including full rules text |
| `candidates`, `decisions` | matcher output and review decisions (`source`: human, rule or jev) |
| `jev_reviews` | Jev's verdict, reason and answers for every pair it has read |
| `discovered` | markets live discovery added between refreshes, with how many suggestions each got |
| `quotes` | top of book per pair, written on change, with the net edge per direction |
| `opportunities` | each profitable depth-walked observation, with both order books (JSON) |
| `episodes` | contiguous profitable runs: start/end, peak edge, peak profit, capital, days to resolution |
| `sweeps` | scanner health: one row per sweep (polling) or per second (streaming, where `dur_ms` is the median exchange-to-edge latency) |

## Tests

```sh
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

## Going further

- **Real-time data.** Kalshi's and Polymarket US's WebSocket feeds need API keys.
  They're worth adding only if the report shows windows short enough that 3-second
  polling misses them.
- **Execution.** Only once the report shows repeatable, sizeable windows. The obvious
  first improvement is posting a maker order on one leg and taking on the other: it
  saves one taker fee, and Polymarket US pays makers a rebate.
