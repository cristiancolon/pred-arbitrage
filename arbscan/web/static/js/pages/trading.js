// The live trading desk: the account, the per-trade cap, how orders are doing, and every trade.
import { html } from "../vendor/preact-htm.js";
import { ago, cents, dateTime, dirLabel, int, money, navigate, useFetch, useNow, usePref } from "../lib.js";
import { ChartCard, LineChart } from "../charts.js";
import { Badge, Banner, Card, DataTable, Empty, Icon, PairName, Seg } from "../ui.js";

const RANGES = [
  { value: 24, label: "24h" }, { value: 168, label: "7d" }, { value: 720, label: "30d" }, { value: 2160, label: "90d" },
];
const VIEWS = [{ value: "all", label: "All" }, { value: "open", label: "Open" }, { value: "settled", label: "Settled" }];
const VENUE = { K: "Kalshi", P: "Polymarket US" };
const NOT_SKIPS = new Set(["sent", "open", "unwound", "missed"]);
// Shards that hold Kalshi's markets, as far as the live trader has seen them.
const SHARD_NAME = { 0: "everything else", 3: "tennis · baseball · basketball" };

const ms = (v) => (v == null ? "—" : `${Math.round(v)}`);
const pct = (v) => (v == null ? "—" : `${Math.round(v * 100)}%`);
const signed = (v) => (v == null ? "—" : `${v > 0 ? "+" : ""}${money(v)}`);
const tone = (v) => (v == null || Math.abs(v) < 0.005 ? "" : v > 0 ? "up" : "down");

function Meter({ value, max, warnAt = 0.6, label }) {
  const f = max ? Math.max(0, Math.min(1, value / max)) : 0;
  const t = f >= 0.9 ? "critical" : f >= warnAt ? "warning" : "ok";
  return html`<div class=${`meter ${t}`} role="meter" aria-valuemin="0" aria-valuemax=${max} aria-valuenow=${value}
    aria-label=${label}><span style=${`width:${(f * 100).toFixed(1)}%`}></span></div>`;
}

function StatusPill({ enabled, account }) {
  if (!enabled) return html`<span class="pill off"><span class="dot"></span>Off</span>`;
  if (!account) return html`<span class="pill off"><span class="dot"></span>Starting…</span>`;
  if (account.halted) return html`<span class="pill stopped"><span class="dot critical"></span>Stopped</span>`;
  if (account.in_flight) return html`<span class="pill busy"><span class="dot accent pulse"></span>Trading now</span>`;
  return html`<span class="pill on"><span class="dot good pulse"></span>Live · watching</span>`;
}

function Hero({ data, enabled, a }) {
  const cash = a ? a.cash.K + a.cash.P : null;
  const tied = a ? a.tied.K + a.tied.P : null;
  const bankroll = a ? cash + tied : null;
  const pnl = a ? a.realized + a.locked : data?.totals?.pnl;
  const today = data?.today;
  return html`<section class="hero card">
    <div class="hero-main">
      <div class="eyebrow">Total bankroll <${StatusPill} enabled=${enabled} account=${a} /></div>
      <div class="hero-value num">${money(bankroll)}</div>
      <div class="hero-sub">
        <span><i class="sw k"></i>Cash ${money(cash)}</span>
        <span><i class="sw t"></i>In open trades ${money(tied)} <span class="muted">at cost</span></span>
      </div>
      ${a && bankroll > 0 && html`<div class="stack" aria-hidden="true">
        <span class="k" style=${`flex:${a.cash.K}`} title=${`Kalshi cash ${money(a.cash.K)}`}></span>
        <span class="p" style=${`flex:${a.cash.P}`} title=${`Polymarket US cash ${money(a.cash.P)}`}></span>
        <span class="t" style=${`flex:${tied}`} title=${`Open trades ${money(tied)}`}></span>
      </div>`}
    </div>
    <div class="hero-pnl">
      <div class="eyebrow">Live P&L</div>
      <div class=${`hero-value num ${tone(pnl)}`}>${signed(pnl)}</div>
      <dl class="mini">
        <div><dt>Today</dt><dd class=${`num ${tone(today?.pnl)}`}>${signed(today?.pnl)}</dd></div>
        <div><dt>Settled</dt><dd class="num">${signed(a?.realized)}</dd></div>
        <div><dt title="Profit the open trades locked in, paid when both legs settle as one bet">Locked in</dt>
          <dd class="num">${signed(a?.locked)}</dd></div>
      </dl>
    </div>
  </section>`;
}

