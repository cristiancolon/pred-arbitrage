import { html, useCallback, useEffect, useRef, useState } from "../vendor/preact-htm.js";
import { api, dateTime, duration, highlight, int, pmTitle, price, splitTitle, toast, usePref, useStore } from "../lib.js";
import { Banner, Card, Empty, Icon, Kbd, RelationChip } from "../ui.js";

const INVERSE_WARNING = html`<b>Inverse pair.</b> Polymarket YES is treated as Kalshi NO. That only hedges if the event
  can't end any other way: no draw or tie, and both venues handle postponement or cancellation the same way.
  Check both rulebooks below.`;

function Score({ value }) {
  return html`<span class="score" title="Match score"><span class="track"><i style=${`width:${Math.min(100, value * 100)}%`}></i></span>${value.toFixed(2)}</span>`;
}

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

export function Review() {
  const [q, setQ] = useState("");
  const [minScore, setMinScore] = usePref("review.min", 0.45);
  const [confident, setConfident] = usePref("review.confident", false);
  const [relation, setRelation] = useState("");
  const [items, setItems] = useState([]);
  const [total, setTotal] = useState(0);
  const [sel, setSel] = useState(0);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const matchUpdated = useStore((s) => s.state?.pipeline?.match?.updated);
  const listRef = useRef();

  const query = useCallback((offset) => {
    const u = new URLSearchParams({ min_score: minScore, offset, limit: 40 });
    if (confident) u.set("confident", "1");
    if (relation) u.set("relation", relation);
    if (q.trim()) u.set("q", q.trim());
    return api(`/api/candidates?${u}`);
  }, [q, minScore, confident, relation]);

  useEffect(() => {
    setLoading(true);
    const t = setTimeout(() => query(0).then((d) => { setItems(d.items); setTotal(d.total); setSel(0); }).finally(() => setLoading(false)), 200);
    return () => clearTimeout(t);
  }, [query, matchUpdated]);

  const more = useCallback(async () => {
    const d = await query(items.length);
    setItems((xs) => [...xs, ...d.items.filter((it) => !xs.some((x) => x.kalshi === it.kalshi && x.pm === it.pm))]);
    setTotal(d.total);
  }, [query, items.length]);

  const cur = items[sel];
  const decide = useCallback(async (decision) => {
    if (!cur || busy) return;
    setBusy(true);
    try {
      await api("/api/candidates/decide", { method: "POST", body: { kalshi: cur.kalshi, pm: cur.pm, decision } });
      toast(decision === "reject" ? "Rejected" : `Approved as ${decision}. Scanning starts next sweep.`);
      setItems((xs) => xs.filter((x) => x !== cur));
      setTotal((t) => t - 1);
      setSel((i) => Math.min(i, items.length - 2));
      if (items.length < 12 && total > items.length) more();
    } finally {
      setBusy(false);
    }
  }, [cur, busy, items.length, total, more]);

  useEffect(() => {
    const fn = (e) => {
      if (e.target.closest("input, select, textarea") || e.metaKey || e.ctrlKey || e.altKey) return;
      const key = e.key.toLowerCase();
      if (key === "s") decide("same");
      else if (key === "i") decide("inverse");
      else if (key === "r") decide("reject");
      else if (key === "j" || key === "arrowdown" || key === "arrowright") setSel((i) => Math.min(items.length - 1, i + 1));
      else if (key === "k" || key === "arrowup" || key === "arrowleft") setSel((i) => Math.max(0, i - 1));
      else return;
      e.preventDefault();
    };
    addEventListener("keydown", fn);
    return () => removeEventListener("keydown", fn);
  }, [decide, items.length]);

  useEffect(() => {
    listRef.current?.querySelector(".q-item.sel")?.scrollIntoView({ block: "nearest" });
  }, [sel]);

  return html`<div class="review">
    <section class="card queue" aria-label="Review queue">
      <div class="queue-filters">
        <label class="search"><${Icon} name="search" size=${15} /><input class="input" placeholder="Search titles or tickers" value=${q} onInput=${(e) => setQ(e.target.value)} aria-label="Search candidates" /></label>
        <div style="display:flex;gap:10px;align-items:center">
          <label class="muted nowrap" style="font-size:12.5px" for="minscore">Min score</label>
          <input id="minscore" type="range" min="0.3" max="1" step="0.05" value=${minScore} onInput=${(e) => setMinScore(+e.target.value)} style="flex:1" />
          <span class="num" style="font-size:12.5px;width:30px">${minScore.toFixed(2)}</span>
        </div>
        <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">
          <label class="check"><input type="checkbox" checked=${confident} onChange=${(e) => setConfident(e.target.checked)} />Labels matched</label>
          <select class="select" value=${relation} onChange=${(e) => setRelation(e.target.value)} aria-label="Relation">
            <option value="">Same or inverse</option><option value="same">Same only</option><option value="inverse">Inverse only</option>
          </select>
          <span class="muted num" style="margin-left:auto;font-size:12.5px">${int(total)} pending</span>
        </div>
      </div>
      <div class="queue-list" ref=${listRef} style=${loading ? "opacity:.55" : ""}>
        ${items.map((c, i) => html`<div class=${`q-item ${i === sel ? "sel" : ""}`} key=${c.kalshi + c.pm} onClick=${() => setSel(i)}>
          <div class="row1"><${Score} value=${c.score} /><${RelationChip} relation=${c.relation} /><span class="spacer"></span>
            ${!c.confident && html`<span class="muted" style="font-size:11px" title="No outcome labels to compare">guessed</span>`}</div>
          <div class="k">${splitTitle(c.k?.title).head}${c.k?.yes_label ? html`<span class="muted"> · ${c.k.yes_label}</span>` : ""}</div>
          <div class="p">Poly US · ${pmTitle(c.p?.title)}</div>
        </div>`)}
        ${items.length < total && html`<div style="padding:12px;text-align:center"><button class="btn sm" onClick=${more}>Load more</button></div>`}
        ${!loading && !items.length && html`<${Empty} icon="review" title="Queue is empty">Nothing matches these filters. New suggestions arrive after each refresh.<//>`}
      </div>
    </section>

    ${cur ? html`<section class="card" aria-label="Candidate">
      <div class="card-head">
        <div style="min-width:0">
          <h2 style="font-size:16px">${splitTitle(cur.k.title).head}</h2>
          <div class="sub" style="display:flex;gap:10px;align-items:center;margin-top:6px;flex-wrap:wrap">
            <${Score} value=${cur.score} /><span>Proposed</span><${RelationChip} relation=${cur.relation} />
            <span>${cur.relation === "inverse" ? "Polymarket YES = Kalshi NO" : "Polymarket YES = Kalshi YES"}</span>
          </div>
        </div>
        <div class="actions"><span class="muted num" style="font-size:12px">${sel + 1} of ${int(total)}</span></div>
      </div>
      <div class="card-body grid" style="gap:16px">
        ${!cur.confident && html`<${Banner} tone="info">The relation is a guess: Polymarket gives no outcome labels to compare. Check which side each YES refers to.<//>`}
        ${cur.relation === "inverse" && html`<${Banner} tone="warning">${INVERSE_WARNING}<//>`}
        <${Mapping} relation=${cur.relation} k=${cur.k} p=${cur.p} />
        <${RulesCompare} k=${cur.k} p=${cur.p} />
      </div>
      <div class="actionbar">
        <button class=${`btn ${cur.relation === "same" ? "primary" : ""}`} disabled=${busy} onClick=${() => decide("same")}>
          <${Icon} name="equal" size=${14} />Approve as same <${Kbd}>S<//></button>
        <button class=${`btn ${cur.relation === "inverse" ? "primary" : ""}`} disabled=${busy} onClick=${() => decide("inverse")}>
          <${Icon} name="swap" size=${14} />Approve as inverse <${Kbd}>I<//></button>
        <button class="btn danger" disabled=${busy} onClick=${() => decide("reject")}><${Icon} name="xcircle" size=${14} />Reject <${Kbd}>R<//></button>
        <span class="spacer"></span>
        <button class="btn ghost" onClick=${() => setSel((i) => Math.min(items.length - 1, i + 1))}>Skip <${Kbd}>J<//></button>
      </div>
    </section>` : html`<${Card}>${loading ? html`<div class="muted">Loading…</div>` : html`<${Empty} icon="check" title="All caught up">
      No pending suggestions at this score. Lower the minimum score, or wait for the next refresh.<//>`}<//>`}
  </div>`;
}
