// Pages: dashboard, volumes & backups, schedules (+ shared backup / restore / prune dialogs)
import { $, $$, esc, icon, api, toast, modal, confirmDialog, taskLogModal, fmtBytes, fmtTime, ago, dur, timeEl, bind, formValues, busy, switchHtml, emptyState, downloadUrl, applyWidths, progressPct, progressText, statusBadge, remoteBadge, sparkline, copyText, fileInput, keepFocus } from "./ui.js";

const isAnonVol = (v) => /^[0-9a-f]{64}$/.test(v.name);
export const sortVolumes = (vols) => [...vols].sort((a, b) => (isAnonVol(a) - isAnonVol(b)) || a.name.localeCompare(b.name));
export const state = { overview: null, volumes: [], backups: [], remotes: [], schedules: [], expanded: new Set(), volFilter: "", volSearch: "" };

// ------------------------------------------------------------------ shared
export async function loadRemotes() { state.remotes = await api.get("/api/remotes"); return state.remotes; }

function remoteCheckList(selected, name = "remotes") {
  const rs = state.remotes.filter((r) => r.enabled);
  if (!rs.length) return `<div class="help">No remotes configured. Backups stay local only. <a href="#remotes">Add a remote</a>.</div>`;
  return `<div class="check-list">${rs.map((r) => `<label class="check"><input type="checkbox" name="${name}" data-multi="1" value="${esc(r.id)}" ${selected.includes(r.id) ? "checked" : ""}> ${esc(r.name)} <span class="muted small">${esc(r.type.toUpperCase())} · ${esc(r.config.host)}</span></label>`).join("")}</div>`;
}

export async function backupDialog(preselect = []) {
  const [volumes] = await Promise.all([api.get("/api/volumes"), loadRemotes()]);
  const ov = state.overview || (await api.get("/api/overview"));
  const encOn = ov.encryption.configured;
  const vols = sortVolumes(volumes.filter((v) => !v.missing));
  const body = `
    <div class="form-group"><label class="field">Volumes</label>
      <div class="search mb">${icon("search", "sm")}<input type="search" placeholder="Filter volumes…" data-action="filter"></div>
      <div class="check-list" data-vols>${vols.map((v) => `<label class="check" data-name="${esc(v.name.toLowerCase())}"><input type="checkbox" name="volumes" data-multi="1" value="${esc(v.name)}" ${preselect.includes(v.name) ? "checked" : ""}> <span class="ellipsis">${esc(v.name)}</span> ${v.containers.length ? `<span class="muted small">${esc(v.containers.map((c) => c.name).join(", "))}</span>` : ""}</label>`).join("")}</div>
      <div class="flex small mt"><a href="#" data-action="all">Select all</a> · <a href="#" data-action="none">None</a> <span class="muted grow right" data-count></span></div>
      <div class="estimate small mt" data-estimate></div>
    </div>
    <div class="form-group"><label class="field">Upload to remotes</label>${remoteCheckList(state.remotes.filter((r) => r.enabled).map((r) => r.id))}</div>
    <div class="form-row">
      <div><label class="field">Encryption</label>
        <select name="encrypt" ${encOn ? "" : "disabled"}><option value="">Default (${ov.encryption.default && encOn ? "encrypt" : "plain"})</option><option value="yes">Encrypt</option><option value="no">Plain</option></select>
        ${encOn ? "" : `<div class="help">No encryption key configured (DOCKBARE_ENCRYPTION_KEY).</div>`}</div>
      <div><label class="field">Containers</label>${switchHtml("stop_containers", true, "Stop containers during backup")}<div class="help">Guarantees a consistent snapshot.</div></div>
    </div>`;
  const m = modal({ title: "Create backup", body, size: "wide", footer: `<button class="btn" data-close>Cancel</button><button class="btn btn-primary" data-action="go">${icon("play")} Start backup</button>` });
  const sizes = {};
  let estTimer = null;
  const estimate = async () => {
    const sel = $$("input[name=volumes]:checked", m.el).map((i) => i.value);
    const box = $("[data-estimate]", m.el);
    if (!sel.length) { box.innerHTML = ""; return; }
    const missing = sel.filter((v) => !(v in sizes)).slice(0, 25);
    if (missing.length) {
      box.innerHTML = `${icon("loader", "spin sm")} measuring ${missing.length} volume(s)…`;
      try { const r = await api.get(`/api/volumes/estimate?volumes=${encodeURIComponent(missing.join(","))}`); r.items.forEach((it) => (sizes[it.volume] = it)); } catch (e) { box.innerHTML = `<span class="danger">${esc(e.message)}</span>`; return; }
    }
    const rows = sel.map((v) => sizes[v]).filter(Boolean);
    const raw = rows.reduce((a, r) => a + (r.bytes || 0), 0), est = rows.reduce((a, r) => a + (r.estimated || 0), 0);
    const unknown = rows.filter((r) => r.bytes == null).length;
    box.innerHTML = `${icon("hard-drive", "sm")} <b>${esc(fmtBytes(raw))}</b> of data in ${sel.length} volume(s) → about <b>${esc(fmtBytes(est))}</b> per backup${unknown ? ` <span class="muted">(${unknown} could not be measured)</span>` : ""} <span class="muted">· compression from ${rows.some((r) => r.ratio_source === "history") ? "your previous backups" : "a default 60% ratio"}</span>`;
    $$("[data-vols] label", m.el).forEach((l) => { const v = l.querySelector("input").value; const sz = sizes[v]; const tag = l.querySelector(".vsize") || l.appendChild(Object.assign(document.createElement("span"), { className: "muted small vsize" })); tag.textContent = sz ? (sz.bytes == null ? "?" : `${fmtBytes(sz.bytes)} → ~${fmtBytes(sz.estimated)}`) : ""; });
  };
  const count = () => { $("[data-count]", m.el).textContent = `${$$("input[name=volumes]:checked", m.el).length} selected`; clearTimeout(estTimer); estTimer = setTimeout(estimate, 300); };
  bind(m.el, {
    filter: (el) => { const q = el.value.toLowerCase(); $$("[data-vols] label", m.el).forEach((l) => l.classList.toggle("hidden", !l.dataset.name.includes(q))); },
    all: () => { $$("[data-vols] label:not(.hidden) input", m.el).forEach((i) => (i.checked = true)); count(); },
    none: () => { $$("[data-vols] input", m.el).forEach((i) => (i.checked = false)); count(); },
    go: async (btn) => {
      const f = formValues(m.el);
      if (!f.volumes?.length) return toast("Select at least one volume", "warning");
      await busy(btn, async () => {
        const r = await api.post("/api/backups", { volumes: f.volumes, remotes: f.remotes || [], encrypt: f.encrypt === "" ? null : f.encrypt === "yes", stop_containers: f.stop_containers });
        toast(`${r.tasks.length} backup task(s) started`, "success");
        m.close();
      }).catch((e) => toast(e.message, "error"));
    },
  });
  $("[data-vols]", m.el).addEventListener("change", count);
  count();
}

