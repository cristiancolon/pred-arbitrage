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

### Resource use

| | |
|---|---|
| Service (scanner + dashboard) | ~60 MB RAM. With a few hundred pairs, each sweep is a handful of requests. |
| Refresh (hourly) | A separate low-priority process for ~2 minutes (plus the Jev review, which only sends new pairs). It peaks around 200 MB during catalog and match, then exits. |
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
| `candidates`, `decisions` | matcher output and review decisions (`source`: human, rule or jev) |
| `jev_reviews` | Jev's verdict, reason and answers for every pair it has read |
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