function VenueCard({ name, cls, cash, tied, children }) {
  return html`<div class="card venue-card">
    <div class="venue-head"><span class=${`venue-mark ${cls}`}></span>${name}</div>
    <div class="big num">${money(cash)}</div>
    <div class="foot">cash${tied ? html` · <span class="num">${money(tied)}</span> in open trades` : ""}</div>
    ${children}
  </div>`;
}

function Shards({ shards }) {
  if (!shards) return html`<div class="foot muted">Shards not read yet</div>`;
  const all = Object.entries(shards).sort((x, y) => y[1] - x[1]);
  const entries = all.filter(([, c]) => c >= 0.005);
  const empty = all.filter(([, c]) => c < 0.005).map(([s]) => s);
  const total = entries.reduce((s, [, c]) => s + c, 0) || 1;
  return html`<div class="shards">
    ${entries.map(([s, c]) => html`<div class="shard" key=${s}>
      <div class="shard-top"><span>Shard ${s}${SHARD_NAME[s] ? html` <span class="muted">· ${SHARD_NAME[s]}</span>` : ""}</span>
        <b class="num">${money(c)}</b></div>
      <div class="bar"><span style=${`width:${((100 * c) / total).toFixed(1)}%`}></span></div>
    </div>`)}
    ${empty.length > 0 && html`<div class="foot muted">Shard${empty.length > 1 ? "s" : ""} ${empty.join(", ")} empty · an order fills only from its market's shard</div>`}
  </div>`;
}

// One venue's money: what's free to trade, and what open trades hold until they settle.
function Holdings({ cash, tied }) {
  if (cash == null) return null;
  const total = cash + (tied || 0) || 1;
  const row = (name, v, cls) => html`<div class="shard">
    <div class="shard-top"><span>${name}</span><b class="num">${money(v)}</b></div>
    <div class=${`bar ${cls}`}><span style=${`width:${((100 * v) / total).toFixed(1)}%`}></span></div>
  </div>`;
  return html`<div class="shards">${row("Free to trade", cash, "p")}${row("In open trades, at cost", tied || 0, "t")}</div>`;
}

function CapCard({ a }) {
  const lim = a?.limits || {};
  const bankroll = a ? a.cash.K + a.cash.P + a.tied.K + a.tied.P : null;
  const frac = lim.max_stake_frac;
  return html`<div class="card venue-card cap">
    <div class="venue-head"><${Icon} name="zap" size=${15} />Per-trade cap</div>
    <div class="big num">${money(lim.max_stake)}</div>
    <div class="foot">${frac != null ? `${Math.round(frac * 100)}% of the ${money(bankroll)} bankroll, both legs together` : "—"}</div>
    ${frac != null && html`<div class="cap-bar" aria-hidden="true"><span style=${`width:${frac * 100}%`}></span></div>`}
    <div class="foot muted">Grows as the bankroll grows · at least ${money(lim.min_profit)} expected profit a trade</div>
  </div>`;
}

function Metric({ label, value, unit, sub, title, children }) {
  return html`<div class="card metric" title=${title}>
    <div class="label">${label}</div>
    <div class="value num">${value}${unit && html`<small>${unit}</small>`}</div>
    ${sub && html`<div class="sub">${sub}</div>`}
    ${children}
  </div>`;
}

function Split({ k, p }) {
  return html`<div class="split"><span><i class="venue-mark k"></i>${k}</span><span><i class="venue-mark p"></i>${p}</span></div>`;
}

function TradeStatus({ t }) {
  if (t.status === "missed") return html`<span class="muted nowrap" style="font-size:12px">No fill</span>`;
  if (t.status === "open") return html`<${Badge} icon="clock">${t.note ? "Open · unhedged" : "Open"}<//>`;
  if (t.note === "unwound") return html`<${Badge} tone="warning" icon="alert">Unwound<//>`;
  if (t.settled_as === "void") {
    return html`<span title="A venue cancelled the market and settled it at a price, so the pair didn't pay exactly $1">
      <${Badge} tone="warning" icon="alert">Voided<//></span>`;
  }
  if (t.settled_as === "conflict") {
    return html`<span title="The two markets settled differently: the pair wasn't one bet, or a venue resolved it wrongly">
      <${Badge} tone="critical" icon="xcircle">Mismatch<//></span>`;
  }
  return html`<${Badge} tone=${t.pnl >= 0 ? "good" : "critical"} icon=${t.pnl >= 0 ? "check" : "xcircle"}>Settled<//>`;
}