export async function restoreDialog(backup) {
  const volumes = sortVolumes((await api.get("/api/volumes")).filter((v) => !v.missing));
  const body = `
    <div class="banner danger">${icon("alert")}<div><b>Destructive operation.</b> All current data in the target volume is deleted and replaced by the backup. Containers using the volume are stopped and restarted.</div></div>
    <dl class="kv mb"><dt>Backup</dt><dd class="mono">${esc(backup.volume)}/${esc(backup.filename)}</dd><dt>Created</dt><dd>${esc(fmtTime(backup.created_at))}</dd><dt>Size</dt><dd>${esc(fmtBytes(backup.size))} ${backup.encrypted ? `<span class="badge ok">${icon("lock")} encrypted</span>` : ""}</dd></dl>
    <div class="form-group"><label class="field">Target volume</label><select name="volume">${volumes.map((v) => `<option value="${esc(v.name)}" ${v.name === backup.volume ? "selected" : ""}>${esc(v.name)}${v.containers.length ? ` (${v.containers.map((c) => c.name).join(", ")})` : ""}</option>`).join("")}</select>
    ${volumes.some((v) => v.name === backup.volume) ? "" : `<div class="help">Volume <b>${esc(backup.volume)}</b> does not exist on this host. Use <a href="#recovery">Recovery</a> to create it, or pick another target.</div>`}</div>
    <label class="check"><input type="checkbox" name="sure"> I understand that the target volume will be wiped.</label>`;
  const m = modal({ title: "Restore backup", body, footer: `<button class="btn" data-close>Cancel</button><button class="btn btn-danger" data-action="go">${icon("restore")} Restore</button>` });
  bind(m.el, {
    go: async (btn) => {
      const f = formValues(m.el);
      if (!f.sure) return toast("Please confirm that you understand", "warning");
      await busy(btn, async () => {
        const r = await api.post("/api/backups/restore", { path: backup.path, volume: f.volume });
        toast("Restore started", "success"); m.close(); taskLogModal(r.task.id);
      }).catch((e) => toast(e.message, "error"));
    },
  });
}

export async function pruneDialog(volume) {
  await loadRemotes();
  const body = `
    <p class="muted small">Delete old backups of <b>${esc(volume)}</b>. The newest backup is always kept.</p>
    <div class="form-row cols-3">
      <div><label class="field">Older than (days)</label><input type="number" name="days" min="0" value="30"></div>
      <div><label class="field">Older than (hours)</label><input type="number" name="hours" min="0" value="0"></div>
      <div><label class="field">Keep at most</label><input type="number" name="keep_max" min="0" value="0" placeholder="0 = unlimited"></div>
    </div>
    <div class="form-group mt">${switchHtml("delete_remote", false, "Also delete on remotes")}</div>
    <div class="mt"><div class="muted small" data-summary>Preview…</div><div class="log mt" data-preview></div></div>`;
  const m = modal({ title: "Prune backups", body, footer: `<button class="btn" data-close>Cancel</button><button class="btn btn-danger" data-action="go" disabled>${icon("scissors")} Delete</button>` });
  const preview = async () => {
    const f = formValues(m.el);
    const keep_seconds = (f.days || 0) * 86400 + (f.hours || 0) * 3600;
    try {
      const r = await api.post("/api/prune", { volume, keep_seconds, keep_max: f.keep_max || 0, dry_run: true });
      $("[data-summary]", m.el).textContent = `${r.deleted.length} backup(s) would be deleted, freeing ${fmtBytes(r.bytes)}; ${r.kept} kept.`;
      $("[data-preview]", m.el).textContent = r.deleted.map((d) => `${d.filename}  ${fmtBytes(d.size)}  ${fmtTime(d.created_at)}`).join("\n") || "Nothing matches.";
      $("[data-action=go]", m.el).disabled = r.deleted.length === 0;
    } catch (e) { $("[data-summary]", m.el).textContent = e.message; }
  };
  $$("input", m.el).forEach((i) => i.addEventListener("input", preview));
  preview();
  bind(m.el, {
    go: async (btn) => {
      const f = formValues(m.el);
      await busy(btn, async () => {
        const r = await api.post("/api/prune", { volume, keep_seconds: (f.days || 0) * 86400 + (f.hours || 0) * 3600, keep_max: f.keep_max || 0, delete_remote: f.delete_remote });
        toast(`Deleted ${r.deleted.length} backup(s), freed ${fmtBytes(r.bytes)}`, "success"); m.close(); window.dispatchEvent(new Event("dockbare:refresh"));
      }).catch((e) => toast(e.message, "error"));
    },
  });
}

