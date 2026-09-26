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
   Jev reads both rulebooks for each one and approves only the pairs it is sure of;
   everything else is rejected, including the ones it can't decide. Approved pairs go
   to `pairs.csv`.
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

### Novig (being added)

[Novig](https://docs.novig.com) is a sports-only exchange. Its game markets charge
takers `0.03 × p × (1 − p)` only while the game is live, so a fill before kickoff is
free, and a Kalshi–Novig or Polymarket–Novig pair needs only ~1.75¢ to break even
before the game. Season futures (`0.06`, always charged) are left out: they tie money
up for months.

So far arbscan reads Novig's public catalog and books, without a key:

- `arbscan catalog` adds Novig's open game markets (`markets.venue = 'N'`). Each
  Novig market has two outcomes; YES is the one the market is about ("Over", "Yes",
  or the team or player it names), recorded in `novig_outcomes`.
- `arbscan match` also suggests Novig pairs with both other venues
  (`novig_candidates`), with the same hard filters as Kalshi–Polymarket plus bet
  types that read alike but settle differently (games vs sets, method of victory,
  first to score, both teams to score).
- `arbscan novig-gaps` samples the gaps: for each mutual-best pair whose game starts
  within `--hours`, it reads both books a few seconds apart and records the edge after
  fees at $100 per venue (`novig_gaps`); `--report` summarizes. Novig limits public
  reads to about 2 books a second per IP, so a sweep of 600 markets takes ~5 minutes.

Streaming Novig's books and paper trading its pairs needs a Novig API key (see
[Get a key](https://docs.novig.com/api/api-keys)); its holder must open the Novig app
from a permitted state at least every 3 days or the API refuses requests.

### Sizing to your bankroll, and picks

Opportunities are sized to `bankroll_usd` ($300 by default), assumed split evenly
across the two venues because each leg is paid for on its own venue. A window's size,
capital and profit are what $150 on each side could buy at that moment. Changing the
bankroll re-sizes the recorded history at the next start, from the order books stored
with each observation (`arbscan rescale` does it by hand).

Money in a pair is tied up until the market resolves, so a small edge that resolves
tonight beats a bigger one that resolves in three months. Windows are ranked by
**return per year of lock-up** (profit ÷ capital, scaled by the days until the market
resolves; anything resolving within 6 hours counts as 6 hours), and a window is a
**pick** only if it:

- resolves within `pick_max_days` (7 days),
- returns at least `pick_min_annualized_return` (100% a year),
- has an edge no bigger than `pick_max_edge` (5¢ per $1 pair; bigger has in practice
  meant a rules mismatch or a stale quote), and
- stayed open at least `pick_min_window_s` (1 s).

One pick may use at most `pick_max_stake` (half) of the money on a venue if it
resolves within a day, and proportionally less the longer it ties the money up (a
quarter at 2 days, 1/14 at 7 days), so a multi-day pick can't starve the quick ones.
Both the simulation below and paper trading size picks this way.

The Overview lists the best picks open right now, and the Opportunities page shows
picks by default (switch to "All windows" to see the rest and why each was left out).
Summing windows would assume a fresh bankroll for each one, so the Overview also
**simulates one bankroll**: whenever cash is free it funds the open picks with the best
return per year first, and each stake stays tied up until its market resolves (at
least an hour). On the first 4.6 hours of streaming data, these rules kept 163 of
~1,900 usable windows and did at least as well as looser or stricter ones, but that's
too little data to tune on; revisit them as history builds up.

### Paper trading

With both venues' API keys set, the streaming scanner also **paper trades** every pick
(`paper_trading = true`; the Paper trading page). It never places an order; it acts
the way a bot on this machine would, and fills against the live books:

1. **Wait** until the window has been open `pick_min_window_s` (1 s) and both legs'
   best prices have held still for `paper_quiet_s` (2 s). Most windows are one
   venue's price moving while the other venue's stale quote is still up, and that
   quote is taken or pulled within tens of milliseconds, well before an order from
   here could reach it. Trading at first sight (2026-09-25, 155 trades), 72% of
   trades missed and 17 of the 43 that filled a leg had to be unwound, which cost
   $6.40 against $6.71 of planned profit. 14 of the 15 fully unwound trades were
   windows that had opened in the very update that triggered them, and the Kalshi
   price they needed was gone a median 35 ms later; our Kalshi order took 250–600 ms
   to get there. Waiting for the window to be 1 s old wasn't enough on its own: in
   the next hour 3 of 18 trades still unwound, each on a Kalshi price that had moved
   in the last 1.3 s (a live cricket match, a gas-price market), in windows up to 34
   minutes old, while Polymarket's side hadn't moved for minutes. Replayed over the
   recorded quotes, requiring 2 s of stillness and leading with the leg that moved
   last (below) cut unwinds to 0.4% of filled trades (0.7% at twice the latency).
2. **Decide** from the liquidity that stayed on both books for the last second (a
   level that came and went doesn't count): size the pair from it and from the cash
   on each venue (`bankroll_usd` split in two, plus whatever settled trades paid out
   there), and send immediate-or-cancel limit orders at the worst price the size
   needs, one leg first and the other only for what that filled, once its fill report
   is back. `paper_lead_venue = "auto"` leads with the leg whose price moved most
   recently: it's the likelier to be gone, and a miss on the first leg costs
   nothing (`"P"` or `"K"` fix the order, `""` sends both at once).
3. **Arrive** after the measured latency. Each leg fills against that venue's book as
   it stood when the order would have reached the exchange: decision time, plus half
   a round trip to the venue's order API, plus how far our feed runs behind the
   exchange (the book we'll have *seen* by then is the one the order meets). Liquidity
   that others took or pulled in the meantime is gone, and a price that moved past
   the limit doesn't fill.
4. **Repair** an unequal fill once both fill reports are back: buy the missing leg at
   up to break-even; if that doesn't fill, sell the extra contracts back (buy the other
   side on the same venue, which nets out), taking the loss.
5. **Settle** when both markets publish results: each venue pays $1 per winning
   contract into its own cash, so cash drifts between venues the way it would for
   real, and a pair that wasn't really the same bet shows up as a loss.

Costs included: taker fees per order, rounded up to each venue's balance precision
(Kalshi $0.0001, Polymarket US whole cents); slippage from walking the book and from
the book moving before the orders arrive; losses unwinding one-sided fills; and cash
tied up until resolution. Our own simulated fills hide the liquidity they took for two
minutes so it can't be taken twice. Not included: deposit and withdrawal costs and
the days it takes to move cash between venues, and any queue position or rate limits
on real order entry.

**How the latency is measured without trading.** Every `latency_probe_s` (15 s) the
scanner times signed, read-only requests on the same host and kept-alive connection an
order would use: Kalshi's `GET /portfolio/balance`, and Polymarket US's
`POST /v1/order/preview`, which runs an order through validation and returns it
without creating it (a 1-contract, 1¢ IOC buy, so even a misrouted request couldn't
trade). Each simulated order draws one of the recent round trips at random, so jitter
shows up; the feed delay is the median of the exchanges' own timestamps on recent
updates. From the Pi in September 2026 that came to roughly: Kalshi 82 ms round trip
+ 45 ms feed ≈ **90 ms** from seeing a change to the order meeting the book, and
Polymarket US 110 ms + 125 ms ≈ **180 ms**.

`arbscan serve` keeps the paper account in the `paper_trades` table. Changing
`bankroll_usd` works like a deposit or withdrawal on each venue.

### What the numbers do *not* include

- **Execution risk.** The two legs can't be filled atomically. By the time you act,
  prices may have moved.
- **Windows shorter than the polling interval**, when polling (~1–3 s). Most of those belong to
  faster bots anyway.
- **Capital lockup.** The report shows days until resolution and an annualized return
  so you can compare it with just holding cash.
- **Settlement mismatch.** If two paired markets resolve differently, you lose both
  legs. Jev's review is the defence. Treat any gap that's large (>5¢) or lasts for hours as
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
  directions, 100 at a time (filtered and sorted on the server). Click a pair for
  which outcome matches which, its edge history, past windows and both rulebooks
  side by side with numbers, dates and settlement wording highlighted, or to stop
  watching it.
- **Opportunities.** Picks by default (or every profitable window): how long it
  lasted, how big the edge got, the capital it needed, its return per year, and why
  a window wasn't a pick.
- **Paper trading.** The simulated account: P&L, cash on each venue, how much of
  each pick actually filled, the latency used, and every paper trade.
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
approved automatically; without one, use `[[auto_approve]]` rules or
`arbscan review` in a terminal. The scanner picks them up right away.

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
.venv/bin/arbscan novig-gaps --hours 12 # sample Novig gaps; --once, --report
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
the same bet. Anything else (Jev unsure) is rejected too: a pair is only watched when
Jev is sure of it. Jev's reason is kept in the `jev_reviews` table. The questions and
thresholds live together at the top of `jev.py`.

Validation (jev-1.13.0): on ~140 hand-labelled pairs it approved all 76 equivalent ones
and none of the 43 wrong games, flipped sides or different competitions. One subtle
case got through: Kalshi counts a #1 album any time in 2026, while Polymarket only
counts charts after its market opened. On 70 further pairs it hadn't been tuned on,
all 50 approvals were correct. Its mistakes lean safe: a few valid pairs get rejected
(unsure counts as a rejection). Because the scanner never trades, a wrong approval shows up as a
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
| `markets` | the catalog, including full rules text (Novig publishes none; its row holds a summary of its structured fields) |
| `novig_outcomes`, `novig_candidates`, `novig_gaps` | Novig: which outcome is YES, suggested pairs with the other venues, and sampled gaps |
| `candidates`, `decisions` | matcher output and review decisions (`source`: human, rule or jev) |
| `jev_reviews` | Jev's verdict, reason and answers for every pair it has read |
| `paper_trades` | every simulated trade: what was planned, what filled on each venue, fees, unwinds, the books it saw and met (`books`, top 5 levels per leg), and the result |
| `discovered` | markets live discovery added between refreshes, with how many suggestions each got |
| `quotes` | top of book per pair, written on change, with the net edge per direction |
| `opportunities` | each profitable depth-walked observation, with both order books (JSON) |
| `episodes` | contiguous profitable runs: start/end, peak edge, peak profit, capital, days to resolution; `cut` = 1 if the scanner stopped while it was open |
| `sweeps` | scanner health: one row per sweep (polling) or per second (streaming, where `dur_ms` is the median exchange-to-edge latency) |
| `results` | how each paired market settled: what one YES contract paid (`yes_value`), the venue's result and status, when it closed and settled |
| `window_outcomes` (view) | every window whose two markets have settled, with `payout_per_pair` and `realized_at_peak` |
| `pair_outcomes` (view) | every approved pair whose markets have settled, and whether they settled as one bet (`consistent`) |

### Settlement results, for backtesting

A trade only worked if both markets settled as one bet. The scanner records each
venue's published result for every market that has been paired, had a profitable
window or a paper trade (`arbscan/results.py`): Kalshi's the moment its lifecycle feed
announces it, and both venues' from their APIs every 10 minutes once a market has
closed (every 6 hours before that) until the result is in. Polymarket US doesn't say
when it resolved a market, so `first_final_ts` records when we first saw it.

`window_outcomes` joins the results to the windows. `payout_per_pair` is what one
contract pair in the window's direction paid: 1 when the markets settled as one bet,
0 or 2 when they didn't, so for example:

```sql
-- How often did profitable windows really pay, and what would taking them at their peak have made?
SELECT COUNT(*) AS windows, SUM(payout_per_pair = 1) AS paid, SUM(realized_at_peak) AS realized
FROM window_outcomes;

-- Approved pairs that did not settle as one bet (what Jev or a rule got wrong).
SELECT * FROM pair_outcomes WHERE NOT consistent;
```

Together with `opportunities` (both order books, top 10 levels, at most once a second
per pair and direction), `quotes` (every top-of-book change) and `paper_trades`, that
is enough to replay a strategy and score it on what actually settled. Windows still
open when the service restarts are saved with `cut = 1`; if the scanner is back within
10 minutes and the window is still open, the saved row is reopened instead of a second
one starting.

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
