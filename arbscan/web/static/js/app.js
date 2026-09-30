import { html, render, useEffect } from "./vendor/preact-htm.js";
import { ago, connect, money, useNow, usePref, useRoute, useStore } from "./lib.js";
import { Icon, Logo, Seg, Status, Toasts } from "./ui.js";
import { Overview } from "./pages/overview.js";
import { Pairs } from "./pages/pairs.js";
import { Opportunities } from "./pages/opportunities.js";
import { Trading } from "./pages/trading.js";
import { Jobs } from "./pages/jobs.js";

// The trading desk comes first; the rest is the pipeline that feeds it.
const PAGES = {
  trading: { title: "Trading", sub: "Live arbitrage between Kalshi and Polymarket US, with real orders", icon: "play", el: Trading },
  overview: { title: "Scanner", sub: "Pipeline status, and how close the two venues come to an arbitrage", icon: "radar", el: Overview, group: "Pipeline" },
  opportunities: { title: "Opportunities", sub: "Every window that was profitable after fees", icon: "zap", el: Opportunities, group: "Pipeline" },
  pairs: { title: "Pairs", sub: "Approved Kalshi ↔ Polymarket US pairs and their live prices", icon: "pairs", el: Pairs, group: "Pipeline" },
  jobs: { title: "Refresh job", sub: "Market catalog and pair matching, on a schedule", icon: "jobs", el: Jobs, group: "Pipeline" },
};
const ALIASES = { paper: "trading" };  // old links

function ThemeToggle() {
  const [theme, setTheme] = usePref("theme", "dark");
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

// Its counters change with every live update, so they live here rather than in App:
// re-rendering App would re-render the whole page underneath it every second.
// The live trader's state on every page: a dot in the menu, a pill in the header.
function LiveDot() {
  const live = useStore((s) => s.state?.live);
  if (!live) return html`<span class="dot" title="Live trading is off"></span>`;
  if (live.halted) return html`<span class="dot critical" title="Live trading stopped"></span>`;
  return html`<span class="dot good pulse" title="Live trading on"></span>`;
}

function LivePill() {
  const live = useStore((s) => s.state?.live);
  if (live === undefined) return null;
  if (!live) return html`<a class="pill off" href="#/trading"><span class="dot"></span>Live trading off</a>`;
  if (live.halted) return html`<a class="pill stopped" href="#/trading" title=${live.halted}><span class="dot critical"></span>Trading stopped</a>`;
  return html`<a class=${`pill ${live.in_flight ? "busy" : "on"}`} href="#/trading">
    <span class=${`dot ${live.in_flight ? "accent" : "good"} pulse`}></span>
    <span class="num">${money(live.bankroll)}</span>
    <span class=${`num ${live.pnl > 0.005 ? "up" : live.pnl < -0.005 ? "down" : ""}`}>${live.pnl > 0 ? "+" : ""}${money(live.pnl)}</span>
  </a>`;
}

function Sidebar({ page }) {
  const open = useStore((s) => s.state?.open_picks || 0);
  const err = useStore((s) => s.state?.scanner?.last_error);
  const now = useNow(5000);
  const counts = { opportunities: open || null };
  return html`<nav class="sidebar" aria-label="Main">
    <div class="brand"><${Logo} /><div>arbscan<small>Kalshi ↔ Polymarket US</small></div></div>
    ${Object.entries(PAGES).map(([key, p], i, all) => html`
      ${p.group && p.group !== all[i - 1]?.[1].group && html`<div class="nav-group">${p.group}</div>`}
      <a class=${`nav-item ${key === page ? "active" : ""}`} href=${`#/${key}`} aria-current=${key === page ? "page" : undefined}>
        <${Icon} name=${p.icon} size=${16} /><span class="label">${p.title}</span>
        ${key === "trading" ? html`<${LiveDot} />` : counts[key] ? html`<span class="count">${counts[key] > 999 ? "999+" : counts[key]}</span>` : ""}
      </a>`)}
    <div class="sidebar-foot">
      <${Connection} />
      ${err && now - err.ts < 600 && html`<${Status} tone="critical">Last sweep error ${ago(err.ts, now)}<//>`}
      <${ThemeToggle} />
    </div>
  </nav>`;
}

function App() {
  const route = useRoute();
  const page = ALIASES[route.page] || route.page;
  const params = route.params;
  const current = PAGES[page] || PAGES.trading;
  useEffect(() => { document.title = `${current.title} · arbscan`; }, [current]);
  const Page = current.el;
  return html`<div class="app">
    <${Sidebar} page=${page} />
    <main>
      <header class="topbar">
        <div><h1>${current.title}</h1><div class="sub">${current.sub}</div></div>
        <span class="spacer"></span>
        <${LivePill} />
        <${Connection} />
      </header>
      <div class="page"><${Page} params=${params} /></div>
    </main>
    <${Toasts} />
  </div>`;
}

connect();
render(html`<${App} />`, document.getElementById("root"));