export async function uploadDialog(preselect = "") {
  const volumes = sortVolumes((await api.get("/api/volumes")).filter((v) => !v.missing));
  const body = `<p class="muted small">Upload an existing <code>.tar.gz</code> or <code>.tar.gz.enc</code> backup into the local backup store.</p>
    <div class="form-group"><label class="field">Volume</label><select name="volume">${volumes.map((v) => `<option value="${esc(v.name)}" ${v.name === preselect ? "selected" : ""}>${esc(v.name)}</option>`).join("")}</select></div>
    <div class="form-group"><label class="field">File</label>${fileInput("file", ".gz,.enc,application/gzip")}</div>
    <div class="progress hidden" data-prog><div data-w="0"></div></div>`;
  const m = modal({ title: "Upload backup file", body, footer: `<button class="btn" data-close>Cancel</button><button class="btn btn-primary" data-action="go">${icon("upload")} Upload</button>` });
  bind(m.el, {
    go: async (btn) => {
      const file = $("input[type=file]", m.el).files[0];
      if (!file) return toast("Choose a file", "warning");
      const fd = new FormData(); fd.append("volume", $("select[name=volume]", m.el).value); fd.append("file", file);
      $("[data-prog]", m.el).classList.remove("hidden");
      await busy(btn, () => new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open("POST", "/api/backups/upload");
        xhr.setRequestHeader("X-Requested-With", "Dockbare");
        xhr.upload.onprogress = (e) => { if (e.lengthComputable) { $("[data-prog] div", m.el).style.width = `${Math.round((e.loaded / e.total) * 100)}%`; } };
        xhr.onload = () => { try { const d = JSON.parse(xhr.responseText); xhr.status < 300 ? resolve(d) : reject(new Error(d.error || "Upload failed")); } catch { reject(new Error("Upload failed")); } };
        xhr.onerror = () => reject(new Error("Upload failed"));
        xhr.send(fd);
      })).then(() => { toast("Backup uploaded", "success"); m.close(); window.dispatchEvent(new Event("dockbare:refresh")); }).catch((e) => toast(e.message, "error"));
    },
  });
}

async function pushDialog(backup) {
  await loadRemotes();
  const m = modal({ title: `Upload ${backup.filename} to remotes`, body: `<div class="form-group">${remoteCheckList([])}</div>`, footer: `<button class="btn" data-close>Cancel</button><button class="btn btn-primary" data-action="go">${icon("cloud-upload")} Upload</button>` });
  bind(m.el, { go: async (btn) => { const f = formValues(m.el); if (!f.remotes?.length) return toast("Select a remote", "warning"); await busy(btn, async () => { const r = await api.post("/api/backups/push", { path: backup.path, remotes: f.remotes }); m.close(); taskLogModal(r.task.id); }).catch((e) => toast(e.message, "error")); } });
}

async function backupEventsDialog(backup) {
  const m = modal({ title: `History of ${backup.filename}`, size: "wide", body: `<div class="muted">Loading…</div>`, footer: `<button class="btn" data-close>Close</button>` });
  try {
    const ev = await api.get(`/api/backups/events?path=${encodeURIComponent(backup.path)}`);
    $(".modal-c", m.el).innerHTML = ev.length ? `<div class="events">${ev.map(eventRow).join("")}</div>` : emptyState("No events recorded for this file.", "activity");
  } catch (e) { $(".modal-c", m.el).innerHTML = `<div class="banner danger">${esc(e.message)}</div>`; }
}

export function eventRow(e) {
  return `<div class="event level-${esc(e.level)}"><span class="dot"></span><div><div>${esc(e.message)}</div><div class="kind">${esc(e.kind)}${e.volume ? " · " + esc(e.volume) : ""}</div></div><div class="time" title="${esc(fmtTime(e.ts))}">${esc(ago(e.ts))}</div></div>`;
}

export function taskCard(t, compact = false) {
  const pct = progressPct(t);
  const meta = t.status === "running" || t.status === "queued" ? (progressText(t) || t.status) : t.status === "success" ? ("done" + (t.result?.size ? " · " + fmtBytes(t.result.size) : "") + (t.finished ? " · " + ago(t.finished) : "")) : (t.error || t.status);
  return `<div class="task" data-task="${esc(t.id)}"><div class="t">${t.status === "running" ? icon("loader", "spin sm") : t.status === "success" ? icon("circle-check", "sm") : icon("circle-x", "sm")}<span class="ellipsis grow">${esc(t.title)}</span><span class="meta" title="${esc(meta)}">${esc(meta)}</span></div>
    <div class="progress ${t.status === "success" ? "ok" : t.status === "failure" ? "bad" : ""} ${pct == null && t.status === "running" ? "indeterminate" : ""}"><div data-w="${pct == null ? (t.status === "running" ? 40 : 100) : pct}"></div></div></div>`;
}

