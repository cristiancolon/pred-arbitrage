import { html } from "../vendor/preact-htm.js";
import { cents, dateTime, dirLabel, int, money, navigate, ruleText, useFetch, useNow, usePref } from "../lib.js";
import { ChartCard, LineChart } from "../charts.js";
import { Badge, Banner, Card, DataTable, Empty, PairName, Seg, Tile } from "../ui.js";
import { RANGES } from "./overview.js";

const VENUE = { K: "Kalshi", P: "Polymarket US" };
const ms = (v) => (v == null ? "—" : `${Math.round(v)} ms`);

function Status({ t }) {
  if (t.status === "missed") return html`<span class="muted nowrap" style="font-size:12px">No fill</span>`;
  if (t.status === "open") return html`<${Badge} icon="clock">${t.note ? "Open · unhedged" : "Held to resolution"}<//>`;
  if (t.note === "unwound") return html`<${Badge} tone="warning" icon="alert">Unwound<//>`;
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

export function Paper() {
  const [hours, setHours] = usePref("range", 24);
  const now = useNow(5000);
  const { data, loading } = useFetch(`/api/paper?hours=${hours}`, [], { refreshOn: (s) => Math.floor(s.pairsVersion / 5) });
  const live = data?.live;
  const tot = data?.totals || {};
  const trades = data?.trades || [];
  if (data && !live) {
    return html`<${Banner} tone="info">Paper trading runs with the streaming scanner. Set both venues' API keys and
      <code>paper_trading = true</code> in config.toml.<//>`;
  }
  const cash = live ? live.cash.K + live.cash.P : null;
  const tied = live ? live.tied.K + live.tied.P : null;
  const pnl = live ? live.realized + live.locked : null;
  const series = [{ name: "P&L", color: "var(--series-1)", step: true, points: data?.curve || [] }];
  return html`
    <div class="filters">
      <${Seg} label="Time range" options=${RANGES} value=${hours} onChange=${setHours} />
      <span class="muted" style="font-size:12.5px">Simulated: no orders are placed. Each pick is filled against the live books as they stood when its orders would have arrived.</span>
    </div>
    <div class="kpis">
      <${Tile} label="Paper P&L" value=${money(pnl)}
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
    <${ChartCard} title="Paper P&L over time" loading=${loading && data}
      sub="Cumulative, by when each trade was made: settled trades at their result, open ones at the profit they locked in"
      table=${{ columns: ["Time", "P&L"], rows: (data?.curve || []).slice().reverse().map((p) => [new Date(p[0] * 1000).toLocaleString(), money(p[1])]) }}>
      <${LineChart} series=${series} height=${200} zero=${0} zeroLabel="Break-even" yFmt=${(v) => money(v, Math.abs(v) < 10 ? 2 : 0)}
        xDomain=${data ? [data.since, now] : undefined} emptyText="No paper trades in this range yet" />
    <//>
    <div class="grid cols-2 align-start">
      <${Card} title="Where the expected profit went" sub="All paper trades so far">
        <dl class="facts">
          <dt>Expected when deciding</dt><dd class="num">${money(tot.planned_profit)}</dd>
          <dt>Taker fees paid</dt><dd class="num">${money(tot.fees)} <span class="muted">(already in the expectation)</span></dd>
          <dt>Lost unwinding one-sided fills</dt><dd class="num">${money(tot.unwind_loss)}</dd>
          <dt>P&L (settled + locked in)</dt><dd class="num"><b>${money(tot.pnl)}</b></dd>
        </dl>
      <//>
      <${Card} title="Order latency, as simulated" sub="Measured from this machine every few seconds, without placing orders" flush>
        <${LatencyTable} latency=${live?.latency} />
        <div class="muted" style="font-size:12px;padding:10px 14px">
          An order reaches the book half a round trip after we decide, and meets the book we see one feed delay later.
          Round trips are timed on Kalshi's balance endpoint and Polymarket US's order preview (which creates no order).
          ${data?.rules ? ` A pick ${ruleText(data.rules)}.` : ""}
        </div>
      <//>
    </div>
    <${Card} title="Paper trades" sub=${data?.total > 300 ? "Newest 300 shown" : "Newest first; click a row for the pair"} flush>
      <${DataTable} rows=${trades} rowKey=${(t) => t.id} limit=${100} onRowClick=${(t) => navigate("pairs", { id: t.pair })}
        empty=${html`<${Empty} icon="play" title="No paper trades yet">When a pick appears, the paper trader acts on it here with the measured latency.<//>`}
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
