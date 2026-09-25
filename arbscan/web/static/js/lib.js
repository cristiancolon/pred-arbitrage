// Shared state, API access, live stream, formatting.
import { useEffect, useRef, useState, useCallback } from "./vendor/preact-htm.js";

// ---------- store ----------
const listeners = new Set();
let store = { state: null, job: null, logs: [], conn: "connecting", pairsVersion: 0 };

export function getStore() { return store; }
export function setStore(patch) {
  store = { ...store, ...(typeof patch === "function" ? patch(store) : patch) };
  listeners.forEach((fn) => fn(store));
}
export function useStore(select) {
  const [value, setValue] = useState(() => select(store));
  const sel = useRef(select);
  sel.current = select;
  useEffect(() => {
    const fn = (s) => setValue(() => sel.current(s));
    listeners.add(fn);
    fn(store);
    return () => listeners.delete(fn);
  }, []);
  return value;
}

// ---------- api ----------
export async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok && res.status !== 409) throw new Error(data.error || `${res.status} ${res.statusText}`);
  return data;
}

// Fetch with "refetch keeps the frame": previous data stays visible while reloading.
export function useFetch(path, deps = [], { refreshOn } = {}) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const tick = useStore((s) => (refreshOn ? refreshOn(s) : 0));
  const seq = useRef(0);
  const load = useCallback(() => {
    if (!path) return;
    const n = ++seq.current;
    setLoading(true);
    api(path)
      .then((d) => { if (n === seq.current) { setData(d); setError(null); } })
      .catch((e) => { if (n === seq.current) setError(e.message); })
      .finally(() => { if (n === seq.current) setLoading(false); });
  }, [path]);
  useEffect(load, [path, tick, ...deps]);
  return { data, loading, error, reload: load };
}

// ---------- live stream ----------
let es = null;
export function connect() {
  if (es) es.close();
  es = new EventSource("/api/stream");
  es.addEventListener("open", () => setStore({ conn: "live" }));
  es.addEventListener("error", () => setStore({ conn: "reconnecting" }));
  es.addEventListener("state", (e) => {
    const state = JSON.parse(e.data);
    setStore((s) => ({ state, job: state.job, conn: "live", pairsVersion: s.pairsVersion + 1 }));
  });
  es.addEventListener("job", (e) => setStore({ job: JSON.parse(e.data) }));
  es.addEventListener("log", (e) => {
    const line = JSON.parse(e.data);
    setStore((s) => ({ logs: [...s.logs.slice(-399), line] }));
  });
}

// ---------- time ----------
export function useNow(ms = 1000) {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now() / 1000), ms);
    return () => clearInterval(id);
  }, [ms]);
  return now;
}

// The value once it has stopped changing for `ms` (e.g. a search box, so each keystroke isn't a request).
export function useDebounced(value, ms = 250) {
  const [v, setV] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setV(value), ms);
    return () => clearTimeout(id);
  }, [value, ms]);
  return v;
}

