// Shell: navigation, routing, theme, task polling.
import { $, $$, esc, icon, api, toast, taskLogModal, applyWidths } from "./ui.js";
import * as P from "./pages.js";
import * as P2 from "./pages2.js";

const NAV = [
  ["dashboard", "Dashboard", "dashboard"], ["volumes", "Volumes & Backups", "database"], ["schedules", "Schedules", "calendar"],
  ["remotes", "Remotes", "server"], ["peers", "Peers", "heart-pulse"], ["uptime", "Uptime", "radar"], ["recovery", "Recovery", "life-buoy"], ["reports", "Reports", "chart"],
  ["activity", "Activity", "activity"], ["settings", "Settings", "settings"],
];
const PAGES = { dashboard: P.dashboard, volumes: P.volumes, schedules: P.schedules, remotes: P2.remotes, peers: P2.peers, uptime: P2.uptime, recovery: P2.recovery, reports: P2.reports, activity: P2.activity, settings: P2.settings };

const theme = {
  get: () => localStorage.getItem("dockbare.theme") || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"),
  apply() { document.documentElement.dataset.theme = this.get(); const b = $("#theme-btn"); if (b) { b.innerHTML = `${icon(this.get() === "dark" ? "sun" : "moon")} <span class="lbl">${this.get() === "dark" ? "Light mode" : "Dark mode"}</span>`; b.title = this.get() === "dark" ? "Light mode" : "Dark mode"; } },
  toggle() { localStorage.setItem("dockbare.theme", this.get() === "dark" ? "light" : "dark"); this.apply(); },
};

let current = null, currentKey = "";
async function route() {
  const hash = location.hash.replace(/^#\/?/, "");
  const [key, sub] = hash.split("/");
  const page = PAGES[key] || PAGES.dashboard;
  const pageKey = PAGES[key] ? key : "dashboard";
  $$(".nav a").forEach((a) => a.classList.toggle("active", a.dataset.page === pageKey));
  $("#page-title").textContent = page.title;
  document.title = `${page.title} · Dockbare`;
  $("#page-actions").innerHTML = page.actions ? page.actions() : "";
  $$("#page-actions [data-action]").forEach((b) => b.addEventListener("click", (e) => { e.preventDefault(); page.onAction?.[b.dataset.action]?.(b, { refresh: () => refresh(true) }); }));
  current = page; currentKey = pageKey;
  closeNav();
  await refresh(true, sub);
}

let rendering = false, pendingRefresh = null;
async function refresh(force = false, sub) {
  if (!current) return;
  if (!force && (document.querySelector(".modal-backdrop") || (document.activeElement && document.activeElement.matches("input, select, textarea") && $("#page").contains(document.activeElement)))) return;
  if (rendering) { pendingRefresh = { force, sub }; return; }  // serialise renders; the page re-renders into #page itself
  rendering = true;
  const el = $("#page");
  if (el.dataset.key !== currentKey) { el.innerHTML = `<div class="muted flex">${icon("loader", "spin")} Loading…</div>`; el.dataset.key = currentKey; }
  try {
    await current.render(el, sub);
  } catch (e) {
    el.innerHTML = `<div class="banner danger">${icon("alert")}<div><b>Could not load this page.</b> ${esc(e.message)}</div></div>`;
  } finally {
    rendering = false;
    if (pendingRefresh) { const p = pendingRefresh; pendingRefresh = null; refresh(p.force, p.sub); }
  }
}
window.addEventListener("dockbare:refresh", () => refresh(true));

// ---- task polling -------------------------------------------------------
const known = new Map();
let pollTimer = null;
async function pollTasks() {
  try {
    const tasks = await api.get("/api/tasks", { noRedirect: true });
    const bar = $("#taskbar");
    const active = tasks.filter((t) => ["running", "queued"].includes(t.status));
    let changed = false;
    for (const t of tasks) {
      const prev = known.get(t.id);
      if (prev && prev !== t.status && !["running", "queued"].includes(t.status)) {
        changed = true;
        if (t.status === "success") toast(`${t.title}: done`, "success");
        else if (t.status === "failure") toast(`${t.title}: ${t.error || "failed"}`, "error", 9000);
        else toast(`${t.title}: ${t.status}`, "warning");
      }
      known.set(t.id, t.status);
    }
    bar.innerHTML = active.slice(0, 4).map((t) => P.taskCard(t)).join("");
    applyWidths(bar);
    $$("[data-task]", bar).forEach((c) => c.addEventListener("click", () => taskLogModal(c.dataset.task)));
    if (changed) refresh(false);
    clearTimeout(pollTimer);
    pollTimer = setTimeout(pollTasks, active.length ? 2000 : 6000);
  } catch (e) {
    if (e.status === 401) { location.href = "/login"; return; }
    clearTimeout(pollTimer);
    pollTimer = setTimeout(pollTasks, 10000);
  }
}

// ---- nav (mobile) -------------------------------------------------------
function openNav() { $(".sidebar").classList.add("open"); $(".backdrop-nav").classList.add("open"); }
function closeNav() { $(".sidebar").classList.remove("open"); $(".backdrop-nav").classList.remove("open"); }
function toggleNav() {
  if (matchMedia("(max-width: 900px)").matches) { $(".sidebar").classList.contains("open") ? closeNav() : openNav(); return; }
  const app = $(".app");
  app.classList.toggle("collapsed");
  localStorage.setItem("dockbare.nav", app.classList.contains("collapsed") ? "collapsed" : "open");
}

async function init() {
  theme.apply();
  let session;
  try { session = await api.get("/api/auth/session", { noRedirect: true }); } catch { session = null; }
  if (!session || !session.authenticated) { location.href = "/login"; return; }
  if (location.pathname !== "/admin") history.replaceState(null, "", "/admin" + location.hash);
  $("#brand-version").textContent = `v${session.version}`;
  $(".nav").innerHTML = NAV.map(([k, label, ic]) => `<a href="#${k}" data-page="${k}" title="${esc(label)}">${icon(ic)} <span class="lbl">${esc(label)}</span></a>`).join("");
  $("#logout-btn").title = "Sign out";
  if (localStorage.getItem("dockbare.nav") === "collapsed") $(".app").classList.add("collapsed");
  $("#theme-btn").addEventListener("click", () => theme.toggle());
  $("#logout-btn").addEventListener("click", async () => { try { await api.post("/api/auth/logout", {}); } finally { location.href = "/login"; } });
  $("#menu-btn").addEventListener("click", toggleNav);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeNav(); });
  $(".backdrop-nav").addEventListener("click", closeNav);
  window.addEventListener("hashchange", route);
  await route();
  pollTasks();
  const tick = () => { if (document.visibilityState === "visible") refresh(false); setTimeout(tick, (current && current.pollMs) || 30000); };
  setTimeout(tick, (current && current.pollMs) || 30000);
}
init();
