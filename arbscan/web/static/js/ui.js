// Shared UI pieces.
import { html, useEffect, useState } from "./vendor/preact-htm.js";
import { cents, pmTitle, splitTitle, useStore } from "./lib.js";

const ICONS = {
  overview: '<rect x="3.5" y="3.5" width="7" height="7" rx="2"/><rect x="13.5" y="3.5" width="7" height="7" rx="2"/><rect x="3.5" y="13.5" width="7" height="7" rx="2"/><rect x="13.5" y="13.5" width="7" height="7" rx="2"/>',
  pairs: '<path d="M10 14a4 4 0 0 0 5.66 0l3-3a4 4 0 0 0-5.66-5.66l-1 1"/><path d="M14 10a4 4 0 0 0-5.66 0l-3 3a4 4 0 0 0 5.66 5.66l1-1"/>',
  review: '<rect x="8" y="3" width="8" height="4" rx="1"/><path d="M16 5h2a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2h2"/><path d="m9 14 2 2 4-4"/>',
  zap: '<path d="M13 2 4 14h7l-1 8 9-12h-7z"/>',
  jobs: '<path d="M20 11a8 8 0 0 0-14.9-3.9L4 8"/><path d="M4 3v5h5"/><path d="M4 13a8 8 0 0 0 14.9 3.9L20 16"/><path d="M20 21v-5h-5"/>',
  database: '<ellipse cx="12" cy="5.5" rx="8" ry="3"/><path d="M4 5.5v13c0 1.7 3.6 3 8 3s8-1.3 8-3v-13"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
  match: '<circle cx="6" cy="6" r="2.5"/><circle cx="18" cy="18" r="2.5"/><path d="M6 8.5V15a3 3 0 0 0 3 3h6.5"/><path d="M18 15.5V9a3 3 0 0 0-3-3H8.5"/>',
  radar: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><path d="M12 12l6.4-6.4"/><circle cx="12" cy="12" r="1.2" fill="currentColor"/>',
  chart: '<path d="M3 21h18"/><rect x="5" y="11" width="3" height="7" rx="1"/><rect x="10.5" y="5" width="3" height="13" rx="1"/><rect x="16" y="14" width="3" height="4" rx="1"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  moon: '<path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z"/>',
  monitor: '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
  x: '<path d="M6 6l12 12M18 6 6 18"/>',
  alert: '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/>',
  check: '<circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/>',
  xcircle: '<circle cx="12" cy="12" r="9"/><path d="m15 9-6 6M9 9l6 6"/>',
  arrow: '<path d="M5 12h14M13 6l6 6-6 6"/>',
  play: '<path d="M7 4.5v15l12.5-7.5z"/>',
  trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>',
  swap: '<path d="M4 8h15l-4-4M20 16H5l4 4"/>',
  equal: '<path d="M5 9h14M5 15h14"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  table: '<rect x="3.5" y="4.5" width="17" height="15" rx="2"/><path d="M3.5 9.5h17M3.5 14.5h17M9.5 9.5v10"/>',
  skip: '<path d="M5 5l9 7-9 7z"/><path d="M19 5v14"/>',
};

export function Icon({ name, size = 16, cls = "" }) {
  return html`<svg class=${cls} width=${size} height=${size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
    stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"
    dangerouslySetInnerHTML=${{ __html: ICONS[name] || "" }}></svg>`;
}

export function Logo({ size = 26 }) {
  return html`<svg width=${size} height=${size} viewBox="0 0 32 32" aria-hidden="true">
    <rect width="32" height="32" rx="9" fill="var(--accent)" />
    <circle cx="12.5" cy="16" r="6.5" fill="none" stroke="#fff" stroke-width="2.2" />
    <circle cx="19.5" cy="16" r="6.5" fill="none" stroke="#fff" stroke-width="2.2" opacity="0.65" />
  </svg>`;
}

export function Card({ title, sub, actions, children, flush = false, cls = "" }) {
  return html`<section class=${`card ${cls}`}>
    ${(title || actions) && html`<div class="card-head">
      <div><h2>${title}</h2>${sub && html`<div class="sub">${sub}</div>`}</div>
      ${actions && html`<div class="actions">${actions}</div>`}
    </div>`}
    <div class=${flush ? "card-body flush" : "card-body"}>${children}</div>
  </section>`;
}

export function Tile({ label, value, foot, title }) {
  return html`<div class="card tile" title=${title}>
    <div class="label">${label}</div>
    <div class="value">${value}</div>
    <div class="foot">${foot}</div>
  </div>`;
}

export function Badge({ tone = "", icon, children }) {
  const iconCls = { good: "icon-good", warning: "icon-warning", critical: "icon-critical" }[tone] || "";
  return html`<span class=${`badge ${tone === "accent" ? "accent" : ""}`}>
    ${icon && html`<${Icon} name=${icon} size=${13} cls=${iconCls} />`}${children}
  </span>`;
}

// Status is never color alone: a dot plus a label.
export function Status({ tone = "", pulse = false, children }) {
  return html`<span class="status"><span class=${`dot ${tone} ${pulse ? "pulse" : ""}`}></span>${children}</span>`;
}

export function RelationChip({ relation }) {
  if (!relation) return null;
  const inv = relation === "inverse";
  return html`<span class=${`chip ${relation}`} title=${inv ? "Polymarket YES = Kalshi NO" : "Polymarket YES = Kalshi YES"}>
    <${Icon} name=${inv ? "swap" : "equal"} size=${12} />${inv ? "Inverse" : "Same"}
  </span>`;
}

