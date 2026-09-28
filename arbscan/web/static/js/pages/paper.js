import { html } from "../vendor/preact-htm.js";
import { cents, dateTime, dirLabel, int, money, navigate, ruleText, useFetch, useNow, usePref } from "../lib.js";
import { ChartCard, LineChart } from "../charts.js";
import { Badge, Banner, Card, DataTable, Empty, PairName, Seg, Tile } from "../ui.js";
import { RANGES } from "./overview.js";

const VENUE = { K: "Kalshi", P: "Polymarket US" };
const BOOKS = [
  { value: "paper", label: "Paper", title: "Every pick, with the paper account ($300)" },
  { value: "dry", label: "Dry run", title: "Only what a first, capped live run would take, with its real orders written out" },
];
const NOT_SKIPS = new Set(["sent", "open", "unwound", "missed"]);
const ms = (v) => (v == null ? "—" : `${Math.round(v)} ms`);

function Status({ t }) {
  if (t.status === "missed") return html`<span class="muted nowrap" style="font-size:12px">No fill</span>`;
  if (t.status === "open") return html`<${Badge} icon="clock">${t.note ? "Open · unhedged" : "Held to resolution"}<//>`;
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

function LatencyTable({ latency }) {
  if (!latency) return null;
  return html`<div class="table-wrap"><table class="data">
    <thead><tr><th></th><th class="num">Feed delay</th><th class="num">Round trip</th><th class="num">Seen → at book</th></tr></thead>
    <tbody>${["K", "P"].map((v) => {
      const l = latency[v];
      return html`<tr><td class="nowrap">${VENUE[v]}</td><td class="num">${ms(l.feed_ms)}</td>
        <td class="num nowrap" title=${`median of ${l.samples} recent measurements`}>${ms(l.rtt_p50_ms)}<span class="muted"> · p90 ${ms(l.rtt_p90_ms)}</span></td>
        <td class="num"><b>${ms(l.order_ms)}</b></td></tr>`;
    })}</tbody>
  </table></div>`;
}

function DryLimits({ live, orders }) {
  if (!live) return null;  // still loading
  const lim = live.limits || {};
  const skipped = Object.entries(live.stats || {}).filter(([k]) => !NOT_SKIPS.has(k));
  const pv = orders?.previews || {};
  const shards = lim.shard_cash
    ? Object.entries(lim.shard_cash).map(([s, c]) => `shard ${s}: ${money(c)}`).join(" · ") : "not read yet";
  const days = lim.attested_until ? (lim.attested_until - Date.now() / 1000) / 86400 : null;
  const refused = orders?.refused?.[0];
  return html`<${Card} title="Live limits" sub="What a first, capped live run would be held to. Nothing is sent">
    <dl class="facts">
      <dt>Per trade</dt><dd>at most ${money(lim.max_stake, 0)} for both legs, expected to make ${money(lim.min_profit)} or more</dd>
      <dt>Account</dt><dd>${money(live.deposits?.K, 0)} on each venue; no market the account already holds</dd>
      <dt title="Since the last restart">Picks the limits skipped</dt>
      <dd>${skipped.length ? skipped.map(([k, n]) => `${k}: ${int(n)}`).join(" · ") : "none yet"}</dd>
      <dt title="At least 20 settled pairs, none conflicting, at most 2% voided">Series with a clean record</dt>
      <dd>${lim.series == null ? "not read yet" : int(lim.series)}</dd>
      <dt>Lost today</dt><dd>${money(lim.lost_today)} of the ${money(lim.daily_loss, 0)} a day limit</dd>
      <dt title="Kalshi fills an order only from the cash on its market's shard">Kalshi cash by shard</dt><dd>${shards}</dd>
      <dt title="After it lapses, Kalshi takes no API orders on sports, elections or entertainment">Kalshi location check</dt>
      <dd>${days == null ? "not read yet, or never done" : `lapses ${dateTime(lim.attested_until)} (in ${days.toFixed(1)} days)`}</dd>
      <dt>Orders written out</dt><dd>Kalshi ${int(orders?.orders?.K || 0)} · Polymarket US ${int(orders?.orders?.P || 0)}</dd>
      <dt title="Polymarket US's order preview validates price, size, market state and buying power without placing anything">Polymarket US previews</dt>
      <dd>${int(pv.ok || 0)} accepted · ${int(pv.refused || 0)} refused${pv.pending ? ` · ${int(pv.pending)} waiting` : ""}</dd>
    </dl>
    ${refused ? html`<div class="muted" style="font-size:12px;padding-top:8px">Latest refusal: ${refused.market}: ${refused.preview}</div>` : null}
  <//>`;
}

export function Paper() {
  const [hours, setHours] = usePref("range", 24);
  const [book, setBook] = usePref("paperBook", "paper");
  const dry = book === "dry";
  const now = useNow(5000);
  const { data, loading } = useFetch(`/api/paper?hours=${hours}${dry ? "&book=dry" : ""}`, [],
    { refreshOn: (s) => Math.floor(s.pairsVersion / 5) });
  const live = data?.live;
  const tot = data?.totals || {};
  const res = tot.results || {};
  const trades = data?.trades || [];
  if (data && !live) {
    return html`<div class="filters"><${Seg} label="Account" options=${BOOKS} value=${book} onChange=${setBook} /></div>
      <${Banner} tone="info">${dry ? html`The dry run runs with the streaming scanner. Set both venues' API keys, and
      <code>dry_run = true</code> in config.toml.` : html`Paper trading runs with the streaming scanner. Set both venues' API keys and
      <code>paper_trading = true</code> in config.toml.`}<//>`;
  }
  const cash = live ? live.cash.K + live.cash.P : null;
  const tied = live ? live.tied.K + live.tied.P : null;
  const pnl = live ? live.realized + live.locked : null;
  const series = [{ name: "P&L", color: "var(--series-1)", step: true, points: data?.curve || [] }];
  return html`
    <div class="filters">
      <${Seg} label="Account" options=${BOOKS} value=${book} onChange=${setBook} />
      <${Seg} label="Time range" options=${RANGES} value=${hours} onChange=${setHours} />
      <span class="muted" style="font-size:12.5px">${dry
        ? "Simulated like paper trading, under a capped live run's limits. Every leg is also written out as the real order a live trader would send, and Polymarket US previews the buys. Nothing is sent."
        : "Simulated: no orders are placed. Each pick is filled against the live books as they stood when its orders would have arrived."}</span>
    </div>
    <div class="kpis">
      <${Tile} label=${dry ? "Dry-run P&L" : "Paper P&L"} value=${money(pnl)}
        title="Settled trades at their actual result, plus open ones at the profit they locked in (if both legs settle as one bet)"
        foot=${live ? `${money(live.realized)} settled · ${money(live.locked)} locked in on ${int(live.open)} open` : "—"} />
      <${Tile} label="Cash" value=${money(cash)}
        foot=${live ? `Kalshi ${money(live.cash.K)} · Polymarket ${money(live.cash.P)} · ${money(tied, 0)} tied up` : "—"} />
      <${Tile} label="Filled" value=${tot.fill_rate != null ? `${Math.round(tot.fill_rate * 100)}%` : "—"}
        title="Contract pairs that filled on both venues, out of what the books showed when deciding"
        foot=${`${int(tot.sent)} picks sent · ${int(tot.missed)} missed · ${int(tot.unwound)} unwound`} />
      <${Tile} label="Order latency" value=${live ? `${ms(live.latency.K.order_ms)} · ${ms(live.latency.P.order_ms)}` : "—"}
        foot="Kalshi · Polymarket US: seen → order at the book" />
    </div>
    <${ChartCard} title=${dry ? "Dry-run P&L over time" : "Paper P&L over time"} loading=${loading && data}
      sub="Cumulative, by when each trade was made: settled trades at their result, open ones at the profit they locked in"
      table=${{ columns: ["Time", "P&L"], rows: (data?.curve || []).slice().reverse().map((p) => [new Date(p[0] * 1000).toLocaleString(), money(p[1])]) }}>
      <${LineChart} series=${series} height=${200} zero=${0} zeroLabel="Break-even" yFmt=${(v) => money(v, Math.abs(v) < 10 ? 2 : 0)}
        xDomain=${data ? [data.since, now] : undefined} emptyText=${dry ? "No dry-run trades in this range yet" : "No paper trades in this range yet"} />
    <//>
    <div class="grid cols-2 align-start">
      <${Card} title="Where the expected profit went" sub=${dry ? "All dry-run trades so far" : "All paper trades so far"}>
        <dl class="facts">
          <dt>Expected when deciding</dt><dd class="num">${money(tot.planned_profit)}</dd>
          <dt>Taker fees paid</dt><dd class="num">${money(tot.fees)} <span class="muted">(already in the expectation)</span></dd>
          <dt>Lost unwinding one-sided fills</dt><dd class="num">${money(tot.unwind_loss)}</dd>
          <dt title="What settled trades really paid, against each contract pair paying $1">How the markets settled</dt>
          <dd class="num">${money(res.effect)} <span class="muted">(${int(res.settled)} settled${res.void ? ` · ${int(res.void)} voided` : ""}${res.conflict ? ` · ${int(res.conflict)} mismatched` : ""})</span></dd>
          <dt>P&L (settled + locked in)</dt><dd class="num"><b>${money(tot.pnl)}</b></dd>
        </dl>
      <//>
      ${dry ? html`<${DryLimits} live=${live} orders=${data?.orders} />` : html`<${Card} title="Order latency, as simulated" sub="Measured from this machine every few seconds, without placing orders" flush>
        <${LatencyTable} latency=${live?.latency} />
        <div class="muted" style="font-size:12px;padding:10px 14px">
          An order reaches the book half a round trip after we decide, and meets the book we see one feed delay later.
          Round trips are timed on Kalshi's balance endpoint and Polymarket US's order preview (which creates no order).
          ${data?.rules ? ` A pick ${ruleText(data.rules)}.` : ""}
        </div>
      <//>`}
    </div>
    <${Card} title=${dry ? "Dry-run trades" : "Paper trades"} sub=${data?.total > 300 ? "Newest 300 shown" : "Newest first; click a row for the pair"} flush>
      <${DataTable} rows=${trades} rowKey=${(t) => t.id} limit=${100} onRowClick=${(t) => navigate("pairs", { id: t.pair })}
        empty=${dry
          ? html`<${Empty} icon="play" title="No dry-run trades yet">When a pick passes the live limits, the dry run acts on it here and writes out its orders.<//>`
          : html`<${Empty} icon="play" title="No paper trades yet">When a pick appears, the paper trader acts on it here with the measured latency.<//>`}
        columns=${[
          { key: "ts", label: "Sent", render: (t) => html`<span class="nowrap">${dateTime(t.ts)}</span>` },
          { key: "k_title", label: "Pair", cls: "market", render: (t) => html`<${PairName} ...${t} />` },
          { key: "direction", label: "Buy", render: (t) => html`<span class="nowrap">${dirLabel(t.direction)}</span>` },
          { key: "planned_edge", label: "Edge seen", cls: "num", render: (t) => cents(t.planned_edge) },
          { key: "planned_size", label: "Filled", cls: "num", title: "Contracts filled on Kalshi / Polymarket US, of the pairs wanted",
            render: (t) => html`<span class="nowrap">${t.k_qty === t.p_qty ? int(t.k_qty) : html`${int(t.k_qty)}<span class="muted">/</span>${int(t.p_qty)}`}<span class="muted"> of ${int(t.planned_size)}</span></span>` },
          { key: "planned_profit", label: "Expected", cls: "num", render: (t) => money(t.planned_profit) },
          { key: "result", label: "Result", cls: "num", sortValue: (t) => (t.status === "settled" ? t.pnl : t.locked_profit),
            render: (t) => (t.status === "missed" ? "—" : html`<b>${money(t.status === "settled" ? t.pnl : t.locked_profit)}</b>`) },
          { key: "k_delay_ms", label: "Latency", cls: "num", sortable: false,
            render: (t) => html`<span class="nowrap muted" title="Kalshi / Polymarket US: decided → order at the book">${int(t.k_delay_ms)} / ${int(t.p_delay_ms)} ms</span>` },
          { key: "status", label: "", sortable: false, render: (t) => html`<${Status} t=${t} />` },
        ]} />
    <//>`;
}