function Guardrails({ a, now }) {
  const lim = a?.limits || {};
  const days = lim.attested_until ? (lim.attested_until - now) / 86400 : null;
  return html`<${Card} title="Guardrails" sub="Trading stops by itself when one of these trips">
    <div class="rails">
      <div class="rail">
        <div class="rail-top"><span>Lost today</span><span class="num">${money(lim.lost_today)} <span class="muted">of ${money(lim.daily_loss, 0)}</span></span></div>
        <${Meter} value=${lim.lost_today || 0} max=${lim.daily_loss} label="Lost today against the daily limit" />
      </div>
      <div class="rail">
        <div class="rail-top"><span>Trades today</span><span class="num">${int(lim.trades_today)} <span class="muted">of ${int(lim.max_trades)}</span></span></div>
        <${Meter} value=${lim.trades_today || 0} max=${lim.max_trades} warnAt=${0.8} label="Trades today against the daily limit" />
      </div>
      <div class="rail">
        <div class="rail-top"><span title="After it lapses, Kalshi takes no API orders on sports">Kalshi location check</span>
          <span class="num">${days == null ? "not read" : days <= 0 ? "lapsed" : `${days.toFixed(1)} days left`}</span></div>
        <${Meter} value=${days == null ? 0 : Math.max(0, 7 - days)} max=${7} warnAt=${5 / 7} label="Location check used up" />
      </div>
    </div>
    <dl class="facts" style="margin-top:14px">
      <dt title="A leg left over is sold back no lower than this under what it cost; if it can't be, trading stops">Sell-back floor</dt>
      <dd>${cents(lim.unwind_max_loss, { sign: false, digits: 0 })} under cost</dd>
      <dt title="At least 20 settled pairs, none conflicting, at most 2% voided">Series cleared to trade</dt>
      <dd>${lim.series == null ? "not read yet" : int(lim.series)}</dd>
      <dt>Markets already held</dt><dd>${lim.held == null ? "—" : int(lim.held)} <span class="muted">(never doubled up)</span></dd>
      <dt>Location check lapses</dt><dd>${lim.attested_until ? dateTime(lim.attested_until) : "—"}</dd>
    </dl>
  <//>`;
}

