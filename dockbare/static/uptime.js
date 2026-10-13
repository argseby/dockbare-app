// Public status page (no login). Reads /api/uptime/public.
import { historyBars, combine, legend, axis, enableBarTooltips, avgPct } from "./bars.js";
import { icon } from "./icons.js";

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (ts) => (ts ? new Date(ts * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "");
const ago = (ts) => { const s = Math.max(0, Math.round(Date.now() / 1000 - ts)); return s < 45 ? "just now" : s < 60 ? `${s}s ago` : s < 3600 ? `${Math.floor(s / 60)} min ago` : s < 86400 ? `${Math.floor(s / 3600)} h ago` : `${Math.floor(s / 86400)} d ago`; };
const dur = (s) => { s = Math.round(s || 0); if (s < 60) return `${s}s`; if (s < 3600) return `${Math.floor(s / 60)} min`; if (s < 86400) return `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min`; return `${Math.floor(s / 86400)} d ${Math.floor((s % 86400) / 3600)} h`; };
const pct = (v) => (v == null ? "—" : v >= 99.995 ? "100%" : `${v.toFixed(2)}%`);
const HERO = {
  operational: ["circle-check", "All systems operational"], degraded: ["alert", "Partial outage"],
  outage: ["circle-x", "Major outage"], maintenance: ["wrench", "Maintenance in progress"],
};
const STATUS_LABEL = { investigating: "Investigating", identified: "Identified", monitoring: "Monitoring", resolved: "Resolved", scheduled: "Scheduled", in_progress: "In progress", completed: "Completed" };
const INC_STEPS = ["investigating", "identified", "monitoring", "resolved"];
const STATUS_ICON = { investigating: "search", identified: "eye", monitoring: "activity", resolved: "check", scheduled: "calendar", in_progress: "wrench", completed: "check" };
const stIcon = (st) => icon(STATUS_ICON[st] || "info", "sm");
const KIND_LABEL = { down: "exited", dead: "dead", oom: "out of memory", unhealthy: "unhealthy", "restart-loop": "restart loop", missing: "missing" };

const theme = {
  get: () => localStorage.getItem("dockbare.theme") || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"),
  apply() { document.documentElement.dataset.theme = this.get(); $("#theme").innerHTML = icon(this.get() === "dark" ? "sun" : "moon"); },
  toggle() { localStorage.setItem("dockbare.theme", this.get() === "dark" ? "light" : "dark"); this.apply(); },
};

function announcement(a, group) {
  const isInc = a.type === "incident";
  const kind = !isInc ? "maintenance" : a.status === "resolved" ? "resolved" : a.severity === "major" ? "major" : "minor";
  const badge = !isInc ? `<span class="badge solid-info">${icon("wrench", "sm")} maintenance</span>` : a.status === "resolved" ? `<span class="badge solid-ok">${icon("check", "sm")} resolved</span>` : a.severity === "major" ? `<span class="badge solid-danger">${icon("alert", "sm")} major</span>` : `<span class="badge solid-warn">${icon("alert", "sm")} minor</span>`;
  const when = isInc
    ? (a.status === "resolved" ? `${icon("clock", "sm")} ${fmt(a.starts_at)} → ${fmt(a.resolved_at)} · ${dur(a.resolved_at - a.starts_at)}` : `<span class="live"></span>${icon("clock", "sm")} ${fmt(a.starts_at)} · ${dur(Date.now() / 1000 - a.starts_at)}`)
    : `${a.status === "in_progress" ? '<span class="live"></span>' : ""}${icon("calendar", "sm")} ${fmt(a.starts_at)} → ${fmt(a.ends_at)}${a.status === "scheduled" ? ` · in ${dur(a.starts_at - Date.now() / 1000)}` : ""}`;
  const steps = isInc ? `<div class="steps">${INC_STEPS.map((st, i) => `<span class="${st === a.status ? "now" : i < INC_STEPS.indexOf(a.status) ? "done" : ""}" title="${STATUS_LABEL[st]}">${stIcon(st)} ${STATUS_LABEL[st]}</span>`).join("")}</div>` : "";
  const ups = a.updates || [];
  const upd = (u) => `<div class="upd"><div><div class="st">${stIcon(u.status)} ${esc(STATUS_LABEL[u.status] || u.status)}</div><div class="tm">${esc(fmt(u.ts))}</div></div><div class="m">${esc(u.message || (u.status === ups[ups.length - 1].status ? "" : `Status → ${STATUS_LABEL[u.status] || u.status}`))}</div></div>`;
  const updHtml = ups.length ? `${ups.slice(0, 3).map(upd).join("")}${ups.length > 3 ? `<div class="upd-more hidden">${ups.slice(3).map(upd).join("")}</div><button type="button" class="btn btn-sm btn-ghost upd-toggle" data-more="${ups.length - 3}">${icon("chevron-down", "sm")} ${ups.length - 3} more</button>` : ""}` : (a.message ? `<div class="msg">${esc(a.message)}</div>` : "");
  return `<div class="st-ann st-ann--${kind}"><div class="grow"><div class="t"><b>${esc(a.title)}</b>${badge}${!isInc ? `<span class="badge outline">${stIcon(a.status)} ${esc(STATUS_LABEL[a.status] || a.status)}</span>` : ""}${group ? `<span class="badge outline">${icon("globe", "sm")} ${esc(group)}</span>` : ""}</div><div class="meta"><span>${when}</span>${a.services && a.services.length ? `<span>${icon("container", "sm")} ${esc(a.services.join(", "))}</span>` : ""}</div>${steps}${updHtml}</div></div>`;
}

function service(i) {
  const down = i.status !== "up";
  return `<div class="st-svc ${down ? "is-down" : ""}">
    <div class="row"><span class="dot ${down ? "bad" : "ok"}"></span><b class="name">${esc(i.name)}</b>${down ? `<span class="badge solid-danger">${icon("x", "sm")} ${esc(KIND_LABEL[i.kind] || i.kind || "down")}</span>` : `<span class="badge ok" title="operational">${icon("check", "sm")}</span>`}
      <span class="stats" title="availability over the last 24 hours, 7 days and 30 days"><span><em>24h</em>${pct(i.uptime.d1)}</span><span><em>7d</em>${pct(i.uptime.d7)}</span><span><em>30d</em>${pct(i.uptime.d30)}</span></span></div>
    ${down ? `<div class="since">${icon("clock", "sm")} since ${esc(fmt(i.since))}</div>` : ""}
    ${i.history ? historyBars(i.history) : ""}</div>`;
}

let timer;
async function load() {
  let d;
  try { d = await fetch("/api/uptime/public", { cache: "no-store" }).then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); }); }
  catch (e) { $("#hero").className = "st-hero st-hero--unknown"; $("#hero").innerHTML = `${icon("alert")}<div><h1>Status page unavailable</h1><p>Could not load data (${esc(e.message)}).</p></div>`; return; }
  document.title = d.title; $("#title").textContent = d.title;
  const items = d.groups.flatMap((g) => g.items.map((i) => ({ ...i, group: g.name })));
  const downItems = items.filter((i) => i.status !== "up");
  const offline = d.groups.filter((g) => !g.online);
  const active = d.groups.flatMap((g) => (g.announcements || []).filter((a) => a.active || a.upcoming).map((a) => ({ ...a, group: g.self ? null : g.name })));
  const activeInc = active.filter((a) => a.type === "incident" && a.active);
  const inMaint = active.filter((a) => a.type === "maintenance" && a.active);
  const [ic, text] = HERO[d.overall] || ["help", d.overall];
  const subBits = [];
  if (downItems.length) subBits.push(`${downItems.length} of ${items.length} service(s) down`);
  if (offline.length) subBits.push(`${offline.map((g) => g.name).join(", ")} unreachable`);
  activeInc.forEach((a) => subBits.push(`${a.title} — ${(STATUS_LABEL[a.status] || a.status).toLowerCase()}${a.updates && a.updates[0] ? `, last update ${ago(a.updates[0].ts)}` : ""}`));
  if (inMaint.length) subBits.push(`maintenance: ${inMaint.map((a) => a.title).join(", ")}`);
  if (!subBits.length) subBits.push(`${items.length} service(s) across ${d.groups.length} location(s) · 30-day availability ${pct(avgPct(items))}`);
  const chips = [["check", items.length - downItems.length, "operational"], ["x", downItems.length, "down"], ["alert", activeInc.length, activeInc.length === 1 ? "incident" : "incidents"], ["wrench", inMaint.length + active.filter((a) => a.upcoming).length, "maintenance"]]
    .filter(([, n], i) => n || i === 0).map(([ic2, n, l]) => `<span class="chip" title="${l}">${icon(ic2, "sm")} ${n}<span class="lbl"> ${l}</span></span>`).join("");
  $("#hero").className = `st-status st-status--${d.overall}`;
  $("#hero").innerHTML = `${icon(ic)}<span class="txt">${esc(text)}</span><span class="chips">${chips}</span>`;
  const nInc = activeInc.length, nMaint = active.length - activeInc.length;
  $("#announcements").innerHTML = active.length
    ? `<div class="st-section"><div class="st-section-h"><h2>Active</h2><span class="muted">${[nInc ? `${nInc} ${nInc === 1 ? "incident" : "incidents"}` : "", nMaint ? `${nMaint} maintenance` : ""].filter(Boolean).join(" · ")}</span></div>${active.map((a) => announcement(a, a.group)).join("")}</div>`
    : `<div class="st-section"><div class="st-section-h"><h2>Active</h2></div><div class="st-tl"><div class="st-empty">No active incidents or maintenance.</div></div></div>`;
  $("#notices").innerHTML = active.length ? active.map((a) => { const isInc = a.type === "incident"; const cls = !isInc ? "info" : a.severity === "major" ? "danger" : "warn"; const last = (a.updates || [])[0];
    return `<a class="st-notice st-notice--${cls}" href="#incidents"><div class="n-top">${isInc && a.active ? '<span class="live"></span>' : ""}<span class="badge solid-${cls === "danger" ? "danger" : cls === "warn" ? "warn" : "info"}">${icon(isInc ? "alert" : "wrench", "sm")} ${isInc ? (a.severity === "major" ? "major" : "minor") : "maintenance"}</span><b>${esc(a.title)}</b><span class="more">${icon("chevron-right", "sm")}</span></div>
      <div class="n-sub"><span class="st">${stIcon(a.status)} ${esc(STATUS_LABEL[a.status] || a.status)}</span>${last ? ` · ${icon("clock", "sm")} ${esc(ago(last.ts))}` : ""}${!isInc && a.upcoming ? ` · ${icon("calendar", "sm")} ${esc(fmt(a.starts_at))}` : ""}${last && last.message ? ` — ${esc(last.message)}` : ""}</div></a>`; }).join("") : "";
  const hists = items.map((i) => i.history || []).filter((h) => h.length);
  $("#overview").innerHTML = hists.length ? `<div class="st-section"><div class="st-overview"><div class="st-section-h"><h2>${icon("chart", "sm")} Last ${d.history_days} days</h2><span class="muted" title="30-day availability, all services">${icon("activity", "sm")} ${pct(avgPct(items))}</span></div>${historyBars(combine(hists), { height: 22 })}${axis(d.history_days)}${legend()}</div></div>` : "";
  const groupName = (g) => g.name;
  $("#groups").innerHTML = `<div class="st-section">${d.groups.map((g) => `<div class="st-group"><div class="st-group-h"><h2>${icon(g.self ? "server" : "globe", "sm")} ${esc(groupName(g))}</h2>${g.self ? "" : `<span class="badge ${g.online ? "ok" : "solid-danger"}">${icon(g.online ? "check" : "x", "sm")} ${g.online ? "online" : "unreachable"}</span>`}<span class="muted">${g.items.length ? `<span title="30-day availability">${icon("activity", "sm")} ${pct(avgPct(g.items))}</span>` : ""}${g.checked_at ? ` · <span title="last check">${icon("clock", "sm")} ${esc(ago(g.checked_at))}</span>` : ""}</span></div>${g.items.length ? g.items.map(service).join("") : `<div class="st-empty">${g.online ? "No public services listed." : "No data while unreachable."}</div>`}</div>`).join("")}</div>`;
  const hist = [...(d.announcements || []).map((a) => ({ name: a.title, kind: a.type === "incident" ? `${a.severity || ""} incident`.trim() : "maintenance", cls: a.type === "incident" ? (a.severity === "major" ? "bad" : "warn") : "info", started_at: a.starts_at, ended_at: a.resolved_at || a.ends_at, duration: (a.resolved_at || a.ends_at || Date.now() / 1000) - a.starts_at, message: a.message, updates: a.updates })),
    ...(d.incidents || []).map((i) => ({ ...i, cls: i.ended_at ? "ok" : "bad" }))].sort((a, b) => b.started_at - a.started_at);
  const days = new Map();
  hist.forEach((i) => { const k = new Date(i.started_at * 1000).toLocaleDateString(undefined, { weekday: "long", year: "numeric", month: "long", day: "numeric" }); (days.get(k) || days.set(k, []).get(k)).push(i); });
  $("#incidents-count").textContent = active.length ? String(active.length) : "";
  $("#history").innerHTML = `<div class="st-section-h"><h2>${icon("clock", "sm")} Past ${d.history_days} days</h2><span class="muted">${hist.length ? `${hist.length} ${hist.length === 1 ? "event" : "events"}` : ""}</span></div>${hist.length ? [...days.entries()].map(([day, list]) => `<div class="st-day"><h3>${esc(day)}</h3><div class="st-tl">${list.map((i) => `<div class="item"><span class="dot ${i.cls || "ok"}"></span><div><b>${esc(i.name)}</b> <span class="badge outline">${esc(KIND_LABEL[i.kind] || i.kind)}</span>${i.ended_at ? "" : '<span class="badge solid-danger">ongoing</span>'}${i.message ? `<div class="msg">${esc(i.message)}</div>` : ""}</div><div class="when">${icon("clock", "sm")} ${esc(new Date(i.started_at * 1000).toLocaleTimeString(undefined, { timeStyle: "short" }))}${i.ended_at ? `<br>${esc(dur(i.duration))}` : ""}</div></div>`).join("")}</div></div>`).join("") : `<div class="st-tl"><div class="st-empty">No incidents in the last ${d.history_days} days.</div></div>`}`;
  $("#foot").innerHTML = `${icon("refresh", "sm")} ${esc(new Date(d.updated * 1000).toLocaleTimeString(undefined, { timeStyle: "short" }))} · <a href="/admin" title="Sign in to Dockbare">Dockbare</a>`;
}
function showTab() {
  const tab = (location.hash === "#incidents" || location.hash === "#history") ? "incidents" : "status";
  document.querySelectorAll("#tabs a").forEach((a) => a.classList.toggle("active", a.dataset.tab === tab));
  $("#tab-status").classList.toggle("hidden", tab !== "status");
  $("#tab-incidents").classList.toggle("hidden", tab !== "incidents");
  if (tab === "incidents") window.scrollTo({ top: 0 });
}
document.addEventListener("click", (e) => {
  const t = e.target.closest?.(".upd-toggle");
  if (!t) return;
  const more = t.previousElementSibling;
  more.classList.toggle("hidden");
  t.textContent = more.classList.contains("hidden") ? `Show all ${Number(t.dataset.more) + 3} updates` : "Show fewer";
});
window.addEventListener("hashchange", showTab);
document.querySelectorAll(".ti[data-icon]").forEach((el) => { el.outerHTML = icon(el.dataset.icon, "sm"); });
theme.apply();
$("#theme").addEventListener("click", () => theme.toggle());
enableBarTooltips();
showTab();
load();
timer = setInterval(load, 60000);
