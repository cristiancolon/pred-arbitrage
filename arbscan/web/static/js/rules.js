// How two paired markets line up, and their rulebooks side by side.
import { html } from "./vendor/preact-htm.js";
import { dateTime, duration, highlight, price, splitTitle } from "./lib.js";
import { Banner } from "./ui.js";

// Which outcome on one venue is the same bet as which on the other.
export function Mapping({ relation, k, p }) {
  const inv = relation === "inverse";
  const kYes = k.yes_label || splitTitle(k.title).tail || "YES";
  return html`<div>
    <div class="mapping" role="img" aria-label=${inv ? "Kalshi YES matches Polymarket NO; Kalshi NO matches Polymarket YES" : "Kalshi YES matches Polymarket YES; Kalshi NO matches Polymarket NO"}>
      <div class="outcome" style="grid-column:1;grid-row:1"><small>Kalshi YES</small>${kYes}</div>
      <div class="outcome" style="grid-column:1;grid-row:2"><small>Kalshi NO</small>Not: ${kYes}</div>
      <svg viewBox="0 0 72 100" preserveAspectRatio="none" style="grid-column:2;grid-row:1 / 3" aria-hidden="true">
        <path class="linkline" vector-effect="non-scaling-stroke" d=${inv ? "M0,23 C36,23 36,77 72,77" : "M0,23 H72"} />
        <path class="linkline alt" vector-effect="non-scaling-stroke" d=${inv ? "M0,77 C36,77 36,23 72,23" : "M0,77 H72"} />
      </svg>
      <div class=${`outcome ${inv ? "" : "hl"}`} style="grid-column:3;grid-row:1"><small>Polymarket US YES · buy</small>${p.yes_label}</div>
      <div class=${`outcome ${inv ? "hl" : ""}`} style="grid-column:3;grid-row:2"><small>Polymarket US NO · sell</small>${p.no_label}</div>
    </div>
    <div class="muted" style="font-size:12.5px;margin-top:8px">
      Lines join the same outcome on both venues. An arb buys opposite outcomes, one on each venue, so exactly one pays $1:
      ${inv ? " Kalshi YES + Poly YES, or Kalshi NO + Poly NO." : " Kalshi YES + Poly NO, or Kalshi NO + Poly YES."}
    </div>
  </div>`;
}

function Rules({ text }) {
  return html`<div class="rules">${highlight(text).map((s) => (s.kind ? html`<mark class=${s.kind}>${s.t}</mark>` : s.t))}</div>`;
}

function Venue({ tag, m, extra }) {
  return html`<div class="venue">
    <div class="venue-tag">${tag}</div>
    <h3>${m.title}</h3>
    <div class="id">${m.id}</div>
    <dl class="facts">
      ${extra}
      <dt>Starts</dt><dd>${m.start_ts ? dateTime(m.start_ts) : "—"}</dd>
      <dt>Resolves</dt><dd>${m.close_ts ? `~${dateTime(m.close_ts)}` : "—"}</dd>
      <dt>YES bid / ask</dt><dd>${price(m.yes_bid)} / ${price(m.yes_ask)} <span class="muted">at catalog time</span></dd>
      <dt>Taker fee</dt><dd>${m.fee_coef?.toFixed(4)} × p(1−p) per contract</dd>
    </dl>
    <${Rules} text=${m.rules || "(no rules text)"} />
  </div>`;
}

export function RulesCompare({ k, p }) {
  const dt = k.start_ts && p.start_ts ? Math.abs(k.start_ts - p.start_ts) : null;
  return html`<div class="grid" style="gap:12px">
    ${dt !== null && dt > 2 * 3600 && html`<${Banner} tone="warning">Start times differ by <b>${duration(dt)}</b>. Make sure these are the same event.<//>`}
    <div class="compare">
      <${Venue} tag="Kalshi" m=${k} extra=${html`<dt>YES</dt><dd>${k.yes_label || "—"}</dd>`} />
      <${Venue} tag="Polymarket US" m=${p} extra=${html`<dt>YES / NO</dt><dd>${p.yes_label} / ${p.no_label}</dd>`} />
    </div>
    <div class="muted" style="font-size:12px">Highlighted: numbers and dates, and wording that changes how a market settles (draws, postponement, exclusions, deadlines).</div>
  </div>`;
}