function Execution({ a, orders }) {
  const skipped = Object.entries(a?.stats || {}).filter(([k]) => !NOT_SKIPS.has(k)).sort((x, y) => y[1] - x[1]);
  const sent = (v) => {
    const o = orders?.orders?.[v] || {};
    const n = Object.values(o).reduce((s, x) => s + x, 0);
    const bad = (o.rejected || 0) + (o.unknown || 0);
    return html`<span class="num">${int(n)}</span>${bad ? html` <span class="bad">· ${int(o.rejected || 0)} rejected${o.unknown ? `, ${int(o.unknown)} unknown` : ""}</span>` : ""}`;
  };
  const problem = orders?.problems?.[0];
  const probe = a?.latency;
  return html`<${Card} title="Execution" sub="Orders sent, and why picks were passed over">
    <dl class="facts">
      <dt>Kalshi orders</dt><dd>${sent("K")}</dd>
      <dt>Polymarket US orders</dt><dd>${sent("P")}</dd>
      <dt title="Signed read-only requests every few seconds, between trades">Round trip right now</dt>
      <dd>${probe ? `Kalshi ${ms(probe.K.rtt_p50_ms)} ms · Polymarket US ${ms(probe.P.rtt_p50_ms)} ms` : "—"}</dd>
    </dl>
    <div class="skips">
      <div class="skips-head">Picks passed over <span class="muted">since the last restart</span></div>
      ${skipped.length ? skipped.slice(0, 6).map(([k, n]) => html`<div class="skip" key=${k}><span>${k}</span><b class="num">${int(n)}</b></div>`)
        : html`<div class="muted" style="font-size:12.5px">None yet</div>`}
    </div>
    ${problem && html`<div class="problem"><${Icon} name="alert" size=${14} /> Latest problem, ${ago(problem.ts)}: ${VENUE[problem.venue]}
      ${problem.market}: ${problem.status}${problem.error ? ` (${problem.error})` : ""}</div>`}
  <//>`;
}

