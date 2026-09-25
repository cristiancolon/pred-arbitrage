import { html, useEffect, useRef } from "../vendor/preact-htm.js";
import { ago, api, clock, dateTime, duration, int, toast, until, useFetch, useNow, useStore } from "../lib.js";
import { Badge, Banner, Card, DataTable, Empty, Icon, Status } from "../ui.js";

function Discovery({ d, stats, now }) {
  if (!d) return html`<${Card} title="Live discovery"><div class="muted">Off (discovery = false in config.toml).</div><//>`;
  const tone = { running: "good", restarting: "warning", starting: "accent", stopped: "" }[d.state];
  const label = { running: "Watching for new markets", restarting: "Restarting", starting: "Starting", stopped: "Stopped" }[d.state];
  const hour = stats?.hour || {};
  const day = stats?.day || {};
  return html`<${Card} title="Live discovery"
    sub="Between refreshes, checks both venues for newly listed markets (Kalshi every 15 s, Polymarket US every 30 s), then matches, reviews and pairs them right away.">
    <div class="grid" style="gap:14px">
      <div style="display:flex;gap:16px;align-items:center;flex-wrap:wrap">
        <${Status} tone=${tone} pulse=${d.state === "running"}>${label}<//>
        ${d.since && d.state === "running" && html`<span class="muted">since ${dateTime(d.since)}${d.restarts ? ` · ${d.restarts} restarts` : ""}</span>`}
      </div>
      <dl class="facts">
        <dt>Last hour</dt><dd>${int(hour.K || 0)} Kalshi · ${int(hour.P || 0)} Polymarket US new markets</dd>
        <dt>Last 24 hours</dt><dd>${int(day.K || 0)} Kalshi · ${int(day.P || 0)} Polymarket US · ${int(stats?.suggestions_day)} suggested pairs</dd>
        <dt>Last found</dt><dd>${stats?.last ? ago(stats.last, now) : "—"}</dd>
      </dl>
    </div>
  <//>`;
}

const STAGE_INFO = {
  catalog: "Download every open market on both venues",
  match: "Suggest equivalent pairs; apply auto-approve rules",
  review: "Jev reads each new suggestion: approves clear matches, rejects clear mismatches, leaves the rest for you",
};

export function Jobs() {
  const job = useStore((s) => s.job);
  const state = useStore((s) => s.state);
  const pipeline = state?.pipeline;
  const logs = useStore((s) => s.logs);
  const { data } = useFetch("/api/jobs");
  const now = useNow(1000);
  const consoleRef = useRef();
  const stick = useRef(true);
  const lines = logs.length ? logs : data?.log || [];

  useEffect(() => {
    const el = consoleRef.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [lines.length]);

  if (!job) return html`<div class="muted">Connecting…</div>`;
  const running = job.state === "running";
  const run = async () => {
    const r = await api("/api/jobs/refresh", { method: "POST" });
    toast(r.started ? "Refresh started" : "A refresh is already running");
  };
  const stageIdx = job.stages.indexOf(job.stage);
  const cat = pipeline?.catalog;
  const tone = { running: "accent", ok: "good", failed: "critical", idle: "" }[job.state];
  const label = { running: "Running", ok: "Last run succeeded", failed: "Last run failed", idle: "Idle" }[job.state];

  return html`
    <div class="grid cols-3-2">
      <${Card} title="Refresh job" sub=${`Runs every ${duration(job.interval_s)} and on demand. Each stage is a separate low-priority process.`}
        actions=${html`<button class="btn primary" onClick=${run} disabled=${running}><${Icon} name="play" size=${13} />${running ? "Running…" : "Run now"}</button>`}>
        <div class="grid" style="gap:16px">
          <div style="display:flex;gap:16px;align-items:center;flex-wrap:wrap">
            <${Status} tone=${tone} pulse=${running}>${label}<//>
            ${running ? html`<span class="muted num">${duration(now - job.started)} elapsed</span>`
              : html`<span class="muted">Next run ${until(job.next_run, now)}${job.next_run ? ` · ${dateTime(job.next_run)}` : ""}</span>`}
          </div>
          <div class="stepper">
            ${job.stages.map((s, i) => html`${i > 0 && html`<${Icon} name="arrow" size=${14} cls="muted" />`}
              <span class=${`step ${running && i === stageIdx ? "active" : ""} ${running ? (i < stageIdx ? "done" : "") : job.state === "ok" ? "done" : ""}`} title=${STAGE_INFO[s]}>
                <span class="n">${i + 1}</span>${s[0].toUpperCase() + s.slice(1)}
                ${running && i === stageIdx && html`<span class="muted num" style="font-weight:500">${duration(now - job.stage_started)}</span>`}
              </span>`)}
          </div>
          ${job.state === "failed" && job.error && html`<${Banner} tone="critical"><b>Refresh failed:</b> ${job.error}. The log below has details.<//>`}
          <dl class="facts">
            <dt>Last success</dt><dd>${job.last_ok ? `${dateTime(job.last_ok)} (${ago(job.last_ok, now)})` : "never"}</dd>
            <dt>Catalog</dt><dd>${int(cat?.kalshi?.count)} Kalshi · ${int(cat?.pm?.count)} Polymarket US markets</dd>
            <dt>Suggestions</dt><dd>${int(pipeline?.match?.candidates)} candidates · ${int(pipeline?.review?.pending)} pending review</dd>
          </dl>
        </div>
      <//>
      <${Card} title="Recent runs" flush>
        ${job.history.length ? html`<${DataTable} rows=${job.history} rowKey=${(h) => h.started}
          columns=${[
            { key: "started", label: "Started", render: (h) => dateTime(h.started) },
            { key: "ok", label: "Result", render: (h) => h.ok ? html`<${Badge} tone="good" icon="check">OK<//>` : html`<${Badge} tone="critical" icon="xcircle">Failed<//>` },
            { key: "catalog", label: "Catalog", cls: "num", sortValue: (h) => h.timings.catalog, render: (h) => (h.timings.catalog == null ? "—" : duration(h.timings.catalog)) },
            ...job.stages.slice(1).map((st) => ({ key: st, label: st[0].toUpperCase() + st.slice(1), cls: "num",
              sortValue: (h) => h.timings[st], render: (h) => (h.timings[st] == null ? "—" : duration(h.timings[st])) })),
          ]} />` : html`<${Empty} icon="jobs" title="No runs yet">Runs since the service started appear here.<//>`}
      <//>
    </div>
    <${Discovery} d=${state?.discovery} stats=${pipeline?.discovery} now=${now} />
    <${Card} title="Log" sub="Output of the refresh job and live discovery, streamed live" flush>
      <div class="console" ref=${consoleRef} role="log" aria-live="polite"
        onScroll=${(e) => { const el = e.currentTarget; stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 30; }}>
        ${lines.length ? lines.map((l) => html`<div class="line"><span class="ts">${clock(l.ts)}</span><span class="stage-tag">${l.stage || "job"}</span><span class="text">${l.text}</span></div>`)
          : html`<div class="line"><span></span><span></span><span class="text muted">No output yet. Press Run now to refresh.</span></div>`}
      </div>
    <//>`;
}
