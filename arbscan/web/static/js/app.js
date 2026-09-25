import { html, render, useEffect } from "./vendor/preact-htm.js";
import { ago, connect, useNow, usePref, useRoute, useStore } from "./lib.js";
import { Icon, Logo, Seg, Status, Toasts } from "./ui.js";
import { Overview } from "./pages/overview.js";
import { Pairs } from "./pages/pairs.js";
import { Review } from "./pages/review.js";
import { Opportunities } from "./pages/opportunities.js";
import { Jobs } from "./pages/jobs.js";

const PAGES = {
  overview: { title: "Overview", sub: "Pipeline status, and how close the two venues come to an arbitrage", icon: "overview", el: Overview },
  pairs: { title: "Pairs", sub: "Approved Kalshi ↔ Polymarket US pairs and their live prices", icon: "pairs", el: Pairs },
  review: { title: "Review", sub: "Approve a pair only after reading both rulebooks", icon: "review", el: Review },
  opportunities: { title: "Opportunities", sub: "Every window that was profitable after fees", icon: "zap", el: Opportunities },
  jobs: { title: "Refresh job", sub: "Market catalog and pair matching, on a schedule", icon: "jobs", el: Jobs },
};

function ThemeToggle() {
  const [theme, setTheme] = usePref("theme", "system");
  useEffect(() => {
    if (theme === "system") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", theme);
  }, [theme]);
  return html`<${Seg} label="Theme" value=${theme} onChange=${setTheme} options=${[
    { value: "system", icon: "monitor", title: "Match system" }, { value: "light", icon: "sun", title: "Light" }, { value: "dark", icon: "moon", title: "Dark" }]} />`;
}

function Connection() {
  const conn = useStore((s) => s.conn);
  const last = useStore((s) => s.state?.scanner?.last?.ts);
  const now = useNow(1000);
  if (conn !== "live") return html`<${Status} tone="warning">Reconnecting…<//>`;
  return html`<${Status} tone="good" pulse>Live${last ? html`<span class="muted"> · swept ${ago(last, now)}</span>` : ""}<//>`;
}

function App() {
  const { page, params } = useRoute();
  const pending = useStore((s) => s.state?.pipeline?.review?.pending);
  const open = useStore((s) => s.state?.open?.length || 0);
  const err = useStore((s) => s.state?.scanner?.last_error);
  const now = useNow(5000);
  const current = PAGES[page] || PAGES.overview;
  const counts = { review: pending || null, opportunities: open || null };
  useEffect(() => { document.title = `${current.title} · arbscan`; }, [current]);
  const Page = current.el;
  return html`<div class="app">
    <nav class="sidebar" aria-label="Main">
      <div class="brand"><${Logo} /><div>arbscan<small>Kalshi ↔ Polymarket US</small></div></div>
      ${Object.entries(PAGES).map(([key, p]) => html`<a class=${`nav-item ${key === page ? "active" : ""}`} href=${`#/${key}`}
          aria-current=${key === page ? "page" : undefined}>
        <${Icon} name=${p.icon} size=${16} /><span class="label">${p.title}</span>
        ${counts[key] ? html`<span class="count">${counts[key] > 999 ? "999+" : counts[key]}</span>` : ""}
      </a>`)}
      <div class="sidebar-foot">
        <${Connection} />
        ${err && now - err.ts < 600 && html`<${Status} tone="critical">Last sweep error ${ago(err.ts, now)}<//>`}
        <${ThemeToggle} />
        <span>Read-only: this never places orders.</span>
      </div>
    </nav>
    <main>
      <header class="topbar">
        <div><h1>${current.title}</h1><div class="sub">${current.sub}</div></div>
        <span class="spacer"></span>
        <${Connection} />
      </header>
      <div class="page"><${Page} params=${params} /></div>
    </main>
    <${Toasts} />
  </div>`;
}

connect();
render(html`<${App} />`, document.getElementById("root"));
