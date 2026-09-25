import { html } from "../vendor/preact-htm.js";
import { cents, dateTime, days, dirLabel, duration, int, money, navigate, perYear, ruleText, useFetch, usePref, useStore } from "../lib.js";
import { ChartCard, ColumnChart } from "../charts.js";
import { Badge, Card, DataTable, Empty, PairName, Seg, Tile } from "../ui.js";
import { RANGES } from "./overview.js";

const SUSPICIOUS_DURATION = 3600;
const WHY = { "suspicious edge": "Edge too big", "too short": "Too short", "resolves too late": "Resolves too late", "low return": "Low return" };

export function Opportunities() {
  const [hours, setHours] = usePref("range", 24);
  const [view, setView] = usePref("opps.view", "picks");
  const { data, loading } = useFetch(`/api/opportunities?hours=${hours}&view=${view}`, [], { refreshOn: (s) => Math.floor(s.pairsVersion / 5) });
  const eps = data?.episodes || [];
  const bankroll = useStore((s) => s.state?.bankroll);
  const picks = view === "picks";
  const durations = eps.map((e) => e.end_ts - e.start_ts).sort((a, b) => a - b);
  const profit = eps.reduce((a, e) => a + (e.max_profit || 0), 0);
  const capital = eps.reduce((a, e) => a + (e.cost_at_max || 0), 0);
  const long = eps.filter((e) => e.end_ts - e.start_ts >= SUSPICIOUS_DURATION).length;
  const durData = (data?.durations || []).map((d) => ({ label: d.label, value: d.count }));
  const edgeData = (data?.edges || []).map((d) => ({ label: d.label, value: d.count, profit: d.profit }));
  const counts = data?.counts || {};
  return html`
    <div class="filters">
      <${Seg} label="Time range" options=${RANGES} value=${hours} onChange=${setHours} />
      <${Seg} label="Which windows" value=${view} onChange=${setView} options=${[
        { value: "picks", label: `Picks ${counts.picks != null ? int(counts.picks) : ""}` },
        { value: "all", label: `All windows ${counts.all != null ? int(counts.all) : ""}` }]} />
      <span class="muted" style="font-size:12.5px">${data?.rules ? `A pick ${ruleText(data.rules)}. Ranked by return per year of lock-up.` : ""}</span>
    </div>
    <div class="kpis">
      <${Tile} label=${picks ? "Picks" : "Profitable windows"} value=${int(data?.total)}
        foot=${picks ? `of ${int(counts.all)} profitable windows${durations.length ? ` · median ${duration(durations[durations.length >> 1])} open` : ""}`
          : durations.length ? `median ${duration(durations[durations.length >> 1])} open` : "none in this range"} />
      <${Tile} label="Best-case profit" value=${money(profit)}
        foot=${bankroll ? `each window at its peak, sized to your ${money(bankroll, 0)}` : "each window caught once at its peak"} />
      <${Tile} label=${bankroll ? "Capital, summed over windows" : "Capital needed"} value=${money(capital, 0)}
        title=${bankroll ? "Each window uses at most your bankroll; windows overlap in time, so one bankroll can't fund all of them. The Overview's bankroll tile simulates that." : ""}
        foot=${capital ? `${((100 * profit) / capital).toFixed(2)}% return before slippage` : "—"} />
      <${Tile} label="Open an hour or more" value=${int(long)} foot="a gap that lasts is often a rules mismatch" />
    </div>
    <div class="grid cols-2">
      <${ChartCard} title="How long windows stay open" sub="Shorter than a second is too fast to act on" loading=${loading && data}
        table=${{ columns: ["Duration", "Windows"], rows: durData.map((d) => [d.label, d.value]) }}>
        <${ColumnChart} data=${durData} height=${200} yFmt=${(v) => int(v)} emptyText="No windows in this range" />
      <//>
      <${ChartCard} title="How big the edge got" sub="Peak net edge per window, per $1 pair after fees" loading=${loading && data}
        table=${{ columns: ["Peak edge", "Windows", "Best-case profit"], rows: edgeData.map((d) => [d.label, d.value, money(d.profit)]) }}>
        <${ColumnChart} data=${edgeData} height=${200} yFmt=${(v) => int(v)} emptyText="No windows in this range"
          tooltip=${(d) => html`<div class="t-row"><span class="key-rect" style="background:var(--series-1)"></span><b>${int(d.value)}</b><span>windows</span></div>
            <div class="t-row"><span></span><b>${money(d.profit)}</b><span>best case</span></div>`} />
      <//>
    </div>
    <${Card} title=${picks ? "Picks" : "Windows"} sub=${data?.total > 500 ? "Newest 500 shown" : "Newest first; click a row for the pair"} flush>
      <${DataTable} rows=${eps} rowKey=${(e) => e.pair + e.direction + e.start_ts} limit=${100}
        onRowClick=${(e) => navigate("pairs", { id: e.pair })}
        empty=${html`<${Empty} icon="zap" title=${picks ? "No picks in this range" : "No profitable windows yet"}>
          ${picks ? `${int(counts.all)} windows were profitable, but none met the pick rules.` : "When a watched pair becomes profitable after both fees, it shows up here with its size, duration and the capital it needs."}<//>`}
        columns=${[
          { key: "start_ts", label: "Opened", render: (e) => html`<span class="nowrap">${dateTime(e.start_ts)}</span>` },
          { key: "k_title", label: "Pair", cls: "market", render: (e) => html`<${PairName} ...${e} />` },
          { key: "direction", label: "Buy", render: (e) => html`<span class="nowrap">${dirLabel(e.direction)}</span>` },
          { key: "max_top_edge", label: "Peak edge", cls: "num", render: (e) => cents(e.max_top_edge) },
          { key: "max_profit", label: "Profit", cls: "num", render: (e) => money(e.max_profit) },
          { key: "cost_at_max", label: "Capital", cls: "num", render: (e) => money(e.cost_at_max, 0) },
          { key: "days_to_resolve", label: "Resolves", cls: "num", render: (e) => days(e.days_to_resolve) },
          { key: "rate", label: "Return/yr", cls: "num", render: (e) => perYear(e.rate, { suffix: "" }) },
          { key: "dur", label: "Open", cls: "num", sortValue: (e) => e.end_ts - e.start_ts, render: (e) => duration(e.end_ts - e.start_ts) },
          { key: "why", label: "", sortable: false, render: (e) => e.why
            ? html`<span class="muted nowrap" style="font-size:12px">${WHY[e.why] || e.why}</span>`
            : e.end_ts - e.start_ts >= SUSPICIOUS_DURATION ? html`<${Badge} tone="warning" icon="alert">Check rules<//>`
            : picks ? "" : html`<${Badge} tone="good" icon="check">Pick<//>` },
        ]} />
    <//>`;
}
