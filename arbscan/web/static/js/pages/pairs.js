import { html, useEffect, useState } from "../vendor/preact-htm.js";
import { api, cents, dateTime, days, dirLabel, duration, int, money, navigate, price, splitTitle, toast, useDebounced, useFetch, useNow } from "../lib.js";
import { ChartCard, LineChart } from "../charts.js";
import { Badge, Banner, Card, DataTable, DivBar, Drawer, Edge, Empty, Icon, Pager, PairName, RelationChip, Seg, Status } from "../ui.js";
import { Mapping, RulesCompare } from "../rules.js";

const STATUS = {
  live: { tone: "good", label: "Live" },
  paused: { tone: "warning", label: "Paused" },
  finished: { tone: "", label: "Finished" },
  pending: { tone: "", label: "Starting" },
};

function bestEdge(p) {
  const v = Object.values(p.edges || {}).filter((e) => e !== null && e !== undefined);
  return v.length ? Math.max(...v) : null;
}

const PAGE = 100;

export function Pairs({ params }) {
  const [q, setQ] = useState("");
  const [status, setStatus] = useState("all");
  const [rel, setRel] = useState("all");
  const [sort, setSort] = useState({ key: "best", dir: "desc" });
  const [offset, setOffset] = useState(0);
  const needle = useDebounced(q.trim());
  useEffect(() => setOffset(0), [needle, status, rel, sort]);
  // The server filters, sorts and pages: the full list runs to thousands of pairs.
  const query = new URLSearchParams({ status, relation: rel, q: needle, sort: sort.key, dir: sort.dir, offset, limit: PAGE });
  const { data, reload } = useFetch(`/api/pairs?${query}`, [], { refreshOn: (s) => Math.floor(s.pairsVersion / 5) });
  const counts = data?.counts || {};
  const rows = (data?.items || []).map((p) => ({ ...p, best: bestEdge(p) }));
  const max = 0.05; // fixed ±5¢ scale so bars compare across filters
  const prune = async () => {
    const r = await api("/api/pairs/remove", { method: "POST", body: { finished: true } });
    toast(`Removed ${r.removed} finished pairs from pairs.csv`);
    reload();
  };
  const columns = [
    { key: "k_title", label: "Market", cls: "market", render: (p) => html`<${PairName} ...${p} />` },
    { key: "relation", label: "Relation", render: (p) => html`<${RelationChip} relation=${p.relation} />` },
    { key: "k", label: "Kalshi YES / NO", cls: "num", sortable: false,
      render: (p) => html`<span class="nowrap">${price(p.k_yes_ask)} <span class="muted">/</span> ${price(p.k_no_ask)}</span>` },
    { key: "p", label: "Poly US bid / ask", cls: "num", sortable: false,
      render: (p) => html`<span class="nowrap">${price(p.p_bid)} <span class="muted">/</span> ${price(p.p_ask)}</span>` },
    { key: "best", label: "Best net edge", cls: "num",
      render: (p) => html`<div style="display:flex;align-items:center;gap:10px;justify-content:flex-end"><${DivBar} value=${p.best} max=${max} width=${72} /><${Edge} edge=${p.best} /></div>` },
    { key: "days", label: "Resolves in", cls: "num", render: (p) => days(p.days) },
    { key: "status", label: "Status", render: (p) => html`<${Status} tone=${STATUS[p.status]?.tone} pulse=${p.status === "live" && p.best > 0}>${STATUS[p.status]?.label || p.status}<//>` },
  ];
  return html`
    <div class="filters">
      <label class="search"><${Icon} name="search" size=${15} /><input class="input" placeholder="Search markets or tickers" value=${q} onInput=${(e) => setQ(e.target.value)} aria-label="Search pairs" /></label>
      <${Seg} label="Status" value=${status} onChange=${setStatus} options=${[
        { value: "all", label: `All ${int(counts.all)}` }, { value: "live", label: "Live" }, { value: "paused", label: "Paused" }, { value: "finished", label: "Finished" }]} />
      <${Seg} label="Relation" value=${rel} onChange=${setRel} options=${[
        { value: "all", label: "Any" }, { value: "same", label: "Same" }, { value: "inverse", label: "Inverse" }]} />
      <span class="spacer"></span>
      ${counts.finished > 0 && html`<button class="btn" onClick=${prune} title="Delete pairs whose markets have closed from pairs.csv"><${Icon} name="trash" size=${14} />Remove ${int(counts.finished)} finished</button>`}
    </div>
    <${Card} title="Watched pairs" sub="Live top of book from both venues. Edges are per $1 pair after both taker fees; the better of the two directions is shown." flush>
      ${data && html`<${DataTable} columns=${columns} rows=${rows} rowKey=${(p) => p.id} onRowClick=${(p) => navigate("pairs", { id: p.id })}
        sort=${sort} onSort=${setSort}
        footer=${html`<${Pager} offset=${offset} limit=${PAGE} total=${data.total} onChange=${setOffset} />`}
        empty=${html`<${Empty} icon="pairs" title=${counts.all ? "No pairs match these filters" : "No pairs yet"}>
          ${counts.all ? "" : "Jev approves matching markets as they're suggested, and you can add rows to pairs.csv. The scanner picks them up right away."}<//>`} />`}
    <//>
    ${params.id && html`<${PairDrawer} id=${params.id} onClose=${() => navigate("pairs")} onRemoved=${reload} />`}`;
}