// ---------- routing ----------
export function parseRoute() {
  const [page, query = ""] = (location.hash.replace(/^#\/?/, "") || "overview").split("?");
  return { page: page || "overview", params: Object.fromEntries(new URLSearchParams(query)) };
}
export function useRoute() {
  const [route, setRoute] = useState(parseRoute);
  useEffect(() => {
    const fn = () => setRoute(parseRoute());
    addEventListener("hashchange", fn);
    return () => removeEventListener("hashchange", fn);
  }, []);
  return route;
}
export function navigate(page, params = {}) {
  const q = new URLSearchParams(params).toString();
  location.hash = `#/${page}${q ? "?" + q : ""}`;
}

// ---------- persisted UI preferences (per browser; optional) ----------
export function usePref(key, initial) {
  const [value, setValue] = useState(() => {
    try {
      const v = localStorage.getItem(`arbscan:${key}`);
      return v === null ? initial : JSON.parse(v);
    } catch { return initial; }
  });
  const set = useCallback((v) => {
    setValue(v);
    try { localStorage.setItem(`arbscan:${key}`, JSON.stringify(v)); } catch {}
  }, [key]);
  return [value, set];
}

// ---------- formatting ----------
const MINUS = "−";
export function cents(edge, { sign = true, digits = 2 } = {}) {
  if (edge === null || edge === undefined) return "—";
  const v = edge * 100;
  const s = Math.abs(v).toFixed(digits);
  return `${v < 0 ? MINUS : sign && v > 0 ? "+" : ""}${s}¢`;
}
export function money(v, digits = 2) {
  if (v === null || v === undefined) return "—";
  const s = Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
  return `${v < 0 ? MINUS : ""}$${s}`;
}
export function compact(n) {
  if (n === null || n === undefined) return "—";
  return Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 }).format(n);
}
export function int(n) {
  return n === null || n === undefined ? "—" : Math.round(n).toLocaleString();
}
// Return per year of lock-up (1.0 = 100%).
export function perYear(r, { suffix = "/yr" } = {}) {
  if (r === null || r === undefined) return "—";
  return `${Math.round(r * 100).toLocaleString()}%${suffix}`;
}
// The pick rules in words, e.g. "resolves within 7 days, returns 100%+ a year, edge up to 5¢, open 1 s+".
export function ruleText(r) {
  if (!r) return "";
  return [r.max_days ? `resolves within ${r.max_days} days` : null, `returns ${Math.round(r.min_annualized * 100)}%+ a year`,
    r.max_edge ? `edge up to ${Math.round(r.max_edge * 100)}¢` : null, `open ${r.min_window_s} s+`].filter(Boolean).join(", ");
}
export function price(p) {
  if (p === null || p === undefined) return "—";
  return `${(p * 100).toFixed(1).replace(/\.0$/, "")}¢`;
}
export function duration(s) {
  if (s === null || s === undefined) return "—";
  if (s < 1) return "<1s";
  if (s < 90) return `${Math.round(s)}s`;
  if (s < 5400) return `${Math.round(s / 60)}m`;
  if (s < 172800) return `${(s / 3600).toFixed(1).replace(/\.0$/, "")}h`;
  return `${Math.round(s / 86400)}d`;
}
export function ago(ts, now = Date.now() / 1000) {
  if (!ts) return "never";
  const d = now - ts;
  if (d < 5) return "just now";
  return `${duration(d)} ago`;
}
export function until(ts, now = Date.now() / 1000) {
  if (!ts) return "—";
  const d = ts - now;
  return d <= 0 ? "now" : `in ${duration(d)}`;
}
export function days(d) {
  if (d === null || d === undefined) return "—";
  if (d < 1) return `${Math.max(0, Math.round(d * 24))}h`;
  return `${d.toFixed(d < 10 ? 1 : 0)}d`;
}
export function dateTime(ts) {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}
export function clock(ts) {
  return new Date(ts * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" });
}

// Kalshi titles are "Event | subtitle | market"; show the event and the market.
export function splitTitle(t) {
  const parts = (t || "").split(" | ");
  return { head: parts[0] || t || "", tail: parts.length > 1 ? parts[parts.length - 1] : "" };
}
// "Who will win in the upcoming football event A vs B scheduled for ..." -> "A vs B"
export function pmTitle(t) {
  return (t || "")
    .replace(/^Who will win in the upcoming [\w-]+ event /i, "")
    .replace(/ scheduled for [^?|]*/i, "")
    .replace(/\?(?= \||$)/, "");
}
export function dirLabel(d) {
  // "K:YES+P:NO" -> "Kalshi YES + Poly NO"
  return (d || "").replace("K:", "Kalshi ").replace("P:", "Poly ").replace("+", " + ");
}

// Highlight the parts of a rulebook that decide equivalence: numbers, dates, and
// wording that changes how a market settles.
const RISK = /\b(draw|tie|tied|void|cancel\w*|postpon\w*|suspend\w*|abandon\w*|forfeit\w*|exclud\w*|not includ\w*|overtime|extra innings|regulation|fair (?:market )?price|\$0\.50|official|dispute\w*|before|by|no later than|at least|more than|or more)\b/gi;
const NUM = /(\d{1,2}:\d{2}(?:\s?[AP]M)?(?:\s?E[DS]T)?|\$?\d[\d,]*(?:\.\d+)?%?\+?|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.? \d{1,2}(?:, \d{4})?)/gi;
const IS_RISK = new RegExp(`^(?:${RISK.source})$`, "i");
export function highlight(text) {
  const out = [];
  const re = new RegExp(`${RISK.source}|${NUM.source}`, "gi");
  let last = 0;
  for (const m of (text || "").matchAll(re)) {
    if (m.index > last) out.push({ t: text.slice(last, m.index) });
    out.push({ t: m[0], kind: IS_RISK.test(m[0]) ? "risk" : "num" });
    last = m.index + m[0].length;
  }
  if (last < (text || "").length) out.push({ t: text.slice(last) });
  return out;
}

// ---------- toasts ----------
export function toast(message) {
  const id = Math.random();
  setStore((s) => ({ toasts: [...(s.toasts || []), { id, message }] }));
  setTimeout(() => setStore((s) => ({ toasts: (s.toasts || []).filter((t) => t.id !== id) })), 3200);
}
