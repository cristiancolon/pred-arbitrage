// Small SVG charts: line (with crosshair + tooltip), column, sparkline.
// Mark specs: 2px lines, thin columns with 4px rounded data-ends, hairline grid,
// text in ink tokens (never the series color), a table view for every chart.
import { html, useEffect, useRef, useState } from "./vendor/preact-htm.js";
import { Card, Icon } from "./ui.js";

function useWidth(ref) {
  const [w, setW] = useState(0);
  useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(([e]) => setW(Math.floor(e.contentRect.width)));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  return w;
}

function niceStep(span, count) {
  const raw = span / Math.max(1, count);
  const mag = 10 ** Math.floor(Math.log10(raw));
  const n = raw / mag;
  return (n >= 5 ? 10 : n >= 2 ? 5 : n >= 1 ? 2 : 1) * mag;
}

function niceDomain(lo, hi, count = 4) {
  if (lo === hi) { lo -= Math.abs(lo) * 0.5 || 1; hi += Math.abs(hi) * 0.5 || 1; }
  const step = niceStep(hi - lo, count);
  const a = Math.floor(lo / step) * step;
  const b = Math.ceil(hi / step) * step;
  const ticks = [];
  for (let v = a; v <= b + step / 2; v += step) ticks.push(Math.abs(v) < step / 1e6 ? 0 : v);
  return { lo: a, hi: b, ticks };
}

