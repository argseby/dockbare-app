// Shared helpers: API client, formatting, toasts, modals, binding.
import { icon } from "./icons.js";

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
export { icon };

// ---------------------------------------------------------------- formatting
export function fmtBytes(n, dec = 1) {
  n = Number(n || 0);
  if (n < 1024) return `${n} B`;
  const u = ["KB", "MB", "GB", "TB", "PB"];
  let i = -1;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n >= 100 ? 0 : dec)} ${u[i]}`;
}
export function fmtTime(ts, opts = {}) {
  if (!ts) return "—";
  const d = new Date(typeof ts === "number" ? ts * 1000 : ts);
  if (isNaN(d)) return "—";
  const tz = localStorage.getItem("dockbare.tz") || undefined;
  return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short", timeZone: tz, ...opts });
}
export function ago(ts) {
  if (!ts) return "never";
  const s = Math.round(Date.now() / 1000 - ts);
  if (s < 0) return `in ${dur(-s)}`;
  if (s < 5) return "just now";
  return `${dur(s)} ago`;
}
export function dur(s) {
  s = Math.max(0, Math.round(s));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  const d = Math.floor(s / 86400);
  return `${d}d ${Math.floor((s % 86400) / 3600)}h`;
}
export function timeEl(ts) {
  if (!ts) return `<span class="muted">—</span>`;
  return `<span title="${esc(fmtTime(ts))}">${esc(ago(ts))}</span>`;
}

// --------------------------------------------------------------------- API
class ApiError extends Error {
  constructor(message, status, data) { super(message); this.status = status; this.data = data; }
}
async function request(method, url, body, opts = {}) {
  const headers = { "X-Requested-With": "Dockbare", Accept: "application/json" };
  const init = { method, headers, credentials: "same-origin" };
  if (body instanceof FormData) init.body = body;
  else if (body !== undefined) { headers["Content-Type"] = "application/json"; init.body = JSON.stringify(body); }
  let r;
  try { r = await fetch(url, init); } catch (e) { throw new ApiError("Network error: " + e.message, 0); }
  if (r.status === 401 && !opts.noRedirect) { location.href = "/login"; throw new ApiError("Session expired", 401); }
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("application/json") ? await r.json().catch(() => ({})) : await r.text();
  if (!r.ok) throw new ApiError((data && data.error) || `Request failed (${r.status})`, r.status, data);
  return data;
}
export const api = {
  get: (u, o) => request("GET", u, undefined, o),
  post: (u, b, o) => request("POST", u, b, o),
  put: (u, b, o) => request("PUT", u, b, o),
  del: (u, b, o) => request("DELETE", u, b, o),
};

// ------------------------------------------------------------------- toasts
export function toast(message, type = "info", ms = 4500) {
  let wrap = $("#toasts");
  if (!wrap) { wrap = document.createElement("div"); wrap.id = "toasts"; wrap.className = "toasts"; document.body.appendChild(wrap); }
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  const ic = { success: "circle-check", error: "circle-x", warning: "alert", info: "info" }[type] || "info";
  el.innerHTML = `${icon(ic, "sm")}<div class="grow">${esc(message)}</div>`;
  wrap.appendChild(el);
  setTimeout(() => el.remove(), ms);
  return el;
}

// ------------------------------------------------------------------- modals
export function modal({ title, body, footer = "", size = "", onClose } = {}) {
  const back = document.createElement("div");
  back.className = "modal-backdrop";
  back.innerHTML = `<div class="modal ${size}" role="dialog" aria-modal="true">
    <div class="modal-h"><h2>${esc(title)}</h2><button class="btn btn-ghost btn-icon close" data-close title="Close">${icon("x")}</button></div>
    <div class="modal-c">${body}</div>
    ${footer ? `<div class="modal-f">${footer}</div>` : ""}
  </div>`;
  const close = () => { if (!back.isConnected) return; back.remove(); document.removeEventListener("keydown", onKey); onClose && onClose(); };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  document.addEventListener("keydown", onKey);
  back.addEventListener("click", (e) => { if (e.target === back) close(); });
  $$("[data-close]", back).forEach((b) => b.addEventListener("click", close));
  document.body.appendChild(back);
  const first = $("input:not([type=hidden]), select, textarea, button.btn-primary", back);
  if (first) setTimeout(() => first.focus(), 30);
  return { el: back, close, modal: $(".modal", back) };
}
export function confirmDialog({ title = "Are you sure?", text = "", confirmLabel = "Confirm", danger = false, extra = "" } = {}) {
  return new Promise((resolve) => {
    const m = modal({
      title, body: `<p>${text}</p>${extra}`,
      footer: `<button class="btn" data-close>Cancel</button><button class="btn ${danger ? "btn-danger" : "btn-primary"}" data-ok>${esc(confirmLabel)}</button>`,
      onClose: () => resolve(false),
    });
    $("[data-ok]", m.el).addEventListener("click", () => { const out = formValues(m.el); m.close(); resolve(out || true); });
  });
}
export function taskLogModal(taskId) {
  const m = modal({ title: "Task", body: `<div class="muted">Loading…</div>`, size: "wide", footer: `<button class="btn" data-close>Close</button>` });
  let timer;
  const load = async () => {
    try {
      const t = await api.get(`/api/tasks/${taskId}`);
      const h = $(".modal-h h2", m.el); if (h) h.textContent = t.title;
      const body = $(".modal-c", m.el);
      const pct = progressPct(t);
      body.innerHTML = `<div class="flex mb">${statusBadge(t.status)}<span class="muted small">${esc(progressText(t))}</span><span class="muted small">${t.finished ? "finished " + esc(ago(t.finished)) : "started " + esc(ago(t.started))}</span></div>
        <div class="progress ${t.status === "success" ? "ok" : t.status === "failure" ? "bad" : ""} ${pct == null && t.status === "running" ? "indeterminate" : ""} mb"><div data-w="${pct == null ? (t.status === "running" ? 40 : 100) : pct}"></div></div>
        ${t.error ? `<div class="banner danger">${icon("alert")}<div>${esc(t.error)}</div></div>` : ""}
        <div class="log">${esc((t.log || []).join("\n") || "No log output.")}</div>`;
      applyWidths(body);
      if (["success", "failure", "cancelled"].includes(t.status)) clearInterval(timer);
    } catch (e) { clearInterval(timer); }
  };
  load();
  timer = setInterval(load, 1500);
  const origClose = m.close;
  m.close = () => { clearInterval(timer); origClose(); };
  $$("[data-close]", m.el).forEach((b) => b.addEventListener("click", () => clearInterval(timer)));
  return m;
}

// ----------------------------------------------------------------- helpers
export function applyWidths(root) {
  $$("[data-w]", root).forEach((el) => { el.style.width = `${Math.max(0, Math.min(100, Number(el.dataset.w)))}%`; });
}
export function progressPct(t) {
  const p = t.progress || {};
  if (p.total && p.bytes != null) return Math.min(100, Math.round((p.bytes / p.total) * 100));
  if (p.upload_total && p.upload_bytes != null) return Math.min(100, Math.round((p.upload_bytes / p.upload_total) * 100));
  if (t.status === "success") return 100;
  return null;
}
export function progressText(t) {
  const p = t.progress || {};
  const parts = [];
  if (p.phase) parts.push(p.phase);
  if (p.item && p.items) parts.push(`${p.item}/${p.items}`);
  if (p.volume && t.kind === "recovery") parts.push(p.volume);
  if (p.upload_bytes != null && p.phase === "uploading") parts.push(`${fmtBytes(p.upload_bytes)}${p.upload_total ? " / " + fmtBytes(p.upload_total) : ""}${p.remote ? " → " + p.remote : ""}`);
  else if (p.bytes != null) parts.push(`${fmtBytes(p.bytes)}${p.total ? " / " + fmtBytes(p.total) : ""}`);
  return parts.join(" · ");
}
export function statusBadge(status) {
  const map = { running: ["info", "loader", "Running"], queued: ["", "clock", "Queued"], success: ["ok", "check", "Success"], failure: ["danger", "x", "Failed"], cancelled: ["warn", "x", "Cancelled"] };
  const [cls, ic, label] = map[status] || ["", "info", status];
  return `<span class="badge ${cls}">${icon(ic, status === "running" ? "spin" : "")} ${label}</span>`;
}
export function levelBadge(level) {
  const cls = { info: "info", success: "ok", warning: "warn", error: "danger" }[level] || "";
  return `<span class="badge ${cls}">${esc(level)}</span>`;
}
export function bind(root, handlers) {
  $$("[data-action]", root).forEach((el) => {
    const name = el.dataset.action;
    if (!handlers[name]) return;
    const evt = el.dataset.on || (el.tagName === "SELECT" || el.tagName === "INPUT" ? "change" : "click");
    el.addEventListener(evt, (e) => { if (evt === "click") e.preventDefault(); handlers[name](el, e); });
  });
}
export function formValues(root) {
  const out = {};
  $$("[name]", root).forEach((el) => {
    const n = el.name;
    if (el.type === "checkbox") {
      if (el.dataset.multi) { (out[n] ||= []); if (el.checked) out[n].push(el.value); }
      else out[n] = el.checked;
    } else if (el.type === "radio") { if (el.checked) out[n] = el.value; }
    else if (el.type === "number") out[n] = el.value === "" ? null : Number(el.value);
    else out[n] = el.value;
  });
  return out;
}
export async function busy(btn, fn) {
  if (!btn) return fn();
  const old = btn.innerHTML; btn.classList.add("loading"); btn.disabled = true;
  btn.innerHTML = `${icon("loader", "spin")} ${old.replace(/<svg.*?<\/svg>\s*/s, "")}`;
  try { return await fn(); } finally { btn.classList.remove("loading"); btn.disabled = false; btn.innerHTML = old; }
}
export function switchHtml(name, checked, label = "", attrs = "") {
  return `<label class="check"><span class="switch"><input type="checkbox" name="${esc(name)}" ${checked ? "checked" : ""} ${attrs}><span class="track"></span></span>${label ? `<span>${label}</span>` : ""}</label>`;
}
export function emptyState(text, ic = "box") {
  return `<div class="empty">${icon(ic)}<div>${esc(text)}</div></div>`;
}
export function downloadUrl(path, decrypt = false) {
  return `/api/backups/download?path=${encodeURIComponent(path)}${decrypt ? "&decrypt=1" : ""}`;
}
export function copyText(text) {
  navigator.clipboard?.writeText(text).then(() => toast("Copied", "success", 1500)).catch(() => toast("Copy failed", "error"));
}
export function remoteBadge(r, health) {
  const h = health ?? r.health;
  if (!r.enabled) return `<span class="badge outline">${esc(r.name)} · disabled</span>`;
  if (!h) return `<span class="badge">${icon("loader", "spin")} ${esc(r.name)}</span>`;
  if (h.ok === true) return `<span class="badge ok" title="${esc(h.message || "")}">${icon("check")} ${esc(r.name)}</span>`;
  if (h.ok === false) return `<span class="badge danger" title="${esc(h.message || "")}">${icon("x")} ${esc(r.name)}</span>`;
  return `<span class="badge outline">${esc(r.name)}</span>`;
}
export function sparkline(samples, key = "total_bytes") {
  if (!samples || samples.length < 2) return `<div class="muted small">Not enough data yet.</div>`;
  const w = 400, h = 64, pad = 2;
  const xs = samples.map((s) => s.ts), ys = samples.map((s) => s[key]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs) || x0 + 1, y1 = Math.max(...ys) || 1;
  const pt = (s) => [pad + ((s.ts - x0) / (x1 - x0 || 1)) * (w - 2 * pad), h - pad - (s[key] / y1) * (h - 2 * pad)];
  const pts = samples.map(pt);
  const line = pts.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const area = `${line} L${pts[pts.length - 1][0].toFixed(1)},${h} L${pts[0][0].toFixed(1)},${h} Z`;
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><path class="area" d="${area}"/><path d="${line}"/></svg>`;
}

