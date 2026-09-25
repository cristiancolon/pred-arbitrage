# arbscan

A read-only scanner that measures how much cross-venue arbitrage actually exists
between **Kalshi** and **Polymarket US**. It never places orders and needs no API keys:
both venues publish market data publicly.

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
3. **review** shows each suggestion with both rulebooks side by side. You approve it
   as same/inverse or reject it. Approved pairs go to `pairs.csv`.
4. **scan** polls the approved pairs every few seconds. For each pair it checks both
   directions (e.g. buy Kalshi YES + Polymarket NO) and computes the edge after both
   taker fees. When that edge is positive it fetches depth and walks both order
   books. The result is how many contracts are profitable and for how many dollars.
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

### What the numbers do *not* include

- **Execution risk.** The two legs can't be filled atomically. By the time you act,
  prices may have moved.
- **Windows shorter than the poll interval** (3 s by default). Most of those belong to
  faster bots anyway.
- **Capital lockup.** The report shows days until resolution and an annualized return
  so you can compare it with just holding cash.
- **Settlement mismatch.** If two paired markets resolve differently, you lose both
  legs. Review is the defence. Treat any gap that's large (>5¢) or lasts for hours as
  a sign the markets aren't really equivalent, not as free money. The report flags these.

## The dashboard

`arbscan serve` runs everything in one process: the scanner, the scheduled refresh
(catalog, then match, every 6 hours), and a web dashboard at `http://<pi>:8787/`.

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
  highlighted. Keys: `S` same, `I` inverse, `R` reject, `J`/`K` next/previous.
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

Needs Python 3.11+ (Raspberry Pi OS Bookworm ships 3.11). Copy this directory to
`~/pred-arbitrage` on the Pi, then:

```sh
cd ~/pred-arbitrage
cp config.example.toml config.toml     # optional; defaults are fine
sh deploy/install.sh                   # venv + systemd user service + starts it
```

Then open `http://<pi-address>:8787/`. On first start the service downloads the
market catalog and runs matching by itself; this takes about two minutes, and you
can watch it on the **Refresh job** page. Then approve pairs in **Review**. The
scanner picks them up on its next sweep.

```sh
journalctl --user -u arbscan -f        # live log, including OPEN/CLOSE lines per opportunity
systemctl --user restart arbscan       # after changing config.toml
```

### Without the dashboard

Each stage is also a CLI command:

```sh
.venv/bin/arbscan catalog              # ~1.5 min
.venv/bin/arbscan match                # ~1 min on a Pi 4
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

### Resource use

| | |
|---|---|
| Service (scanner + dashboard) | ~60 MB RAM. With a few hundred pairs, each sweep is a handful of requests. |
| Refresh (every 6 h) | A separate low-priority process for ~2 minutes. It peaks around 200 MB during catalog and match, then exits. |
| Limits | The systemd unit caps the whole service at 768 MB (soft limit 500 MB), leaving the rest of the Pi's memory to other processes. |
| Disk | Top-of-book quotes are written only when they change. Depth snapshots are stored only for profitable observations. Expect tens of MB/day for a few hundred pairs. |

SQLite runs in WAL mode with `synchronous=NORMAL` and commits once per sweep, which
keeps SD-card writes low. Dashboard queries use their own short-lived read
connections on worker threads, so a slow page never stalls the scanner.

## Data

Everything is in `data/arbscan.db`, so you can query it directly:

| table | contents |
|---|---|
| `markets` | the catalog, including full rules text |
| `candidates`, `decisions` | matcher output and your review decisions |
| `quotes` | top of book per pair, written on change, with the net edge per direction |
| `opportunities` | each profitable depth-walked observation, with both order books (JSON) |
| `episodes` | contiguous profitable runs: start/end, peak edge, peak profit, capital, days to resolution |
| `sweeps` | scanner health: timing, requests, errors |

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