// --------------------------------------------------------------- dashboard
export const dashboard = {
  title: "Dashboard",
  actions: () => `<button class="btn" data-action="sync">${icon("refresh")} Sync remotes</button><button class="btn btn-primary" data-action="backup">${icon("plus")} Backup now</button>`,
  async render(el) {
    const seq = (this._seq = (this._seq || 0) + 1);
    const o = (state.overview = await api.get("/api/overview"));
    const running = o.tasks.filter((t) => ["running", "queued"].includes(t.status));
    const recent = o.tasks.filter((t) => !["running", "queued"].includes(t.status)).slice(0, 5);
    const sched = await api.get("/api/schedules");
    const upcoming = sched.filter((s) => s.enabled && s.next_run).sort((a, b) => a.next_run - b.next_run).slice(0, 8);
    const remotesOk = o.remotes.filter((r) => r.enabled && r.health?.ok).length, remotesOn = o.remotes.filter((r) => r.enabled).length;
    const peers = o.peers || [];
    const peersDown = peers.filter((p) => p.enabled && (p.alert_active || p.ok === false));
    const up = o.uptime || null;
    const banners = [];
    (o.security || []).forEach((w) => banners.push(`<div class="banner warn">${icon("shield-alert")}<div>${esc(w)}</div></div>`));
    if (up && up.down.length) banners.push(`<div class="banner danger">${icon("radar")}<div><b>Container${up.down.length > 1 ? "s" : ""} down:</b> ${esc(up.down.join(", "))}. <a href="#uptime">Uptime</a></div></div>`);
    if (peersDown.length) banners.push(`<div class="banner danger">${icon("heart-pulse")}<div><b>Peer instance down:</b> ${esc(peersDown.map((p) => p.name + (p.error ? " — " + p.error : "")).join("; "))}. <a href="#peers">Peers</a></div></div>`);
    if (!o.docker.ok) banners.push(`<div class="banner danger">${icon("alert")}<div><b>Docker is not reachable.</b> ${esc(o.docker.error || "")} Make sure <code>/var/run/docker.sock</code> is mounted.</div></div>`);
    if (!o.encryption.configured) banners.push(`<div class="banner warn">${icon("unlock")}<div><b>Encryption is off.</b> Backups are stored and uploaded in plain text. Set <code>DOCKBARE_ENCRYPTION_KEY</code> in your <code>.env</code> and restart to encrypt new backups.</div></div>`);
    if (!o.remotes.length) banners.push(`<div class="banner info">${icon("cloud-upload")}<div>No remote configured yet. <a href="#remotes">Add an FTP, FTPS or SFTP remote</a> to keep off-site copies.</div></div>`);
    if (o.remotes.some((r) => r.enabled && r.health?.ok === false)) banners.push(`<div class="banner danger">${icon("server")}<div><b>Remote unreachable:</b> ${esc(o.remotes.filter((r) => r.enabled && r.health?.ok === false).map((r) => r.name + " — " + (r.health.message || "")).join("; "))}</div></div>`);
    if (seq !== this._seq) return;
    el.innerHTML = `${banners.join("")}
      <div class="grid cols-5 mb">
        <div class="card stat"><div class="label">${icon("box", "sm")} Volumes protected</div><div class="value">${o.volumes.protected} <span class="muted">/ ${o.volumes.total}</span></div><div class="sub">${o.volumes.scheduled} scheduled</div></div>
        <div class="card stat"><div class="label">${icon("hard-drive", "sm")} Backups</div><div class="value">${o.backups.count}</div><div class="sub">${esc(fmtBytes(o.backups.bytes))} on disk${o.disk.free != null ? ` · ${esc(fmtBytes(o.disk.free))} free` : ""}${o.forecast ? `<br><span title="projected local usage after ${o.forecast.days} days with the current schedules and retention${o.forecast.unknown.length ? `; no size known yet for ${esc(o.forecast.unknown.join(", "))}` : ""}">≈ ${esc(fmtBytes(o.forecast.projected))} in ${o.forecast.days} days${o.forecast.unknown.length ? " + ?" : ""}</span>` : ""}</div></div>
        <div class="card stat"><div class="label">${icon("server", "sm")} Remotes</div><div class="value">${remotesOk} <span class="muted">/ ${remotesOn}</span></div><div class="sub">${o.remotes.length ? "reachable" : "none configured"}</div></div>
        <div class="card stat"><div class="label">${icon(o.encryption.configured ? "lock" : "unlock", "sm")} Encryption</div><div class="value">${o.encryption.configured ? "On" : "Off"}</div><div class="sub">${o.encryption.configured ? `key ${esc(o.encryption.fingerprint)} · ${o.backups.encrypted}/${o.backups.count} files` : "no key configured"}</div></div>
        <div class="card stat"><div class="label">${icon("calendar", "sm")} Next backup</div><div class="value">${o.next_run ? esc(ago(o.next_run).replace(" ago", "")) : "—"}</div><div class="sub">${o.next_run ? esc(fmtTime(o.next_run)) : "no active schedule"}</div></div>
      </div>
      <div class="grid cols-2 mb">
        <div class="card"><div class="card-h"><h2>Tasks</h2><div class="actions muted small">${running.length} running</div></div><div class="card-c">
          ${running.length || recent.length ? `<div class="stack">${running.map((t) => taskCard(t)).join("")}${recent.map((t) => taskCard(t)).join("")}</div>` : emptyState("No tasks yet. Start a backup to see progress here.", "activity")}</div></div>
        <div class="card"><div class="card-h"><h2>Upcoming</h2><div class="actions"><a class="btn btn-sm btn-ghost" href="#schedules">Schedules ${icon("arrow-right", "sm")}</a></div></div><div class="card-c">
          ${upcoming.length ? `<div class="list">${upcoming.map((s) => `<div><span class="grow ellipsis">${icon("box", "sm")} ${esc(s.volume)}</span><span class="muted small">${esc(s.interval_str)}</span><span class="nowrap" title="${esc(fmtTime(s.next_run))}">${esc(ago(s.next_run))}</span></div>`).join("")}</div>` : emptyState("No schedules active.", "calendar")}</div></div>
      </div>
      <div class="grid cols-2">
        <div class="card"><div class="card-h"><h2>Recent activity</h2><div class="actions"><a class="btn btn-sm btn-ghost" href="#activity">All ${icon("arrow-right", "sm")}</a></div></div><div class="card-c">${o.recent_events.length ? `<div class="events">${o.recent_events.map(eventRow).join("")}</div>` : emptyState("Nothing happened yet.", "activity")}</div></div>
        <div class="stack">
          <div class="card"><div class="card-h"><h2>Storage (30 days)</h2><div class="actions muted small">${esc(fmtBytes(o.backups.bytes))}</div></div><div class="card-c">${sparkline(o.storage_history)}</div></div>
          <div class="card"><div class="card-h"><h2>Remotes</h2><div class="actions"><a class="btn btn-sm btn-ghost" href="#remotes">Manage ${icon("arrow-right", "sm")}</a></div></div><div class="card-c">${o.remotes.length ? `<div class="pills">${o.remotes.map((r) => remoteBadge(r)).join("")}</div><div class="help mt">Auto-sync ${o.sync.enabled ? `every ${o.sync.interval_minutes} min · next ${esc(ago(o.sync.next_run))}` : "disabled"}</div>` : `<div class="muted small">None configured.</div>`}</div></div>
          ${up ? `<div class="card"><div class="card-h"><h2>Uptime</h2><div class="actions"><a class="btn btn-sm btn-ghost" href="#uptime">Details ${icon("arrow-right", "sm")}</a></div></div><div class="card-c"><div class="flex"><span class="badge ${up.down.length ? "danger" : "ok"}">${icon(up.down.length ? "x" : "check", "sm")} ${up.down.length ? `${up.down.length} down` : "all up"}</span><span class="muted small">${up.monitored} of ${up.total} containers monitored · ${up.incidents_24h} incident(s) in 24h</span></div></div></div>` : ""}
          ${peers.length ? `<div class="card"><div class="card-h"><h2>Peers</h2><div class="actions"><a class="btn btn-sm btn-ghost" href="#peers">Manage ${icon("arrow-right", "sm")}</a></div></div><div class="card-c"><div class="pills">${peers.map((p) => `<span class="badge ${!p.enabled ? "outline" : p.alert_active || p.ok === false ? "danger" : p.ok ? (p.problems.length ? "warn" : "ok") : ""}" title="${esc(p.error || p.problems.join("; ") || (p.checked_at ? "checked " + ago(p.checked_at) : "not checked yet"))}">${icon(p.alert_active || p.ok === false ? "x" : p.ok ? (p.problems.length ? "alert" : "check") : "clock", "sm")} ${esc(p.name)}</span>`).join("")}</div></div></div>` : ""}
        </div>
      </div>`;
    applyWidths(el);
    $$("[data-task]", el).forEach((c) => c.addEventListener("click", () => taskLogModal(c.dataset.task)));
  },
  onAction: { backup: () => backupDialog(), sync: async (btn) => busy(btn, async () => { const r = await api.post("/api/remotes/sync", {}); taskLogModal(r.task.id); }).catch((e) => toast(e.message, "error")) },
};