function PairDrawer({ id, onClose, onRemoved }) {
  const [hours, setHours] = useState(24);
  const { data, loading } = useFetch(`/api/pair?id=${encodeURIComponent(id)}&hours=${hours}`, [], { refreshOn: (s) => Math.floor(s.pairsVersion / 3) });
  const now = useNow(1000);
  const live = data?.live;
  const k = data?.kalshi, p = data?.pm;
  const dirs = data?.relation === "inverse" ? ["K:YES+P:YES", "K:NO+P:NO"] : ["K:YES+P:NO", "K:NO+P:YES"];
  const series = dirs.map((d, i) => ({
    name: dirLabel(d), color: i ? "var(--series-2)" : "var(--series-1)", step: true,
    points: (data?.history || []).map((r) => [r[0], r[1 + i]]),
  }));
  const remove = async () => {
    await api("/api/pairs/remove", { method: "POST", body: { ids: [id] } });
    toast("Removed from pairs.csv");
    onRemoved();
    onClose();
  };
  const head = splitTitle(k?.title || id.split("|")[0]);
  return html`<${Drawer} title=${head.head} sub=${html`<span class="mono">${id.replace("|", "  ↔  ")}</span>`} onClose=${onClose}
    actions=${html`<button class="btn danger sm" onClick=${remove}><${Icon} name="trash" size=${13} />Stop watching</button>`}>
    ${!data ? html`<div class="muted">Loading…</div>` : html`
      <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        <${RelationChip} relation=${data.relation} />
        <${Status} tone=${STATUS[live?.status]?.tone} pulse=${live?.status === "live"}>${STATUS[live?.status]?.label || "Not scanned yet"}<//>
        ${live?.days != null && html`<${Badge} icon="clock">Resolves in ${days(live.days)}<//>`}
        ${(data.open || []).length > 0 && html`<${Badge} tone="good" icon="check">Profitable now<//>`}
      </div>
      ${data.relation && k && p && html`<${Mapping} relation=${data.relation} k=${k} p=${p} />`}
      <div class="grid cols-2">
        ${dirs.map((d) => html`<div class="card tile">
          <div class="label">${dirLabel(d)}</div>
          <div class="value"><${Edge} edge=${live?.edges?.[d]} strong /></div>
          <div class="foot">net per $1 pair, top of book</div>
        </div>`)}
      </div>
      <${ChartCard} title="Net edge history" sub="Top-of-book edge after fees for each direction; recorded whenever quotes change"
        loading=${loading}
        actions=${html`<${Seg} label="History range" value=${hours} onChange=${setHours} options=${[{ value: 1, label: "1h" }, { value: 6, label: "6h" }, { value: 24, label: "24h" }, { value: 168, label: "7d" }]} />`}
        table=${{ columns: ["Time", dirLabel(dirs[0]), dirLabel(dirs[1])], rows: (data.history || []).slice().reverse().map((r) => [new Date(r[0] * 1000).toLocaleString(), cents(r[1]), cents(r[2])]) }}>
        <${LineChart} series=${series} height=${220} zero=${0} zeroLabel="Breakeven" yFmt=${(v) => cents(v, { digits: 1 })}
          xDomain=${[data.since, now]} emptyText="No quotes recorded in this range" />
      <//>
      <${Card} title="Profitable windows" sub="Closed episodes for this pair" flush>
        ${data.episodes.length ? html`<${DataTable} rowKey=${(e) => e.start_ts + e.direction} rows=${data.episodes}
          columns=${[
            { key: "start_ts", label: "Opened", render: (e) => dateTime(e.start_ts) },
            { key: "direction", label: "Direction", render: (e) => dirLabel(e.direction) },
            { key: "max_top_edge", label: "Peak edge", cls: "num", render: (e) => cents(e.max_top_edge) },
            { key: "max_profit", label: "Peak profit", cls: "num", render: (e) => money(e.max_profit) },
            { key: "dur", label: "Lasted", cls: "num", sortValue: (e) => e.end_ts - e.start_ts, render: (e) => duration(e.end_ts - e.start_ts) },
          ]} />` : html`<${Empty} icon="zap" title="None yet">This pair hasn't been profitable after fees while the scanner was watching.<//>`}
      <//>
      ${k && p ? html`<${RulesCompare} k=${k} p=${p} />` : html`<${Banner} tone="info">Market details appear once the catalog includes both markets.<//>`}
    `}
  <//>`;
}
