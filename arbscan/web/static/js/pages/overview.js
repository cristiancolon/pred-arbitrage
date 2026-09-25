import { html } from "../vendor/preact-htm.js";
import { ago, api, cents, compact, dirLabel, duration, int, money, navigate, toast, useFetch, useNow, usePref, useStore } from "../lib.js";
import { ChartCard, ColumnChart, LineChart, Sparkline } from "../charts.js";
import { Card, DivBar, Edge, Empty, Icon, PairName, RelationChip, Seg, Status, Tile } from "../ui.js";

export const RANGES = [
  { value: 1, label: "1h" }, { value: 6, label: "6h" }, { value: 24, label: "24h" }, { value: 168, label: "7d" },
];

function Stage({ icon, name, value, sub, foot, action, active, progress }) {
  return html`<div class=${`stage ${active ? "active" : ""}`}>
    <div class="stage-top">
      <div class="stage-icon"><${Icon} name=${icon} size=${17} /></div>
      <div class="stage-name">${name}</div>
      <div class="stage-action">${action}</div>
    </div>
    <div class="stage-value">${value}</div>
    <div class="stage-sub">${sub}</div>
    <div class="stage-foot">${foot}</div>
    ${progress && html`<div class="stage-progress"></div>`}
  </div>`;
}

export function Pipeline() {
  const s = useStore((st) => st.state);
  const job = useStore((st) => st.job);
  const now = useNow(1000);
  if (!s) return html`<div class="card" style="height:176px"></div>`;
  const p = s.pipeline || {};
  const cat = p.catalog || { kalshi: {}, pm: {} };
  const running = job?.state === "running";
  const catUpdated = Math.max(cat.kalshi?.updated || 0, cat.pm?.updated || 0) || null;
  const stale = !catUpdated || now - catUpdated > (job?.interval_s || 21600) * 2;
  const sc = s.scanner;
  const last = sc.last;
  const fresh = last && now - last.ts < Math.max(15, sc.poll_interval_s * 4);
  const streaming = sc.mode === "stream";
  const feedsUp = streaming && Object.values(sc.feeds || {}).every((f) => f.connected);
  const refresh = async () => {
    const r = await api("/api/jobs/refresh", { method: "POST" });
    toast(r.started ? `Refresh started: ${(job?.stages || ["catalog", "match"]).join(", then ")}` : "A refresh is already running");
  };
  const rev = p.review || {};
  const jevOn = s.features?.jev;
  const liveDisc = s.discovery?.state === "running";
  const discHour = Object.values(p.discovery?.hour || {}).reduce((a, b) => a + b, 0);
  const jv = rev.jev || {};
  const rep = p.report || {};
  const openNow = (s.open || []).length;
  const stageRunning = (name) => running && job.stage === name;
  return html`<section class="card pipeline" aria-label="Pipeline">
    <${Stage} icon="database" name="Catalog" active=${stageRunning("catalog")} progress=${stageRunning("catalog")}
      action=${html`<button class="btn ghost sm" onClick=${refresh} disabled=${running} title="Re-download markets and re-run matching">
        <${Icon} name="jobs" size=${13} />Refresh</button>`}
      value=${compact((cat.kalshi?.count || 0) + (cat.pm?.count || 0))}
      sub=${`${compact(cat.kalshi?.count)} Kalshi · ${compact(cat.pm?.count)} Poly US`}
      foot=${stageRunning("catalog")
        ? html`<${Status} tone="accent" pulse>Downloading · ${duration(now - job.stage_started)}<//>`
        : liveDisc ? html`<${Status} tone="good" pulse>Live · ${int(discHour)} new in 1 h<//>`
        : html`<${Status} tone=${stale ? "warning" : "good"}>Updated ${ago(catUpdated, now)}<//>`} />
    <${Stage} icon="match" name="Match" active=${stageRunning("match")} progress=${stageRunning("match")}
      value=${int(p.match?.candidates)} sub=${`suggested pairs · ${int(p.match?.confident)} with matching labels`}
      foot=${stageRunning("match")
        ? html`<${Status} tone="accent" pulse>Matching · ${duration(now - job.stage_started)}<//>`
        : html`<${Status} tone=${p.match?.updated ? "good" : ""}>Updated ${ago(p.match?.updated, now)}<//>`} />
    <${Stage} icon="review" name="Review" active=${stageRunning("review") || (!jevOn && rev.pending > 0)} progress=${stageRunning("review")}
      action=${html`<a class="btn ghost sm" href="#/review">Open<${Icon} name="arrow" size=${13} /></a>`}
      value=${int(rev.pending)}
      sub=${jevOn ? `pending · ${int(jv.unsure)} Jev unsure · ${int(jv.unreviewed)} not read yet` : "awaiting your review"}
      foot=${stageRunning("review")
        ? html`<${Status} tone="accent" pulse>Jev reviewing · ${duration(now - job.stage_started)}<//>`
        : html`<span class="muted" style="font-size:12px">${int(rev.approved)} approved (${int(rev.auto)} by rules${jevOn ? `, ${int(jv.approved)} by Jev` : ""}) · ${int(rev.rejected)} rejected</span>`} />
    <${Stage} icon="radar" name="Scan" active=${fresh && sc.pairs.total > 0}
      value=${html`${int(sc.pairs.live)}<span class="muted" style="font-size:15px;font-weight:500"> / ${int(sc.pairs.total)}</span>`}
      sub=${`pairs live${sc.pairs.finished ? ` · ${sc.pairs.finished} finished` : ""}${sc.pairs.paused ? ` · ${sc.pairs.paused} paused` : ""}`}
      foot=${sc.pairs.total === 0 ? html`<${Status}>Idle until pairs are approved<//>`
        : streaming && fresh ? html`<${Status} tone=${feedsUp ? "good" : "warning"} pulse=${feedsUp}>${feedsUp
            ? `Streaming · ${last.dur_ms != null ? `${last.dur_ms} ms behind the exchanges` : "waiting for updates"}`
            : `Reconnecting: ${Object.entries(sc.feeds || {}).filter(([, f]) => !f.connected).map(([n]) => (n === "pmus" ? "Polymarket" : "Kalshi")).join(", ")}`}<//>`
        : fresh ? html`<${Status} tone="good" pulse>Sweeping every ${sc.poll_interval_s}s · ${(last.dur_ms / 1000).toFixed(1)}s each<//>`
        : html`<${Status} tone="warning">Last sweep ${ago(last?.ts, now)}<//>`} />
    <${Stage} icon="chart" name="Report" active=${openNow > 0}
      action=${html`<a class="btn ghost sm" href="#/opportunities">Open<${Icon} name="arrow" size=${13} /></a>`}
      value=${int(rep.windows_24h)} sub="profitable windows in 24h"
      foot=${html`<span class="muted" style="font-size:12px">${money(rep.profit_24h)} best case · ${openNow} open now</span>`} />
  </section>`;
}

function OpenList({ open, now }) {
  if (!open.length) return null;
  return html`<div class="list">${open.map((o) => html`<div class="list-row clickable" key=${o.pair + o.direction}
      style="grid-template-columns:minmax(0,1fr) auto auto" onClick=${() => navigate("pairs", { id: o.pair })}>
    <div><${PairName} ...${o} /><div class="muted" style="font-size:12px;margin-top:2px">${dirLabel(o.direction)} · open ${duration(now - o.start_ts)}</div></div>
    <div class="cell-2" style="text-align:right"><${Edge} edge=${o.edge} strong /><span class="muted num" style="font-size:12px">${int(o.size)} contracts</span></div>
    <div class="cell-2" style="text-align:right;min-width:84px"><b class="num">${money(o.profit)}</b><span class="muted num" style="font-size:12px">on ${money(o.cost, 0)}</span></div>
  </div>`)}</div>`;
}

function Closest({ rows }) {
  if (!rows.length) return html`<${Empty} icon="radar" title="Nothing to rank yet">Approve some pairs and the scanner will rank them by how close they are to breakeven.<//>`;
  const max = Math.max(0.02, ...rows.map((r) => Math.abs(r.edge)));
  return html`<div class="list">${rows.map((r) => html`<div class="list-row clickable rank-row" key=${r.pair + r.direction}
      style="grid-template-columns:minmax(0,1fr) 120px 76px" onClick=${() => navigate("pairs", { id: r.pair })}>
    <div style="min-width:0"><${PairName} ...${r} /><div style="margin-top:3px;display:flex;gap:6px;align-items:center"><${RelationChip} relation=${r.relation} /><span class="muted nowrap" style="font-size:12px;overflow:hidden;text-overflow:ellipsis">${dirLabel(r.direction)}</span></div></div>
    <${DivBar} value=${r.edge} max=${max} width=${120} />
    <div style="text-align:right"><${Edge} edge=${r.edge} /></div>
  </div>`)}</div>`;
}