const TIME_STEPS = [60, 300, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 604800];
function timeTicks(t0, t1, maxTicks) {
  const step = TIME_STEPS.find((s) => (t1 - t0) / s <= maxTicks) || 604800;
  const off = -new Date().getTimezoneOffset() * 60;
  const ticks = [];
  for (let t = Math.ceil((t0 + off) / step) * step - off; t <= t1; t += step) ticks.push(t);
  const fmt = step >= 86400
    ? (t) => new Date(t * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" })
    : (t) => new Date(t * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  return { ticks, fmt };
}

function fmtTooltipTime(t) {
  return new Date(t * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

// Value of a series at time t: last point at or before t for step series,
// nearest point otherwise.
function valueAt(s, t) {
  const pts = s.points;
  let lo = 0, hi = pts.length - 1;
  if (hi < 0) return null;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (pts[mid][0] <= t) lo = mid; else hi = mid - 1;
  }
  if (s.step) return pts[lo][0] <= t ? pts[lo] : null;
  const next = pts[Math.min(lo + 1, pts.length - 1)];
  return Math.abs(next[0] - t) < Math.abs(pts[lo][0] - t) ? next : pts[lo];
}

function Tooltip({ x, y, width, children }) {
  const left = x > width - 190 ? x - 184 : x + 14;
  return html`<div class="tooltip" style=${`left:${Math.max(0, left)}px;top:${Math.max(0, y)}px`}>${children}</div>`;
}

export function LineChart({ series, height = 220, yFmt = (v) => v, xDomain, zero, zeroLabel, area = false, endLabel = false, emptyText = "No data yet" }) {
  const ref = useRef();
  const w = useWidth(ref);
  const [hover, setHover] = useState(null);
  const all = series.flatMap((s) => s.points.filter((p) => p[1] !== null && p[1] !== undefined));
  const legend = series.length >= 2 && html`<div class="legend">${series.map((s) =>
    html`<span><span class="key-line" style=${`background:${s.color}`}></span>${s.name}</span>`)}</div>`;
  if (!all.length) {
    return html`<div ref=${ref} class="chart-wrap">${legend}<div class="chart-empty" style=${`height:${height}px`}>${emptyText}</div></div>`;
  }
  const pad = { l: 56, r: endLabel ? 64 : 14, t: 10, b: 26 };
  const iw = Math.max(10, w - pad.l - pad.r), ih = height - pad.t - pad.b;
  const [t0, t1] = xDomain || [Math.min(...all.map((p) => p[0])), Math.max(...all.map((p) => p[0]))];
  const vals = all.map((p) => p[1]).concat(zero !== undefined ? [zero] : []);
  const y = niceDomain(Math.min(...vals), Math.max(...vals), Math.max(2, Math.floor(ih / 44)));
  const sx = (t) => pad.l + ((t - t0) / Math.max(1, t1 - t0)) * iw;
  const sy = (v) => pad.t + ih - ((v - y.lo) / (y.hi - y.lo || 1)) * ih;
  const xt = timeTicks(t0, t1, Math.max(2, Math.floor(iw / 110)));

  const path = (s) => {
    let d = "", prev = null;
    for (const [t, v] of s.points) {
      if (v === null || v === undefined) { prev = null; continue; }
      const X = sx(t).toFixed(1), Y = sy(v).toFixed(1);
      if (prev === null) d += `M${X},${Y}`;
      else d += s.step ? `H${X}V${Y}` : `L${X},${Y}`;
      prev = v;
    }
    if (s.step && prev !== null) d += `H${sx(t1).toFixed(1)}`;
    return d;
  };

  const onMove = (e) => {
    const r = e.currentTarget.getBoundingClientRect();
    const px = Math.min(Math.max(e.clientX - r.left, pad.l), pad.l + iw);
    const t = t0 + ((px - pad.l) / iw) * (t1 - t0);
    // Line series snap to the nearest point that has a value; step series hold
    // their last value, so a gap (null) there really means "no data".
    const rows = series.map((s) => ({ s, p: valueAt(s.step ? s : { ...s, points: s.points.filter((q) => q[1] !== null && q[1] !== undefined) }, t) }));
    const snapT = series.length === 1 && rows[0].p ? rows[0].p[0] : t;
    setHover({ x: sx(snapT), t: snapT, rows, py: e.clientY - r.top });
  };

  return html`<div ref=${ref} class="chart-wrap">
    ${legend}
    <div class="chart" style=${`height:${height}px`}>
      ${w > 0 && html`<svg width=${w} height=${height} onPointerMove=${onMove} onPointerLeave=${() => setHover(null)}
          role="img" aria-label=${series.map((s) => s.name).join(", ")}>
        ${y.ticks.map((v) => html`<g>
          <line class=${zero !== undefined && v === zero ? "zero" : "gridline"} x1=${pad.l} x2=${pad.l + iw} y1=${sy(v)} y2=${sy(v)} />
          <text class="tick" x=${pad.l - 8} y=${sy(v) + 4} text-anchor="end">${yFmt(v)}</text></g>`)}
        ${zero !== undefined && !y.ticks.includes(zero) && html`<line class="zero" x1=${pad.l} x2=${pad.l + iw} y1=${sy(zero)} y2=${sy(zero)} />`}
        ${zero !== undefined && zeroLabel && html`<text class="zero-label" x=${pad.l + iw - 4} y=${sy(zero) - 6} text-anchor="end">${zeroLabel}</text>`}
        <line class="baseline" x1=${pad.l} x2=${pad.l + iw} y1=${pad.t + ih} y2=${pad.t + ih} />
        ${xt.ticks.map((t) => html`<text class="tick" x=${sx(t)} y=${height - 6} text-anchor="middle">${xt.fmt(t)}</text>`)}
        ${area && series.length === 1 && html`<path d=${`${path(series[0])}V${pad.t + ih}H${sx(series[0].points.find((p) => p[1] !== null)?.[0] ?? t0)}Z`}
          fill=${series[0].color} opacity="0.1" />`}
        ${series.map((s) => html`<path d=${path(s)} fill="none" stroke=${s.color} stroke-width="2" stroke-linejoin="round" stroke-linecap="round" />`)}
        ${series.map((s) => {
          const last = [...s.points].reverse().find((p) => p[1] !== null && p[1] !== undefined);
          if (!last) return null;
          const x = s.step ? sx(t1) : sx(last[0]);
          return html`<g>
            <circle cx=${x} cy=${sy(last[1])} r="4" fill=${s.color} stroke="var(--surface-1)" stroke-width="2" />
            ${endLabel && series.length === 1 && html`<text class="val-label" x=${x + 9} y=${sy(last[1]) + 4}>${yFmt(last[1])}</text>`}
          </g>`;
        })}
        ${hover && html`<g>
          <line class="crosshair" x1=${hover.x} x2=${hover.x} y1=${pad.t} y2=${pad.t + ih} />
          ${hover.rows.filter((r) => r.p && r.p[1] !== null).map((r) => html`<circle cx=${hover.x} cy=${sy(r.p[1])} r="4.5"
            fill=${r.s.color} stroke="var(--surface-1)" stroke-width="2" />`)}
        </g>`}
      </svg>`}
      ${hover && html`<${Tooltip} x=${hover.x} y=${Math.max(0, hover.py - 70)} width=${w}>
        <div class="t-head">${fmtTooltipTime(hover.t)}</div>
        ${hover.rows.map((r) => html`<div class="t-row"><span class="key-line" style=${`background:${r.s.color}`}></span>
          <b>${r.p && r.p[1] !== null ? yFmt(r.p[1]) : "—"}</b><span>${r.s.name}</span></div>`)}
      <//>`}
    </div>
  </div>`;
}

export function ColumnChart({ data, height = 200, yFmt = (v) => v, color = "var(--series-1)", tooltip, xFmt = (d) => d.label, emptyText = "No data yet" }) {
  const ref = useRef();
  const w = useWidth(ref);
  const [hover, setHover] = useState(null);
  const max = Math.max(0, ...data.map((d) => d.value));
  if (!data.length || max <= 0) {
    return html`<div ref=${ref} class="chart-wrap"><div class="chart-empty" style=${`height:${height}px`}>${emptyText}</div></div>`;
  }
  const pad = { l: 56, r: 10, t: 18, b: 26 };
  const iw = Math.max(10, w - pad.l - pad.r), ih = height - pad.t - pad.b;
  const y = niceDomain(0, max, Math.max(2, Math.floor(ih / 44)));
  const band = iw / data.length;
  const bw = Math.max(2, Math.min(24, band - 2));
  const sy = (v) => pad.t + ih - (v / (y.hi || 1)) * ih;
  const base = pad.t + ih;
  const maxLabelW = Math.max(...data.map((d) => String(xFmt(d)).length)) * 6.5 + 10;
  const every = Math.max(1, Math.ceil(maxLabelW / band));
  const maxIdx = data.findIndex((d) => d.value === max);
  const bar = (x, top) => {
    const h = base - top, r = Math.min(4, h, bw / 2);
    return `M${x},${base}V${top + r}Q${x},${top} ${x + r},${top}H${x + bw - r}Q${x + bw},${top} ${x + bw},${top + r}V${base}Z`;
  };
  return html`<div ref=${ref} class="chart-wrap"><div class="chart" style=${`height:${height}px`}>
    ${w > 0 && html`<svg width=${w} height=${height} role="img" onPointerLeave=${() => setHover(null)}>
      ${y.ticks.map((v) => html`<g><line class="gridline" x1=${pad.l} x2=${pad.l + iw} y1=${sy(v)} y2=${sy(v)} />
        <text class="tick" x=${pad.l - 8} y=${sy(v) + 4} text-anchor="end">${yFmt(v)}</text></g>`)}
      ${data.map((d, i) => {
        const x = pad.l + i * band + (band - bw) / 2;
        return html`<g>
          ${d.value > 0 && html`<path class=${`bar ${hover !== null && hover !== i ? "dim" : ""}`} d=${bar(x, sy(d.value))} fill=${color} />`}
          ${i === maxIdx && band >= 18 && html`<text class="val-label" x=${x + bw / 2} y=${sy(d.value) - 6} text-anchor="middle">${yFmt(d.value)}</text>`}
          ${i % every === 0 && html`<text class="tick" x=${pad.l + i * band + band / 2} y=${height - 6} text-anchor="middle">${xFmt(d)}</text>`}
          <rect x=${pad.l + i * band} y=${pad.t} width=${band} height=${ih} fill="transparent" tabindex="0"
            onPointerEnter=${() => setHover(i)} onFocus=${() => setHover(i)} onBlur=${() => setHover(null)} />
        </g>`;
      })}
      <line class="baseline" x1=${pad.l} x2=${pad.l + iw} y1=${base} y2=${base} />
    </svg>`}
    ${hover !== null && data[hover] && html`<${Tooltip} x=${pad.l + hover * band + band / 2} y=${Math.max(0, sy(data[hover].value) - 60)} width=${w}>
      <div class="t-head">${xFmt(data[hover])}</div>
      ${tooltip ? tooltip(data[hover]) : html`<div class="t-row"><span class="key-rect" style=${`background:${color}`}></span><b>${yFmt(data[hover].value)}</b></div>`}
    <//>`}
  </div></div>`;
}

export function Sparkline({ values, width = 96, height = 26 }) {
  const v = values.filter((x) => x !== null && x !== undefined);
  if (v.length < 2) return null;
  const lo = Math.min(...v), hi = Math.max(...v);
  const sx = (i) => 2 + (i / (v.length - 1)) * (width - 6);
  const sy = (x) => height - 3 - ((x - lo) / (hi - lo || 1)) * (height - 6);
  const d = v.map((x, i) => `${i ? "L" : "M"}${sx(i).toFixed(1)},${sy(x).toFixed(1)}`).join("");
  return html`<svg class="spark" width=${width} height=${height} aria-hidden="true">
    <path d=${d} fill="none" stroke="var(--text-3)" stroke-width="1.5" stroke-linejoin="round" />
    <circle cx=${sx(v.length - 1)} cy=${sy(v[v.length - 1])} r="3" fill="var(--accent)" stroke="var(--surface-1)" stroke-width="1.5" />
  </svg>`;
}

// A chart card with a table-view toggle (the accessible twin of the chart).
export function ChartCard({ title, sub, table, actions, loading, children }) {
  const [showTable, setShowTable] = useState(false);
  return html`<${Card} title=${title} sub=${sub} actions=${html`${actions}
      ${table && html`<button class="btn ghost sm" aria-pressed=${showTable} onClick=${() => setShowTable(!showTable)}
        title=${showTable ? "Show chart" : "Show as table"}><${Icon} name=${showTable ? "chart" : "table"} size=${14} />${showTable ? "Chart" : "Table"}</button>`}`}>
    <div class=${loading ? "chart loading" : ""}>
      ${showTable && table ? html`<div class="table-wrap" style="max-height:${table.maxHeight || 320}px;overflow:auto">
        <table class="data"><thead><tr>${table.columns.map((c, i) => html`<th class=${i ? "num" : ""}>${c}</th>`)}</tr></thead>
        <tbody>${table.rows.map((r) => html`<tr>${r.map((c, i) => html`<td class=${i ? "num" : ""}>${c}</td>`)}</tr>`)}</tbody></table>
      </div>` : children}
    </div>
  <//>`;
}