// ----------------------------------------------------------------- volumes
export const volumes = {
  title: "Volumes & Backups",
  actions: () => `<button class="btn" data-action="upload">${icon("upload")} Upload file</button><button class="btn btn-primary" data-action="backup">${icon("plus")} Backup now</button>`,
  async render(el) {
    const seq = (this._seq = (this._seq || 0) + 1);
    const [vols, backups] = await Promise.all([api.get("/api/volumes"), api.get("/api/backups"), loadRemotes()]);
    state.volumes = vols; state.backups = backups;
    const byVol = {}; backups.forEach((b) => (byVol[b.volume] ||= []).push(b));
    const q = state.volSearch.toLowerCase();
    const isAnon = (v) => /^[0-9a-f]{64}$/.test(v.name);
    const anonCount = vols.filter(isAnon).length;
    vols.sort((a, b) => (isAnon(a) - isAnon(b)) || ((b.backup_count > 0) - (a.backup_count > 0)) || ((b.containers.length > 0) - (a.containers.length > 0)) || a.name.localeCompare(b.name));
    const filtered = vols.filter((v) => (state.showAnon || !isAnon(v) || v.backup_count || v.schedule) && (!q || v.name.toLowerCase().includes(q) || v.containers.some((c) => c.name.toLowerCase().includes(q))) &&
      (state.volFilter === "" || (state.volFilter === "protected" && v.backup_count) || (state.volFilter === "unprotected" && !v.backup_count) || (state.volFilter === "scheduled" && v.schedule) || (state.volFilter === "missing" && v.missing)));
    if (seq !== this._seq) return;
    el.innerHTML = `
      <div class="flex flex-wrap mb">
        <div class="search grow">${icon("search", "sm")}<input type="search" placeholder="Search volumes or containers…" value="${esc(state.volSearch)}" data-action="search" data-on="input"></div>
        <div class="seg">${[["", "All"], ["protected", "Backed up"], ["unprotected", "No backup"], ["scheduled", "Scheduled"], ["missing", "Orphaned"]].map(([k, l]) => `<button class="${state.volFilter === k ? "active" : ""}" data-action="filter" data-v="${k}">${l}</button>`).join("")}</div>
        <span class="muted small nowrap">${filtered.length} of ${vols.length}</span>
      </div>
      ${anonCount ? `<div class="flex mb small muted"><label class="check"><input type="checkbox" data-action="anon" ${state.showAnon ? "checked" : ""}> Show ${anonCount} anonymous volume(s) (64-character names created implicitly by Docker)</label></div>` : ""}
      ${filtered.length ? filtered.map((v) => volCard(v, byVol[v.name] || [])).join("") : emptyState(vols.length ? "No volumes match." : "No Docker volumes found.", "box")}`;
    applyWidths(el);
    bind(el, {
      search: (i) => { state.volSearch = i.value; clearTimeout(this._t); this._t = setTimeout(() => keepFocus(el, () => this.render(el)), 200); },
      filter: (b) => { state.volFilter = b.dataset.v; this.render(el); },
      anon: (i) => { state.showAnon = i.checked; this.render(el); },
      toggle: (h) => { const n = h.dataset.vol; state.expanded.has(n) ? state.expanded.delete(n) : state.expanded.add(n); this.render(el); },
      backupVol: (b) => backupDialog([b.dataset.vol]),
      measure: async (a) => { const box = $(`[data-usage="${CSS.escape(a.dataset.vol)}"]`, el); box.innerHTML = `· ${icon("loader", "spin sm")} measuring…`; try { const r = await api.get(`/api/volumes/estimate?volumes=${encodeURIComponent(a.dataset.vol)}`); const it = r.items[0]; box.innerHTML = it.bytes == null ? "· size unknown" : `· ${esc(fmtBytes(it.bytes))} of data on the volume → about ${esc(fmtBytes(it.estimated))} per backup`; } catch (e) { box.innerHTML = `· ${esc(e.message)}`; } },
      prune: (b) => pruneDialog(b.dataset.vol),
      uploadVol: (b) => uploadDialog(b.dataset.vol),
      schedule: (b) => scheduleDialog(b.dataset.vol).then(() => this.render(el)),
      restore: (b) => restoreDialog(findBackup(b.dataset.path)),
      verify: async (b) => { try { const r = await api.post("/api/backups/verify", { path: b.dataset.path }); taskLogModal(r.task.id); } catch (e) { toast(e.message, "error"); } },
      push: (b) => pushDialog(findBackup(b.dataset.path)),
      events: (b) => backupEventsDialog(findBackup(b.dataset.path)),
      download: (b) => { const bk = findBackup(b.dataset.path); if (!bk.encrypted) return (location.href = downloadUrl(bk.path)); const m = modal({ title: "Download", body: `<p>This backup is encrypted. Download it as stored, or let Dockbare decrypt it on the fly.</p>`, footer: `<a class="btn" href="${downloadUrl(bk.path)}" data-close>${icon("lock")} Encrypted (.enc)</a><a class="btn btn-primary" href="${downloadUrl(bk.path, true)}" data-close>${icon("unlock")} Decrypted (.tar.gz)</a>` }); },
      del: async (b) => {
        const bk = findBackup(b.dataset.path);
        const synced = Object.values(bk.uploads || {}).some((u) => u.status === "synced");
        const ok = await confirmDialog({ title: "Delete backup", text: `Delete <b>${esc(bk.filename)}</b> (${esc(fmtBytes(bk.size))}) of <b>${esc(bk.volume)}</b>? This cannot be undone.`, confirmLabel: "Delete", danger: true, extra: synced ? `<label class="check mt"><input type="checkbox" name="delete_remote"> Also delete the copies on remotes</label>` : "" });
        if (!ok) return;
        try { await api.del("/api/backups", { path: bk.path, delete_remote: !!ok.delete_remote }); toast("Backup deleted", "success"); this.render(el); } catch (e) { toast(e.message, "error"); }
      },
      task: (b) => taskLogModal(b.dataset.task),
    });
    function findBackup(p) { return state.backups.find((b) => b.path === p); }
  },
  onAction: { backup: () => backupDialog(), upload: () => uploadDialog() },
};