export function Overview() {
  const [hours, setHours] = usePref("range", 24);
  const s = useStore((st) => st.state);
  const now = useNow(1000);
  const { data, loading } = useFetch(`/api/overview?hours=${hours}`, [], { refreshOn: (st) => Math.floor(st.pairsVersion / 5) });
  const k = data?.kpi || {};
  const closest = s?.closest || [];
  const open = s?.open || [];
  const best = closest[0];
  const edgeSeries = [{ name: "Best net edge across all pairs", color: "var(--series-1)", points: data?.edge || [] }];
  const profit = (data?.profit || []).map((b) => ({ ...b, value: b.profit }));
  const bucketFmt = (d) => new Date(d.ts * 1000).toLocaleString(undefined,
    data?.profit_bucket_s >= 86400 ? { month: "short", day: "numeric" } : { hour: "2-digit", minute: "2-digit" });
  return html`
    <div class="filters">
      <${Seg} label="Time range" options=${RANGES} value=${hours} onChange=${setHours} />
      <span class="muted" style="font-size:12.5px">Charts and totals cover the selected range; live panels show right now.</span>
    </div>

    <${Pipeline} />

    <div class="kpis">
      <${Tile} label="Closest to breakeven now" value=${best ? cents(best.edge) : "—"}
        foot=${best ? html`<span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${(best.k_title || "").split(" | ")[0]}</span>` : "per $1 pair, after both fees"} />
      <${Tile} label="Profitable windows" value=${int(k.windows)}
        foot=${k.median_duration != null ? `median ${duration(k.median_duration)} open` : "none in this range"} />
      ${data?.sim ? html`<${Tile} label=${`With your ${money(data.sim.bankroll, 0)}`} value=${money(data.sim.profit)}
          title=${`Simulated best case for one bankroll: windows taken in the order they appeared, skipping any open under ${data.sim.min_window_s} s or returning under ${Math.round(data.sim.min_annualized * 100)}% a year; each stake stays tied up until its market resolves. Assumes both legs fill at the quoted prices.`}
          foot=${`${int(data.sim.taken)} windows taken · ${money(data.sim.tied_up, 0)} still tied up`} />`
        : html`<${Tile} label="Best-case profit" value=${money(k.profit)}
          foot=${`on ${money(k.capital, 0)} of capital`} title="If every window were caught once at its peak, before slippage" />`}
      ${s?.scanner?.mode === "stream"
        ? html`<${Tile} label="Latency" value=${k.avg_sweep_ms != null ? `${Math.round(k.avg_sweep_ms)} ms` : "—"}
            title="Median time from the exchange's timestamp on a book update to the edge being computed here"
            foot=${html`<span>exchange → edge computed${k.errors ? ` · ${k.errors} reconnects` : ""}</span><${Sparkline} values=${(data?.latency || []).slice(-40).map((p) => p[1])} />`} />`
        : html`<${Tile} label="Sweep time" value=${k.avg_sweep_ms != null ? `${(k.avg_sweep_ms / 1000).toFixed(2)}s` : "—"}
            foot=${html`<span>${int(k.sweeps)} sweeps${k.errors ? ` · ${k.errors} API errors` : ""}</span><${Sparkline} values=${(data?.latency || []).slice(-40).map((p) => p[1])} />`} />`}
    </div>

    <${ChartCard} title="How close the market got to an arb"
      sub="Best top-of-book edge of any watched pair after both taker fees, per interval. Above the line means profitable."
      loading=${loading && data}
      table=${{ columns: ["Time", "Best net edge"], rows: (data?.edge || []).slice().reverse().map((p) => [new Date(p[0] * 1000).toLocaleString(), cents(p[1])]) }}>
      <${LineChart} series=${edgeSeries} height=${240} area endLabel zero=${0} zeroLabel="Breakeven after fees"
        yFmt=${(v) => cents(v, { digits: 1 })} xDomain=${data ? [data.since, now] : undefined}
        emptyText=${s?.scanner?.pairs?.total ? "Waiting for the first sweeps…" : "No pairs are being watched yet. Approve some in Review."} />
    <//>

    <div class="grid cols-2 align-start">
      <${Card} title="Open opportunities" sub="Profitable after fees right now, walked through both order books" flush
        actions=${open.length ? html`<span class="badge accent">${open.length} open</span>` : null}>
        ${open.length ? html`<${OpenList} open=${open} now=${now} />` : html`<${Empty} icon="zap" title="No open windows">
          ${best ? html`The closest pair is ${cents(best.edge)} from breakeven.` : "Nothing is profitable after fees right now."}<//>`}
      <//>
      <${Card} title="Closest to breakeven" sub="Every watched pair's best direction, right now" flush>
        <${Closest} rows=${closest} />
      <//>
    </div>

    <${ChartCard} title="Best-case profit by period"
      sub=${`Sum of each window's peak profit${data?.sim ? `, each sized to your ${money(data.sim.bankroll, 0)}` : ""}, grouped by ${data?.profit_bucket_s >= 86400 ? "day" : data?.profit_bucket_s >= 21600 ? "6 hours" : "hour"}. Windows overlap, so this adds up more than one bankroll could make.`}
      loading=${loading && data}
      table=${{ columns: ["Period", "Windows", "Best-case profit", "Capital"], rows: profit.slice().reverse().map((b) => [bucketFmt(b), b.windows, money(b.profit), money(b.capital, 0)]) }}>
      <${ColumnChart} data=${profit} height=${200} yFmt=${(v) => money(v, v < 10 ? 2 : 0)} xFmt=${bucketFmt}
        emptyText="No profitable windows in this range"
        tooltip=${(d) => html`<div class="t-row"><span class="key-rect" style="background:var(--series-1)"></span><b>${money(d.profit)}</b><span>best case</span></div>
          <div class="t-row"><span></span><b>${d.windows}</b><span>windows · ${money(d.capital, 0)} capital</span></div>`} />
    <//>`;
}