// Net edge after fees. Ink carries the number; a check icon + the sign mark profit.
export function Edge({ edge, strong = false }) {
  if (edge === null || edge === undefined) return html`<span class="muted">—</span>`;
  const pos = edge > 0;
  return html`<span class="num nowrap" style=${strong || pos ? "font-weight:650" : ""}>
    ${pos && html`<span style="color:var(--good);vertical-align:-2px;margin-right:4px"><${Icon} name="check" size=${13} /></span>`}${cents(edge)}
  </span>`;
}

// Diverging mini-bar: position of an edge relative to breakeven (0).
export function DivBar({ value, max = 0.05, width = 110 }) {
  const v = Math.max(-max, Math.min(max, value ?? 0));
  const half = width / 2;
  const w = (Math.abs(v) / max) * half;
  return html`<div class="divbar" style=${`width:${width}px`} role="img" aria-label=${`${cents(value)} from breakeven`}>
    <div class=${`fill ${v < 0 ? "neg" : "pos"}`} style=${v < 0 ? `right:${half}px;width:${w}px` : `left:${half}px;width:${w}px`}></div>
    <div class="mid" style=${`left:${half}px`}></div>
  </div>`;
}

export function Seg({ options, value, onChange, label }) {
  return html`<div class="seg" role="group" aria-label=${label}>
    ${options.map((o) => html`<button type="button" aria-pressed=${o.value === value} onClick=${() => onChange(o.value)} title=${o.title}>
      ${o.icon ? html`<${Icon} name=${o.icon} size=${14} />` : o.label}
    </button>`)}
  </div>`;
}

export function Empty({ icon = "info", title, children, action }) {
  return html`<div class="empty">
    <div class="icon-wrap"><${Icon} name=${icon} size=${20} /></div>
    <h3>${title}</h3>
    ${children && html`<p>${children}</p>`}
    ${action}
  </div>`;
}

export function Banner({ tone = "info", icon, children }) {
  const name = icon || { warning: "alert", critical: "xcircle", info: "info" }[tone];
  return html`<div class=${`banner ${tone}`} role=${tone === "info" ? "note" : "alert"}><${Icon} name=${name} size=${16} /><div>${children}</div></div>`;
}

export function PairName({ k_title, p_title, k_yes }) {
  const k = splitTitle(k_title);
  const yes = k_yes || (k.tail !== k.head ? k.tail : "");
  return html`<div class="pair-name">
    <div class="k" title=${k_title}>${k.head}</div>
    <div class="p" title=${`Kalshi YES: ${yes}\nPolymarket US: ${p_title}`}>${yes ? html`YES ${yes} · ` : ""}Poly US ${pmTitle(p_title)}</div>
  </div>`;
}

export function Drawer({ title, sub, onClose, actions, children }) {
  useEffect(() => {
    const fn = (e) => e.key === "Escape" && onClose();
    addEventListener("keydown", fn);
    return () => removeEventListener("keydown", fn);
  }, [onClose]);
  return html`<div class="scrim" onClick=${onClose}></div>
    <aside class="drawer" role="dialog" aria-modal="true" aria-label=${title}>
      <div class="drawer-head">
        <div style="min-width:0;flex:1"><h2>${title}</h2>${sub && html`<div class="muted" style="font-size:12.5px;margin-top:3px">${sub}</div>`}</div>
        ${actions}
        <button class="btn ghost sm" onClick=${onClose} aria-label="Close"><${Icon} name="x" size=${16} /></button>
      </div>
      <div class="drawer-body">${children}</div>
    </aside>`;
}

export function Toasts() {
  const toasts = useStore((s) => s.toasts || []);
  return html`<div class="toasts" aria-live="polite">
    ${toasts.map((t) => html`<div class="toast" key=${t.id}><${Icon} name="check" size=${16} />${t.message}</div>`)}
  </div>`;
}

export function Kbd({ children }) {
  return html`<kbd>${children}</kbd>`;
}

// A table with click-to-sort headers.
export function DataTable({ columns, rows, rowKey, onRowClick, initialSort, empty }) {
  const [sort, setSort] = useState(initialSort || null);
  let sorted = rows;
  if (sort) {
    const col = columns.find((c) => c.key === sort.key);
    const get = col?.sortValue || ((r) => r[sort.key]);
    sorted = [...rows].sort((a, b) => {
      const x = get(a), y = get(b);
      if (x === y) return 0;
      if (x === null || x === undefined) return 1;
      if (y === null || y === undefined) return -1;
      return (x < y ? -1 : 1) * (sort.dir === "desc" ? -1 : 1);
    });
  }
  if (!rows.length && empty) return empty;
  return html`<div class="table-wrap"><table class="data">
    <thead><tr>${columns.map((c) => html`<th class=${`${c.cls || ""} ${c.sortable !== false ? "sortable" : ""}`}
      aria-sort=${sort?.key === c.key ? (sort.dir === "desc" ? "descending" : "ascending") : undefined}
      onClick=${() => c.sortable !== false && setSort((s) => ({ key: c.key, dir: s?.key === c.key && s.dir === "desc" ? "asc" : "desc" }))}>
      ${c.label}${sort?.key === c.key ? (sort.dir === "desc" ? " ↓" : " ↑") : ""}</th>`)}</tr></thead>
    <tbody>${sorted.map((r) => html`<tr key=${rowKey(r)} class=${onRowClick ? "clickable" : ""} onClick=${onRowClick ? () => onRowClick(r) : undefined}>
      ${columns.map((c) => html`<td class=${c.cls || ""}>${c.render ? c.render(r) : r[c.key]}</td>`)}</tr>`)}</tbody>
  </table></div>`;
}