function volCard(v, backups) {
  const open = state.expanded.has(v.name);
  const last = v.last_backup;
  const ctr = v.containers.map((c) => `<span class="ctr" title="${esc(c.image || "")}${c.project ? " · project " + esc(c.project) : ""}"><span class="dot ${c.status === "running" ? "ok" : "off"}"></span>${esc(c.name)}</span>`).join(" ");
  const s = v.schedule;
  const lastUploads = last ? Object.entries(last.uploads || {}) : [];
  return `<div class="vol ${v.missing ? "missing" : ""}">
    <div class="vol-h" data-action="toggle" data-vol="${esc(v.name)}">
      <div class="name">${icon(open ? "chevron-down" : "chevron-right", "sm")}<span class="ellipsis" title="${esc(v.name)}">${esc(v.name)}</span>${v.missing ? `<span class="badge warn">volume missing</span>` : ""}${v.active_task ? `<span class="badge info">${icon("loader", "spin")} ${esc(v.active_task.kind)}</span>` : ""}</div>
      <div><div class="cell-label">Containers</div>${ctr || `<span class="muted small">no container</span>`}</div>
      <div><div class="cell-label">Schedule</div>${s ? `<span class="badge ${s.enabled ? "ok" : "outline"}">${icon(s.enabled ? "calendar" : "pause", "sm")} ${esc(s.interval_str)}</span>` : `<span class="muted small">none</span>`}</div>
      <div><div class="cell-label">Last backup</div>${last ? `<div>${timeEl(last.created_at)} ${last.encrypted ? icon("lock", "sm") : ""}</div><div class="sub">${v.backup_count} backups · ${esc(fmtBytes(v.backup_bytes))}</div>` : `<span class="muted small">never</span>`}</div>
      <div class="act flex gap-2">${v.missing ? "" : `<button class="btn btn-sm" data-action="backupVol" data-vol="${esc(v.name)}" title="Backup now">${icon("play")} Backup</button>`}<button class="btn btn-sm btn-ghost btn-icon" data-action="schedule" data-vol="${esc(v.name)}" title="${s ? "Edit schedule" : "Add schedule"}">${icon("calendar")}</button></div>
    </div>
    ${open ? `<div class="vol-b">
      <div class="vol-toolbar"><span class="muted small grow">${backups.length} backup(s), ${esc(fmtBytes(backups.reduce((a, b) => a + b.size, 0)))} <span data-usage="${esc(v.name)}" class="muted">· <a href="#" data-action="measure" data-vol="${esc(v.name)}">measure volume size</a></span></span>
        <button class="btn btn-sm" data-action="uploadVol" data-vol="${esc(v.name)}">${icon("upload")} Upload file</button>
        ${backups.length > 1 ? `<button class="btn btn-sm" data-action="prune" data-vol="${esc(v.name)}">${icon("scissors")} Prune</button>` : ""}</div>
      ${backups.length ? `<div class="table-wrap"><table><thead><tr><th>Created</th><th>Size</th><th>Security</th><th>Remotes</th><th>Source</th><th></th></tr></thead><tbody>
        ${backups.map((b) => `<tr>
          <td><div>${esc(fmtTime(b.created_at))}</div><div class="muted small mono">${esc(b.filename)}</div></td>
          <td>${esc(fmtBytes(b.size))}${b.meta?.raw_bytes ? `<div class="muted small">${esc(fmtBytes(b.meta.raw_bytes))} raw</div>` : ""}</td>
          <td>${b.encrypted ? `<span class="badge ok">${icon("lock")} encrypted</span>` : `<span class="badge warn">${icon("unlock")} plain</span>`}${b.meta?.verify ? `<div class="small mt ${b.meta.verify.ok ? "muted" : "danger"}" title="verified ${esc(fmtTime(b.meta.verified_at))}">${b.meta.verify.ok ? `${icon("shield-check", "sm")} ${b.meta.verify.files} files` : `${icon("shield-alert", "sm")} verify failed`}</div>` : ""}</td>
          <td><div class="pills">${state.remotes.filter((r) => r.enabled).map((r) => { const u = b.uploads?.[r.id]; const st = u?.status; return `<span class="badge ${st === "synced" ? "ok" : st === "failed" ? "danger" : "outline"}" title="${esc(u?.error || (u ? fmtTime(u.updated_at) : "not uploaded"))}">${icon(st === "synced" ? "check" : st === "failed" ? "x" : "clock", "sm")} ${esc(r.name)}</span>`; }).join("") || `<span class="muted small">—</span>`}</div></td>
          <td class="muted small">${esc(b.source || "")}${b.meta?.hostname ? `<div>${esc(b.meta.hostname)}</div>` : ""}</td>
          <td><div class="actions">
            <button class="btn btn-sm btn-ghost btn-icon" data-action="restore" data-path="${esc(b.path)}" title="Restore">${icon("restore")}</button>
            <button class="btn btn-sm btn-ghost btn-icon" data-action="verify" data-path="${esc(b.path)}" title="Verify integrity">${icon("shield-check")}</button>
            <button class="btn btn-sm btn-ghost btn-icon" data-action="push" data-path="${esc(b.path)}" title="Upload to remote">${icon("cloud-upload")}</button>
            <button class="btn btn-sm btn-ghost btn-icon" data-action="download" data-path="${esc(b.path)}" title="Download">${icon("download")}</button>
            <button class="btn btn-sm btn-ghost btn-icon" data-action="events" data-path="${esc(b.path)}" title="History">${icon("file-text")}</button>
            <button class="btn btn-sm btn-danger-ghost btn-icon" data-action="del" data-path="${esc(b.path)}" title="Delete">${icon("trash")}</button>
          </div></td></tr>`).join("")}</tbody></table></div>` : emptyState("No backups of this volume yet.", "hard-drive")}
    </div>` : ""}
  </div>`;
}