export function fileInput(name, accept = "", hint = "or drop a file here") {
  return `<label class="file"><input type="file" name="${esc(name)}" accept="${esc(accept)}"><span class="file-btn">${icon("upload", "sm")} Choose file</span><span class="file-name">No file selected</span><span class="file-hint">${esc(hint)}</span></label>`;
}
function fileLabelUpdate(input) {
  const label = input.closest(".file");
  if (!label) return;
  const f = input.files && input.files[0];
  label.classList.toggle("has-file", !!f);
  label.querySelector(".file-name").textContent = f ? `${f.name} (${fmtBytes(f.size)})` : "No file selected";
}
document.addEventListener("change", (e) => { if (e.target.matches(".file input[type=file]")) fileLabelUpdate(e.target); });
document.addEventListener("dragover", (e) => { const l = e.target.closest?.(".file"); if (l) { e.preventDefault(); l.classList.add("drag"); } });
document.addEventListener("dragleave", (e) => { const l = e.target.closest?.(".file"); if (l) l.classList.remove("drag"); });
document.addEventListener("drop", (e) => {
  const l = e.target.closest?.(".file");
  if (!l) return;
  e.preventDefault();
  l.classList.remove("drag");
  const input = l.querySelector("input[type=file]");
  if (input && e.dataTransfer?.files?.length) { try { input.files = e.dataTransfer.files; fileLabelUpdate(input); } catch { /* read-only in some browsers */ } }
});

export function keepFocus(root, renderFn) {
  // Remember which input (by data-action or name) had focus, re-render, then restore focus and caret.
  const a = document.activeElement;
  const key = a && root.contains(a) ? (a.dataset.action ? `[data-action="${a.dataset.action}"]` : a.name ? `[name="${a.name}"]` : null) : null;
  const pos = key && a.selectionEnd != null ? a.selectionEnd : null;
  const out = renderFn();
  const restore = () => { if (!key) return; const el = root.querySelector(key); if (el) { el.focus({ preventScroll: true }); try { if (pos != null) el.setSelectionRange(pos, pos); } catch { /* not a text input */ } } };
  if (out && typeof out.then === "function") return out.then((v) => { restore(); return v; });
  restore();
  return out;
}