export function Trading() {
  const [hours, setHours] = usePref("tradingRange", 168);
  const [view, setView] = usePref("tradingView", "all");
  const now = useNow(5000);
  const { data, loading } = useFetch(`/api/live?hours=${hours}`, [], { refreshOn: (s) => Math.floor(s.pairsVersion / 3) });
  const a = data?.account;
  const enabled = data?.enabled;
  const tot = data?.totals || {};
  const lat = data?.order_latency;
  const fills = data?.order_fills;
  const all = data?.trades || [];
  const trades = view === "all" ? all : all.filter((t) => t.status === view);
  const tiedK = a?.tied.K, tiedP = a?.tied.P;
  const series = [{ name: "P&L", color: "var(--series-1)", step: true, points: data?.curve || [] }];
  return html`
    ${data && !enabled && html`<${Banner} tone="info">Live trading is off. Switch it on with <code>live_trading = true</code>
      in config.toml; <code>arbscan live-check</code> shows what it would start with.<//>`}
    ${a?.halted && html`<${Banner} tone="critical"><b>Stopped:</b> ${a.halted}. Check the positions on both venues, then run
      <code>arbscan live-resume</code> on the machine it runs on.<//>`}

    <${Hero} data=${data} enabled=${enabled} a=${a} />

    <div class="grid cols-3">
      <${VenueCard} name="Kalshi" cls="k" cash=${a?.cash.K} tied=${tiedK}>
        <${Shards} shards=${a?.limits?.shard_cash} />
      <//>
      <${VenueCard} name="Polymarket US" cls="p" cash=${a?.cash.P} tied=${tiedP}>
        <${Holdings} cash=${a?.cash.P} tied=${tiedP} />
        <div class="foot muted" style="margin-top:auto">Buying power, read from the venue every 30 s and after every trade</div>
      <//>
      <${CapCard} a=${a} />
    </div>

    <div class="metrics">
      <${Metric} label="Filled" value=${tot.fill_rate != null ? Math.round(tot.fill_rate * 100) : "—"} unit=${tot.fill_rate != null ? "%" : ""}
        title="Contract pairs that filled on both venues, out of what the books showed when deciding"
        sub=${`${int(tot.sent - tot.missed)} trades · ${int(tot.missed)} no fill · ${int(tot.unwound)} unwound`}>
        <${Split} k=${`Kalshi ${pct(fills?.K?.rate)}`} p=${`Poly ${pct(fills?.P?.rate)}`} />
      <//>
      <${Metric} label="Order latency" value=${html`${ms(lat?.K?.p50_ms)}<span class="muted"> / </span>${ms(lat?.P?.p50_ms)}`} unit=" ms"
        title="Round trip of each real order, from sending it to the venue's reply: the median of the latest 50 per venue"
        sub="Kalshi / Polymarket US, median of the latest real orders">
        <${Split} k=${`p90 ${ms(lat?.K?.p90_ms)} ms`} p=${`p90 ${ms(lat?.P?.p90_ms)} ms`} />
      <//>
      <${Metric} label="Open trades" value=${int(a?.open)} sub=${a ? `${money(a.tied.K + a.tied.P)} at cost · ${signed(a.locked)} locked in` : "—"} />
      <${Metric} label="Today" value=${int(data?.today?.trades)} unit=${` trade${data?.today?.trades === 1 ? "" : "s"}`}
        sub=${html`<span class=${tone(data?.today?.pnl)}>${signed(data?.today?.pnl)}</span> settled today`} />
    </div>

    <${ChartCard} title="P&L over time" loading=${loading && data}
      sub="Cumulative: settled trades at their result, open ones at the profit they locked in"
      actions=${html`<${Seg} label="Time range" options=${RANGES} value=${hours} onChange=${setHours} />`}
      table=${{ columns: ["Time", "P&L"], rows: (data?.curve || []).slice().reverse().map((p) => [new Date(p[0] * 1000).toLocaleString(), money(p[1])]) }}>
      <${LineChart} series=${series} height=${220} zero=${0} zeroLabel="Break-even" area yFmt=${(v) => money(v, Math.abs(v) < 10 ? 2 : 0)}
        xDomain=${data ? [data.since, now] : undefined} emptyText="No live trades in this range yet" />
    <//>

    <div class="grid cols-2 align-start">
      <${Guardrails} a=${a} now=${now} />
      <${Execution} a=${a} orders=${data?.orders} />
    </div>

    <${Card} title="Trades" sub=${data?.total > 300 ? "Newest 300 in range" : "Newest first · click a row for the pair"} flush
      actions=${html`<${Seg} label="Show" options=${VIEWS} value=${view} onChange=${setView} />`}>
      <${DataTable} rows=${trades} rowKey=${(t) => t.id} limit=${25} onRowClick=${(t) => navigate("pairs", { id: t.pair })}
        empty=${html`<${Empty} icon="play" title="No trades here yet">When a pick passes the live limits, its orders go out and the trade shows here.<//>`}
        columns=${[
          { key: "ts", label: "Time", render: (t) => html`<span class="nowrap">${dateTime(t.ts)}</span>` },
          { key: "k_title", label: "Market", cls: "market", render: (t) => html`<${PairName} ...${t} />` },
          { key: "direction", label: "Bought", render: (t) => html`<span class="nowrap soft">${dirLabel(t.direction)}</span>` },
          { key: "planned_size", label: "Filled", cls: "num", title: "Contracts filled on Kalshi / Polymarket US, of the pairs wanted",
            render: (t) => html`<span class="nowrap">${t.k_qty === t.p_qty ? int(t.k_qty) : html`${int(t.k_qty)}<span class="muted">/</span>${int(t.p_qty)}`}<span class="muted"> of ${int(t.planned_size)}</span></span>` },
          { key: "planned_cost", label: "Cost", cls: "num", sortValue: (t) => (t.k_out || 0) + (t.p_out || 0), render: (t) => money((t.k_out || 0) + (t.p_out || 0)) },
          { key: "planned_edge", label: "Edge", cls: "num", render: (t) => cents(t.planned_edge) },
          { key: "result", label: "P&L", cls: "num", sortValue: (t) => (t.status === "settled" ? t.pnl : t.locked_profit),
            render: (t) => {
              if (t.status === "missed") return "—";
              const v = t.status === "settled" ? t.pnl : t.locked_profit;
              return html`<b class=${tone(v)}>${signed(v)}</b>`;
            } },
          { key: "k_delay_ms", label: "Latency", cls: "num", sortable: false,
            render: (t) => html`<span class="nowrap muted" title="Kalshi / Polymarket US: decided → order at the book">${int(t.k_delay_ms)} / ${int(t.p_delay_ms)} ms</span>` },
          { key: "status", label: "", sortable: false, render: (t) => html`<${TradeStatus} t=${t} />` },
        ]} />
    <//>`;
}