// --------------------------------------------------------------- schedules
export async function scheduleDialog(volume = null) {
  const [vols, schedules, settings] = await Promise.all([api.get("/api/volumes"), api.get("/api/schedules"), api.get("/api/settings"), loadRemotes()]);
  const existing = volume ? schedules.find((s) => s.volume === volume) : null;
  const s = existing || { mode: "daily", time: "03:00", weekdays: [0, 1, 2, 3, 4, 5, 6], interval_seconds: 86400, keep_seconds: 30 * 86400, keep_max: 0, remotes: state.remotes.filter((r) => r.enabled).map((r) => r.id), encrypt: null, stop_containers: settings.general.stop_containers_default, notify: true, prune_remote: true, enabled: true };
  const iv = s.interval_seconds || 86400;
  const unit = iv % 86400 === 0 ? 86400 : iv % 3600 === 0 ? 3600 : 60;
  const days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const encOn = settings.encryption.configured;
  const body = `
    <div class="form-group"><label class="field">Volume</label>${existing ? `<input type="text" value="${esc(volume)}" readonly><input type="hidden" name="volume" value="${esc(volume)}">` : `<select name="volume">${sortVolumes(vols.filter((v) => !v.missing)).map((v) => `<option value="${esc(v.name)}" ${v.name === volume ? "selected" : ""} ${schedules.some((x) => x.volume === v.name) ? "disabled" : ""}>${esc(v.name)}${schedules.some((x) => x.volume === v.name) ? " (already scheduled)" : ""}</option>`).join("")}</select>`}</div>
    <div class="form-group"><label class="field">When</label>
      <div class="seg mb"><button type="button" class="${s.mode === "daily" ? "active" : ""}" data-mode="daily">At a fixed time</button><button type="button" class="${s.mode === "interval" ? "active" : ""}" data-mode="interval">Every N hours/days</button></div>
      <input type="hidden" name="mode" value="${esc(s.mode)}">
      <div data-pane="daily" class="${s.mode === "daily" ? "" : "hidden"}"><div class="form-row"><div><label class="field">Time (${esc(settings.env.timezone)})</label><input type="time" name="time" value="${esc(s.time || "03:00")}"></div>
        <div><label class="field">Days</label><div class="flex flex-wrap gap-2">${days.map((d, i) => `<label class="check"><input type="checkbox" name="weekdays" data-multi="1" value="${i}" ${(s.weekdays || []).includes(i) ? "checked" : ""}>${d}</label>`).join("")}</div></div></div></div>
      <div data-pane="interval" class="${s.mode === "interval" ? "" : "hidden"}"><div class="form-row"><div><label class="field">Every</label><input type="number" name="ivn" min="1" value="${iv / unit}"></div><div><label class="field">Unit</label><select name="ivu"><option value="60" ${unit === 60 ? "selected" : ""}>minutes</option><option value="3600" ${unit === 3600 ? "selected" : ""}>hours</option><option value="86400" ${unit === 86400 ? "selected" : ""}>days</option></select></div></div></div>
    </div>
    <div class="form-group"><label class="field">Retention</label><div class="form-row"><div><label class="field muted">Delete backups older than (days, 0 = never)</label><input type="number" name="keep_days" min="0" value="${Math.round((s.keep_seconds || 0) / 86400)}"></div><div><label class="field muted">Keep at most (0 = unlimited)</label><input type="number" name="keep_max" min="0" value="${s.keep_max || 0}"></div></div><div class="help">Applied after each scheduled run. The newest backup is never deleted.</div></div>
    <div class="form-group"><label class="field">Upload to remotes</label>${remoteCheckList(s.remotes || [])}</div>
    <div class="form-row"><div><label class="field">Encryption</label><select name="encrypt" ${encOn ? "" : "disabled"}><option value="" ${s.encrypt == null ? "selected" : ""}>Default (${settings.general.encrypt_default && encOn ? "encrypt" : "plain"})</option><option value="yes" ${s.encrypt === true ? "selected" : ""}>Encrypt</option><option value="no" ${s.encrypt === false ? "selected" : ""}>Plain</option></select></div>
      <div class="stack gap-2"><label class="field">Options</label>${switchHtml("stop_containers", s.stop_containers, "Stop containers during backup")}${switchHtml("notify", s.notify, "Send notifications")}${switchHtml("prune_remote", s.prune_remote, "Apply retention on remotes too")}${switchHtml("enabled", s.enabled, "Schedule enabled")}</div></div>`;
  return new Promise((resolve) => {
    const m = modal({ title: existing ? `Edit schedule: ${volume}` : "Add schedule", body, size: "wide", footer: `${existing ? `<button class="btn btn-danger-ghost left" data-action="delete">${icon("trash")} Remove</button>` : ""}<button class="btn" data-close>Cancel</button><button class="btn btn-primary" data-action="save">${icon("check")} Save</button>`, onClose: () => resolve(false) });
    $$("[data-mode]", m.el).forEach((b) => b.addEventListener("click", () => { $$("[data-mode]", m.el).forEach((x) => x.classList.toggle("active", x === b)); $("input[name=mode]", m.el).value = b.dataset.mode; $$("[data-pane]", m.el).forEach((p) => p.classList.toggle("hidden", p.dataset.pane !== b.dataset.mode)); }));
    bind(m.el, {
      save: async (btn) => {
        const f = formValues(m.el);
        const payload = { volume: f.volume, mode: f.mode, time: f.time, weekdays: (f.weekdays || []).map(Number), interval_seconds: (f.ivn || 1) * Number(f.ivu), keep_seconds: (f.keep_days || 0) * 86400, keep_max: f.keep_max || 0, remotes: f.remotes || [], encrypt: f.encrypt === "" ? null : f.encrypt === "yes", stop_containers: f.stop_containers, notify: f.notify, prune_remote: f.prune_remote, enabled: f.enabled };
        await busy(btn, async () => { existing ? await api.put(`/api/schedules/${encodeURIComponent(volume)}`, payload) : await api.post("/api/schedules", payload); toast("Schedule saved", "success"); resolve(true); m.close(); }).catch((e) => toast(e.message, "error"));
      },
      delete: async () => { if (!(await confirmDialog({ title: "Remove schedule", text: `Remove the schedule for <b>${esc(volume)}</b>? Existing backups are kept.`, confirmLabel: "Remove", danger: true }))) return; try { await api.del(`/api/schedules/${encodeURIComponent(volume)}`); toast("Schedule removed", "success"); resolve(true); m.close(); } catch (e) { toast(e.message, "error"); } },
    });
  });
}

export const schedules = {
  title: "Schedules",
  actions: () => `<button class="btn btn-primary" data-action="add">${icon("plus")} Add schedule</button>`,
  async render(el) {
    const seq = (this._seq = (this._seq || 0) + 1);
    const [list, settings] = await Promise.all([api.get("/api/schedules"), api.get("/api/settings"), loadRemotes()]);
    state.schedules = list;
    const encOn = settings.encryption.configured, encDefault = settings.general.encrypt_default;
    const encBadge = (s) => { const wants = s.encrypt === true || (s.encrypt == null && encDefault); if (wants && encOn) return `<span class="badge ok" title="encrypted${s.encrypt == null ? " (default)" : ""}">${icon("lock", "sm")}</span>`; if (wants && !encOn) return `<span class="badge danger" title="Encryption requested but DOCKBARE_ENCRYPTION_KEY is not set: backups are written in plain text">${icon("unlock", "sm")} no key</span>`; return `<span class="badge warn" title="plain${s.encrypt == null ? " (default)" : ""}">${icon("unlock", "sm")}</span>`; };
    const rname = (id) => state.remotes.find((r) => r.id === id)?.name || "?";
    if (seq !== this._seq) return;
    el.innerHTML = `${!encOn && list.some((x) => x.encrypt === true || (x.encrypt == null && encDefault)) ? `<div class="banner danger">${icon("unlock")}<div><b>No encryption key configured.</b> Schedules set to encrypt will write plain backups until <code>DOCKBARE_ENCRYPTION_KEY</code> is set and the container restarted.</div></div>` : ""}<div class="card"><div class="card-c tight">${list.length ? `<div class="table-wrap"><table><thead><tr><th>Volume</th><th>When</th><th>Retention</th><th>Remotes</th><th>Options</th><th>Next run</th><th></th></tr></thead><tbody>
      ${list.map((s) => `<tr>
        <td><div class="flex">${icon("box", "sm")} <b>${esc(s.volume)}</b></div></td>
        <td>${s.enabled ? `<span class="badge ok">${esc(s.interval_str)}</span>` : `<span class="badge outline">${icon("pause", "sm")} paused · ${esc(s.interval_str)}</span>`}</td>
        <td class="small">${s.keep_seconds ? `older than ${Math.round(s.keep_seconds / 86400)}d` : ""}${s.keep_seconds && s.keep_max ? ", " : ""}${s.keep_max ? `max ${s.keep_max}` : ""}${!s.keep_seconds && !s.keep_max ? `<span class="muted">keep all</span>` : ""}${s.prune_remote && (s.keep_seconds || s.keep_max) ? `<div class="muted">+ remotes</div>` : ""}</td>
        <td><div class="pills">${s.remotes.length ? s.remotes.map((id) => `<span class="badge">${esc(rname(id))}</span>`).join("") : `<span class="muted small">local only</span>`}</div></td>
        <td><div class="pills">${encBadge(s)}${s.stop_containers ? `<span class="badge outline" title="stops containers">${icon("container", "sm")}</span>` : ""}${s.notify ? `<span class="badge outline" title="notifications">${icon("bell", "sm")}</span>` : ""}</div></td>
        <td>${s.enabled && s.next_run ? `<div>${esc(ago(s.next_run))}</div><div class="muted small">${esc(fmtTime(s.next_run))}</div>` : `<span class="muted">—</span>`}</td>
        <td><div class="actions">
          <button class="btn btn-sm btn-ghost btn-icon" data-action="run" data-vol="${esc(s.volume)}" title="Run now">${icon("play")}</button>
          <button class="btn btn-sm btn-ghost btn-icon" data-action="toggle" data-vol="${esc(s.volume)}" data-en="${s.enabled ? 0 : 1}" title="${s.enabled ? "Pause" : "Resume"}">${icon(s.enabled ? "pause" : "play")}</button>
          <button class="btn btn-sm btn-ghost btn-icon" data-action="edit" data-vol="${esc(s.volume)}" title="Edit">${icon("pencil")}</button>
        </div></td></tr>`).join("")}</tbody></table></div>` : emptyState("No schedules yet. Add one to back up a volume automatically.", "calendar")}</div></div>`;
    bind(el, {
      edit: (b) => scheduleDialog(b.dataset.vol).then((ch) => ch && this.render(el)),
      toggle: async (b) => { try { await api.post(`/api/schedules/${encodeURIComponent(b.dataset.vol)}/toggle`, { enabled: b.dataset.en === "1" }); this.render(el); } catch (e) { toast(e.message, "error"); } },
      run: async (b) => { try { await api.post(`/api/schedules/${encodeURIComponent(b.dataset.vol)}/run`, {}); toast(`Backup of ${b.dataset.vol} started`, "success"); } catch (e) { toast(e.message, "error"); } },
    });
  },
  onAction: { add: (btn, ctx) => scheduleDialog().then((ch) => ch && ctx.refresh()) },
};
