// Pages: remotes, recovery, reports, activity, settings
import { $, $$, esc, icon, api, toast, modal, confirmDialog, taskLogModal, fmtBytes, fmtTime, ago, dur, timeEl, bind, formValues, busy, switchHtml, emptyState, applyWidths, levelBadge, remoteBadge, copyText, fileInput } from "./ui.js";
import { state, loadRemotes, eventRow } from "./pages.js";
import { historyBars, legend as barsLegend, enableBarTooltips } from "./bars.js";

// ----------------------------------------------------------------- remotes
async function remoteDialog(existing = null, instanceName = "") {
  const r = existing || { type: "ftp", name: "", enabled: true, config: { port: 21, base_path: "/backups", passive: true, secure: true, tls_mode: "explicit", verify_tls: true, instance_dir: true } };
  const c = r.config;
  const body = `
    <div class="form-row"><div><label class="field">Name</label><input type="text" name="name" value="${esc(r.name)}" placeholder="Hetzner Storage Box"></div>
      <div><label class="field">Type</label><select name="type"><option value="ftp" ${r.type === "ftp" ? "selected" : ""}>FTP / FTPS</option><option value="sftp" ${r.type === "sftp" ? "selected" : ""}>SFTP (SSH)</option></select></div></div>
    <div class="form-row mt"><div><label class="field">Host</label><input type="text" name="host" value="${esc(c.host || "")}" placeholder="backup.example.com"></div><div><label class="field">Port</label><input type="number" name="port" value="${esc(c.port || "")}" min="1" max="65535"></div></div>
    <div class="form-row mt"><div><label class="field">Username</label><input type="text" name="username" value="${esc(c.username || "")}" autocomplete="off"></div>
      <div><label class="field">Password ${c.password_set ? `<span class="muted">(stored; leave blank to keep)</span>` : ""}</label><div class="input-group"><input type="password" name="password" autocomplete="new-password" placeholder="${c.password_set ? "••••••••" : ""}"><button type="button" class="btn btn-ghost btn-icon" data-action="eye">${icon("eye", "sm")}</button></div></div></div>
    <div class="form-group mt"><label class="field">Base path on the remote</label><input type="text" name="base_path" value="${esc(c.base_path || "")}" placeholder="/backups"><div class="help">Must not be the root directory.</div></div>
    <div class="form-group">${switchHtml("instance_dir", c.instance_dir !== false, "Subfolder per instance")}<div class="help">On: <code>&lt;base&gt;/${esc(instanceName || "<instance>")}/&lt;volume&gt;/&lt;timestamp&gt;.tar.gz[.enc]</code> — lets several Dockbare instances share this server. Off: <code>&lt;base&gt;/&lt;volume&gt;/…</code> (v2 layout).</div></div>
    <div data-pane="ftp" class="${r.type === "ftp" ? "" : "hidden"}">
      <div class="form-row"><div><label class="field">TLS</label><select name="tls"><option value="none" ${!c.secure ? "selected" : ""}>Plain FTP (insecure)</option><option value="explicit" ${c.secure && c.tls_mode !== "implicit" ? "selected" : ""}>FTPS explicit (AUTH TLS, port 21)</option><option value="implicit" ${c.secure && c.tls_mode === "implicit" ? "selected" : ""}>FTPS implicit (port 990)</option></select></div>
        <div class="stack gap-2"><label class="field">Options</label>${switchHtml("passive", c.passive !== false, "Passive mode")}${switchHtml("nat_pasv_fix", !!c.nat_pasv_fix, "NAT fix (ignore PASV address)")}${switchHtml("verify_tls", c.verify_tls !== false, "Verify TLS certificate")}</div></div>
    </div>
    <div data-pane="sftp" class="${r.type === "sftp" ? "" : "hidden"}">
      <div class="form-group"><label class="field">Private key (PEM, optional) ${c.private_key_set ? `<span class="muted">(stored; leave blank to keep)</span>` : ""}</label><textarea name="private_key" placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"></textarea></div>
      <div class="form-row"><div><label class="field">Key passphrase</label><input type="password" name="key_passphrase" autocomplete="new-password" placeholder="${c.key_passphrase_set ? "••••••••" : ""}"></div>
        <div><label class="field">Pinned host key</label><input type="text" name="host_key_fingerprint" value="${esc(c.host_key_fingerprint || "")}" placeholder="learned on first successful test"><div class="help">Trust on first use; clear to re-learn.</div></div></div>
    </div>
    <div class="mt">${switchHtml("enabled", r.enabled !== false, "Enabled")}</div>
    <div class="mt" data-result></div>`;
  return new Promise((resolve) => {
    const m = modal({ title: existing ? `Edit remote: ${existing.name}` : "Add remote", body, size: "wide", footer: `<button class="btn left" data-action="test">${icon("zap")} Test connection</button><button class="btn" data-close>Cancel</button><button class="btn btn-primary" data-action="save">${icon("check")} Save</button>`, onClose: () => resolve(false) });
    const payload = () => {
      const f = formValues(m.el);
      const cfg = { host: f.host, port: f.port, username: f.username, password: f.password, base_path: f.base_path, instance_dir: f.instance_dir, timeout: 30 };
      if (f.type === "ftp") Object.assign(cfg, { secure: f.tls !== "none", tls_mode: f.tls === "implicit" ? "implicit" : "explicit", passive: f.passive, nat_pasv_fix: f.nat_pasv_fix, verify_tls: f.verify_tls });
      else Object.assign(cfg, { private_key: f.private_key, key_passphrase: f.key_passphrase, host_key_fingerprint: f.host_key_fingerprint });
      return { name: f.name, type: f.type, enabled: f.enabled, config: cfg };
    };
    $("select[name=type]", m.el).addEventListener("change", (e) => { const t = e.target.value; $$("[data-pane]", m.el).forEach((p) => p.classList.toggle("hidden", p.dataset.pane !== t)); const port = $("input[name=port]", m.el); if (!existing || !port.value) port.value = t === "ftp" ? 21 : 22; });
    bind(m.el, {
      eye: (b) => { const i = $("input[name=password]", m.el); i.type = i.type === "password" ? "text" : "password"; b.innerHTML = icon(i.type === "password" ? "eye" : "eye-off", "sm"); },
      test: async (btn) => { const out = $("[data-result]", m.el); out.innerHTML = ""; await busy(btn, async () => { const r = await api.post("/api/remotes/test", { ...payload(), id: existing?.id }); out.innerHTML = `<div class="banner ${r.ok ? "success" : "danger"}">${icon(r.ok ? "circle-check" : "circle-x")}<div>${esc(r.message)}</div></div>`; }).catch((e) => { out.innerHTML = `<div class="banner danger">${icon("circle-x")}<div>${esc(e.message)}</div></div>`; }); },
      save: async (btn) => { await busy(btn, async () => { existing ? await api.put(`/api/remotes/${existing.id}`, payload()) : await api.post("/api/remotes", payload()); toast("Remote saved", "success"); resolve(true); m.close(); }).catch((e) => toast(e.message, "error")); },
    });
  });
}

export const remotes = {
  title: "Remotes",
  actions: () => `<button class="btn btn-primary" data-action="add">${icon("plus")} Add remote</button>`,
  async render(el) {
    const seq = (this._seq = (this._seq || 0) + 1);
    const [list, settings] = await Promise.all([loadRemotes(), api.get("/api/settings")]);
    const g = settings.general;
    if (seq !== this._seq) return;
    el.innerHTML = `
      <div class="card mb"><div class="card-h"><h2>Automatic sync</h2><div class="actions"><button class="btn btn-sm" data-action="syncAll">${icon("refresh")} Sync all now</button></div></div><div class="card-c">
        <div class="flex flex-wrap gap-4">${switchHtml("sync_enabled", g.sync_enabled, "Periodically upload every backup that is missing on a remote")}<div class="flex"><label class="field nowrap">every</label><input type="number" name="sync_interval_minutes" value="${g.sync_interval_minutes}" min="5" class="input" data-narrow> <span class="muted small">min</span></div><button class="btn btn-sm" data-action="saveSync">${icon("check")} Save</button></div>
        <div class="help">Backups are also uploaded right after they are created. Sync catches up after outages or when you add a remote later.</div>
        <div class="help">This instance is <b>${esc(settings.instance.name)}</b>${settings.instance.from_env ? " (from .env)" : ""}; remotes with "subfolder per instance" store under <code>&lt;base&gt;/${esc(settings.instance.name)}/&lt;volume&gt;</code>. Remotes can also be defined in <code>.env</code> (<code>DOCKBARE_REMOTE_1_*</code>). <a href="#settings/general">Change the name</a>.</div></div></div>
      ${list.length ? `<div class="grid cols-2">${list.map((r) => remoteCard(r)).join("")}</div>` : `<div class="card">${emptyState("No remotes yet. Add an FTP, FTPS or SFTP server to mirror backups off-site.", "server")}</div>`}`;
    $$("[data-narrow]", el).forEach((i) => (i.style.width = "90px"));
    bind(el, {
      add: () => remoteDialog(null, settings.instance.name).then((ch) => ch && this.render(el)),
      edit: (b) => remoteDialog(list.find((r) => r.id === b.dataset.id), settings.instance.name).then((ch) => ch && this.render(el)),
      del: async (b) => { const r = list.find((x) => x.id === b.dataset.id); if (!(await confirmDialog({ title: "Delete remote", text: `Delete <b>${esc(r.name)}</b>? Files on the server are not touched; upload status for this remote is forgotten.`, confirmLabel: "Delete", danger: true }))) return; try { await api.del(`/api/remotes/${r.id}`); toast("Remote deleted", "success"); this.render(el); } catch (e) { toast(e.message, "error"); } },
      test: async (b) => { await busy(b, async () => { const r = await api.post("/api/remotes/test", { id: b.dataset.id }); toast(r.message, r.ok ? "success" : "error", 7000); this.render(el); }).catch((e) => toast(e.message, "error")); },
      sync: async (b) => { try { const r = await api.post("/api/remotes/sync", { remotes: [b.dataset.id] }); taskLogModal(r.task.id); } catch (e) { toast(e.message, "error"); } },
      syncAll: async (b) => { try { const r = await api.post("/api/remotes/sync", {}); taskLogModal(r.task.id); } catch (e) { toast(e.message, "error"); } },
      toggle: async (sw) => { const r = list.find((x) => x.id === sw.dataset.id); try { await api.put(`/api/remotes/${r.id}`, { name: r.name, type: r.type, enabled: sw.checked, config: r.config }); toast(sw.checked ? "Remote enabled" : "Remote disabled", "success"); this.render(el); } catch (e) { toast(e.message, "error"); } },
      saveSync: async (b) => { const f = formValues(el); await busy(b, async () => { await api.put("/api/settings/general", { sync_enabled: f.sync_enabled, sync_interval_minutes: f.sync_interval_minutes }); toast("Sync settings saved", "success"); }).catch((e) => toast(e.message, "error")); },
    });
  },
  onAction: { add: (btn, ctx) => api.get("/api/settings").then((st) => remoteDialog(null, st.instance.name)).then((ch) => ch && ctx.refresh()) },
};

function remoteCard(r) {
  const h = r.health, c = r.config;
  const sec = r.type === "sftp" ? "SSH" : c.secure ? `FTPS ${c.tls_mode}` : "plain FTP";
  const env = r.source === "env";
  return `<div class="card"><div class="card-h"><h2 class="ellipsis">${esc(r.name)}</h2><span class="badge outline">${esc(r.type.toUpperCase())}</span>${env ? `<span class="badge info" title="Defined by DOCKBARE_REMOTE_* in .env">${icon("lock", "sm")} from .env</span>` : ""}<div class="actions"><label class="switch" title="${env ? "Set DOCKBARE_REMOTE_n_ENABLED in .env" : r.enabled ? "Disable" : "Enable"}"><input type="checkbox" data-action="toggle" data-id="${esc(r.id)}" ${r.enabled ? "checked" : ""} ${env ? "disabled" : ""}><span class="track"></span></label></div></div>
    <div class="card-c">
      <dl class="kv"><dt>Server</dt><dd class="mono">${esc(c.username ? c.username + "@" : "")}${esc(c.host)}:${esc(c.port)}</dd><dt>Layout</dt><dd class="mono">${esc(c.base_path)}/${r.instance ? `<b>${esc(r.instance)}</b>/` : ""}&lt;volume&gt;/${r.instance ? "" : ` <span class="muted small">(flat)</span>`}</dd><dt>Security</dt><dd>${esc(sec)}${r.type === "ftp" && !c.secure ? ` <span class="badge warn">${icon("unlock", "sm")} credentials in clear text</span>` : ""}${c.host_key_fingerprint ? `<div class="muted small mono">${esc(c.host_key_fingerprint)}</div>` : ""}</dd>
        <dt>Status</dt><dd>${!r.enabled ? `<span class="badge outline">disabled</span>` : !h ? `<span class="badge">${icon("loader", "spin sm")} checking</span>` : h.ok ? `<span class="badge ok">${icon("check", "sm")} reachable</span>` : `<span class="badge danger">${icon("x", "sm")} unreachable</span>`}${h ? ` <span class="muted small">${esc(ago(h.checked_at))}</span><div class="muted small">${esc(h.message || "")}</div>` : ""}</dd></dl>
      <div class="flex mt flex-wrap"><button class="btn btn-sm" data-action="test" data-id="${esc(r.id)}">${icon("zap")} Test</button><button class="btn btn-sm" data-action="sync" data-id="${esc(r.id)}">${icon("cloud-upload")} Sync</button><span class="grow"></span>${env ? `<span class="muted small">edit in .env</span>` : `<button class="btn btn-sm btn-ghost" data-action="edit" data-id="${esc(r.id)}">${icon("pencil")} Edit</button><button class="btn btn-sm btn-danger-ghost" data-action="del" data-id="${esc(r.id)}">${icon("trash")}</button>`}</div>
    </div></div>`;
}

// ---------------------------------------------------------------- recovery
export const recovery = {
  title: "Recovery",
  actions: () => "",
  async render(el) {
    const seq = (this._seq = (this._seq || 0) + 1);
    await loadRemotes();
    const scan = state.recoveryScan || (await api.get("/api/recovery/last").catch(() => null));
    state.recoveryScan = scan;
    const enabled = state.remotes.filter((r) => r.enabled);
    if (seq !== this._seq) return;
    el.innerHTML = `
      <div class="banner info">${icon("life-buoy")}<div><b>Disaster recovery.</b> Bring up your stack on the new host first (<code>docker compose up -d</code>) so the volumes exist with the same names, then scan a remote: Dockbare lists every backup it finds, matches them to local volumes and restores the ones you pick (download → verify → stop containers → restore → restart). Volumes that do not exist yet can be created on the fly.</div></div>
      <div class="card mb"><div class="card-c"><div class="flex flex-wrap">
        <div class="grow"><label class="field">Remote to scan</label><select name="remote" ${enabled.length ? "" : "disabled"}>${enabled.map((r) => `<option value="${esc(r.id)}" ${scan?.remote?.id === r.id ? "selected" : ""}>${esc(r.name)} (${esc(r.type.toUpperCase())} · ${esc(r.config.host)})</option>`).join("") || "<option>No enabled remotes</option>"}</select></div>
        <div><label class="field">&nbsp;</label><button class="btn btn-primary" data-action="scan" ${enabled.length ? "" : "disabled"}>${icon("search")} Scan remote</button></div></div>
        ${enabled.length ? "" : `<div class="help">Add the remote that holds your backups under <a href="#remotes">Remotes</a> first. Use the same encryption key (<code>DOCKBARE_ENCRYPTION_KEY</code>) as on the old host for encrypted backups.</div>`}</div></div>
      <div data-results>${scan ? renderScan(scan) : ""}</div>`;
    $$("[data-narrow]", el).forEach((i) => (i.style.width = "190px"));
    const handlers = {
      scan: async (b) => {
        const rid = $("select[name=remote]", el).value;
        await busy(b, async () => {
          const r = await api.post("/api/recovery/scan", { remote: rid });
          const out = $("[data-results]", el);
          out.innerHTML = `<div class="card"><div class="card-c"><div class="flex">${icon("loader", "spin")} Scanning… this reads the directory listing and the latest metadata of every volume.</div></div></div>`;
          await waitTask(r.task.id);
          state.recoveryScan = await api.get("/api/recovery/last");
          out.innerHTML = renderScan(state.recoveryScan);
          $$("[data-narrow]", out).forEach((i) => (i.style.width = "190px"));
          bind(out, handlers);
        }).catch((e) => toast(e.message, "error"));
      },
      importCfg: async (b) => {
        const inst = b.dataset.inst || null;
        const ok = await confirmDialog({ title: "Import configuration", confirmLabel: "Import", text: `Import the configuration snapshot of <b>${esc(inst || "flat layout")}</b> into this instance? Existing entries with the same name/id are overwritten; anything defined in <code>.env</code> is left alone.`,
          extra: `<div class="stack gap-2 mt">${["schedules", "remotes", "notifications", "reports", "peers", "monitors", "uptime", "general"].map((k) => `<label class="check"><input type="checkbox" name="sections" data-multi="1" value="${k}" checked> ${k}</label>`).join("")}</div>` });
        if (!ok) return;
        await busy(b, async () => { const r = await api.post("/api/recovery/config/import", { remote: state.recoveryScan.remote.id, instance: inst, sections: ok.sections || [] }); toast(`Imported: ${Object.entries(r.summary).map(([k, v]) => `${k} ${v === true ? "✓" : v}`).join(", ")}`, "success", 9000); }).catch((e) => toast(e.message, "error"));
      },
      selAll: () => { $$("[data-results] tr[data-vol]:not(.hidden) input[name=sel]", el).forEach((i) => (i.checked = !i.disabled)); },
      instFilter: (sel) => { const v = sel.value; $$("[data-results] tr[data-vol]", el).forEach((tr) => { const show = v === "*" || (tr.dataset.inst || "") === v; tr.classList.toggle("hidden", !show); if (!show) $("input[name=sel]", tr).checked = false; }); },
      selNone: () => { $$("[data-results] input[name=sel]", el).forEach((i) => (i.checked = false)); },
      restore: async (b) => {
        const rows = $$("[data-results] tr[data-vol]", el).filter((tr) => $("input[name=sel]", tr).checked);
        if (!rows.length) return toast("Select at least one volume", "warning");
        const verify = $("input[name=verify]", el).checked;
        const downloadOnly = $("input[name=download_only]", el).checked;
        const items = rows.map((tr) => ({ volume: tr.dataset.vol, instance: tr.dataset.inst || null, filename: $("select[name=file]", tr).value, target_volume: $("input[name=target]", tr).value.trim() || tr.dataset.vol, create_volume: true, restore: !downloadOnly }));
        const ok = await confirmDialog({ title: downloadOnly ? "Download backups" : "Restore volumes", danger: !downloadOnly, confirmLabel: downloadOnly ? "Download" : "Restore now",
          text: downloadOnly ? `Download ${items.length} backup(s) into the local backup store without touching any volume.` : `<b>This wipes the target volumes</b> and replaces their content with the selected backups. Containers using them are stopped and restarted.`,
          extra: `<div class="log mt">${items.map((i) => esc(`${i.instance ? i.instance + "/" : ""}${i.volume}/${i.filename} → ${i.target_volume}`)).join("\n")}</div>` });
        if (!ok) return;
        await busy(b, async () => { const r = await api.post("/api/recovery/restore", { remote: state.recoveryScan.remote.id, items, verify }); taskLogModal(r.task.id); }).catch((e) => toast(e.message, "error"));
      },
    };
    bind(el, handlers);
  },
};

function renderScan(s) {
  if (!s) return "";
  const items = s.items || [];
  const keyBadge = (k) => ({ match: `<span class="badge ok">${icon("key", "sm")} key matches</span>`, mismatch: `<span class="badge danger">${icon("key", "sm")} wrong key</span>`, missing: `<span class="badge danger">${icon("key", "sm")} no key configured</span>`, unknown: `<span class="badge warn">${icon("key", "sm")} key unknown</span>` }[k] || "");
  const insts = s.instances || [];
  const multi = insts.length > 0;
  const defaultInst = insts.includes(s.own_instance) ? s.own_instance : "*";
  const instLabel = (it) => it.instance ? `<span class="badge ${it.own_instance ? "info" : "outline"}" title="instance folder">${icon("hard-drive", "sm")} ${esc(it.instance)}${it.own_instance ? " · this instance" : ""}</span>` : `<span class="badge outline" title="flat layout (no instance folder)">${icon("folder", "sm")} flat</span>`;
  const cfgs = (s.configs || []).filter((c) => !c.error);
  const cfgCard = cfgs.length ? `<div class="card mb"><div class="card-h"><h2>${icon("settings", "sm")} Configuration snapshots</h2><span class="muted small">schedules, remotes, notifications, peers, monitors</span></div><div class="card-c tight"><div class="table-wrap"><table><thead><tr><th>Instance</th><th>Exported</th><th>Contents</th><th></th></tr></thead><tbody>
    ${cfgs.map((c) => `<tr><td><b>${esc(c.instance || "flat layout")}</b>${c.instance === s.own_instance ? ` <span class="badge info">this instance</span>` : ""}<div class="muted small">${esc(c.hostname || "")} · Dockbare ${esc(c.app_version || "?")}</div></td><td>${esc(fmtTime(c.exported_at))}<div class="muted small">${esc(ago(c.exported_at))}</div></td><td class="small">${c.counts.schedules} schedules · ${c.counts.remotes} remotes · ${c.counts.peers} peers · ${c.counts.monitors} monitors${c.channels.length ? ` · ${esc(c.channels.join(", "))}` : ""}</td><td><div class="actions"><button class="btn btn-sm btn-primary" data-action="importCfg" data-inst="${esc(c.instance || "")}">${icon("download")} Import</button></div></td></tr>`).join("")}</tbody></table></div></div></div>` : "";
  return `${cfgCard}<div class="card"><div class="card-h"><h2>${esc(s.remote.name)}</h2><span class="muted small">scanned ${esc(ago(s.scanned_at))} · ${items.length} volume(s)${multi ? ` · ${insts.length} instance(s)` : ""}</span><div class="actions">${multi ? `<select data-action="instFilter" data-narrow><option value="*">All instances</option>${insts.map((i) => `<option value="${esc(i)}" ${i === defaultInst ? "selected" : ""}>${esc(i)}${i === s.own_instance ? " (this instance)" : ""}</option>`).join("")}${s.flat_layout ? `<option value="">flat layout</option>` : ""}</select>` : ""}<a href="#" class="small" data-action="selAll">Select all</a> · <a href="#" class="small" data-action="selNone">None</a></div></div>
    ${items.length ? `<div class="table-wrap"><table><thead><tr><th></th><th>Volume on remote</th><th>Backup to restore</th><th>Local volume</th><th>Target</th></tr></thead><tbody>
      ${items.map((it) => { const lv = it.local_volume; const bad = it.key_status === "mismatch" || it.key_status === "missing"; const hidden = defaultInst !== "*" && (it.instance || "") !== defaultInst; return `<tr data-vol="${esc(it.volume)}" data-inst="${esc(it.instance || "")}" class="${hidden ? "hidden" : ""}">
        <td><input type="checkbox" name="sel" ${bad ? "disabled" : ""} ${lv.exists && !bad && !hidden && (it.own_instance || !it.instance) ? "checked" : ""}></td>
        <td><div class="flex">${icon("box", "sm")} <b>${esc(it.volume)}</b></div><div class="pills mt">${instLabel(it)}</div><div class="muted small">${it.backups.length} backup(s)${it.meta?.hostname ? ` · from ${esc(it.meta.hostname)}` : ""}${it.meta?.containers?.length ? ` · ${esc(it.meta.containers.map((c) => c.name).join(", "))}` : ""}</div></td>
        <td><select name="file">${it.backups.map((b, i) => `<option value="${esc(b.filename)}" ${i === 0 ? "selected" : ""}>${esc(fmtTime(b.created_at))} · ${esc(fmtBytes(b.size))}${b.encrypted ? " · encrypted" : ""}${b.local ? " · local copy" : ""}</option>`).join("")}</select><div class="pills mt">${it.latest.encrypted ? keyBadge(it.key_status) : `<span class="badge warn">${icon("unlock", "sm")} plain</span>`}${it.latest.has_meta ? "" : `<span class="badge outline">no metadata</span>`}</div></td>
        <td>${lv.exists ? `<span class="badge ok">${icon("check", "sm")} exists</span>${lv.containers.length ? `<div class="small mt">${lv.containers.map((c) => `<span class="ctr"><span class="dot ${c.status === "running" ? "ok" : "off"}"></span>${esc(c.name)}</span>`).join(" ")}</div>` : `<div class="muted small">no container uses it</div>`}` : `<span class="badge warn">${icon("alert", "sm")} missing</span><div class="muted small">will be created</div>`}</td>
        <td><input type="text" name="target" value="${esc(it.volume)}" title="Restore into a differently named volume"></td></tr>`; }).join("")}</tbody></table></div>
    <div class="card-c flex flex-wrap"><label class="check"><input type="checkbox" name="verify" checked> Verify archives before restoring</label><label class="check"><input type="checkbox" name="download_only"> Download only (do not restore)</label><span class="grow"></span><button class="btn btn-danger" data-action="restore">${icon("restore")} Restore selected</button></div>` : emptyState("No backups found under the base path of this remote.", "folder")}</div>`;
}

async function waitTask(id) {
  for (;;) {
    const t = await api.get(`/api/tasks/${id}`);
    if (["success", "failure", "cancelled"].includes(t.status)) { if (t.status !== "success") throw new Error(t.error || "Task failed"); return t; }
    await new Promise((r) => setTimeout(r, 1000));
  }
}

// ----------------------------------------------------------------- reports
export const reports = {
  title: "Reports",
  actions: () => "",
  async render(el) {
    const seq = (this._seq = (this._seq || 0) + 1);
    const [data, settings] = await Promise.all([api.get("/api/reports"), api.get("/api/settings")]);
    const s = data.settings, n = data.next_runs, ns = settings.notifications;
    const days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
    const chan = (k, label, on) => `<label class="check"><input type="checkbox" name="channels" data-multi="1" value="${k}" ${s.channels.includes(k) ? "checked" : ""}> ${label} ${on ? "" : `<span class="badge warn">not enabled</span>`}</label>`;
    if (seq !== this._seq) return;
    el.innerHTML = `
      <div class="grid cols-2 mb">
        <div class="card"><div class="card-h"><h2>Schedule</h2></div><div class="card-c">
          <div class="form-group"><div class="flex flex-wrap">${switchHtml("daily_enabled", s.daily.enabled, "Daily")}<input type="time" name="daily_time" value="${esc(s.daily.time)}" data-narrow>${n.daily ? `<span class="muted small">next ${esc(ago(n.daily))}</span>` : ""}</div></div>
          <div class="form-group"><div class="flex flex-wrap">${switchHtml("weekly_enabled", s.weekly.enabled, "Weekly")}<select name="weekly_weekday" data-narrow>${days.map((d, i) => `<option value="${i}" ${s.weekly.weekday === i ? "selected" : ""}>${d}</option>`).join("")}</select><input type="time" name="weekly_time" value="${esc(s.weekly.time)}" data-narrow>${n.weekly ? `<span class="muted small">next ${esc(ago(n.weekly))}</span>` : ""}</div></div>
          <div class="form-group"><div class="flex flex-wrap">${switchHtml("monthly_enabled", s.monthly.enabled, "Monthly")}<span class="muted small">day</span><input type="number" name="monthly_day" min="1" max="28" value="${s.monthly.day}" data-narrow><input type="time" name="monthly_time" value="${esc(s.monthly.time)}" data-narrow>${n.monthly ? `<span class="muted small">next ${esc(ago(n.monthly))}</span>` : ""}</div></div>
          <div class="help">Times are in ${esc(settings.env.timezone)}. Set <code>TZ</code> or <code>DOCKBARE_TIMEZONE</code> to change.</div>
          <hr><div class="form-group"><label class="field">Send via</label><div class="stack gap-2">${chan("email", "Email (SMTP)", ns.email.enabled)}${chan("ntfy", "ntfy", ns.ntfy.enabled)}${chan("webhook", "Webhook", ns.webhook.enabled)}</div><div class="help">Configure channels under <a href="#settings/notifications">Settings → Notifications</a>. Email gets a formatted HTML report, ntfy and webhooks get the text version.</div></div>
          <div class="form-group">${switchHtml("only_on_problems", s.only_on_problems, "Only send scheduled reports when there were problems")}</div>
          <button class="btn btn-primary" data-action="save">${icon("check")} Save</button></div></div>
        <div class="card"><div class="card-h"><h2>Preview</h2><div class="actions"><select name="period" data-narrow><option value="daily">Last 24 hours</option><option value="weekly">Last 7 days</option><option value="monthly">Last 30 days</option></select><button class="btn btn-sm" data-action="preview">${icon("eye")} Preview</button><button class="btn btn-sm btn-primary" data-action="send">${icon("send")} Send now</button></div></div>
          <div class="card-c"><div data-preview class="muted small">Pick a period and click Preview.</div></div></div>
      </div>
      <div class="card"><div class="card-h"><h2>Sent reports</h2></div><div class="card-c tight">${data.history.length ? `<div class="table-wrap"><table><thead><tr><th>Generated</th><th>Period</th><th>Subject</th><th>Channels</th><th>Status</th><th></th></tr></thead><tbody>
        ${data.history.map((r) => `<tr><td class="nowrap">${esc(fmtTime(r.generated_at))}</td><td>${esc(r.period)}</td><td>${esc(r.subject)}</td><td class="small">${esc(r.channels.join(", "))}</td><td><span class="badge ${r.status === "sent" ? "ok" : r.status.startsWith("skipped") ? "outline" : "warn"}">${esc(r.status)}</span></td><td><div class="actions"><button class="btn btn-sm btn-ghost" data-action="view" data-id="${r.id}">${icon("eye")} View</button></div></td></tr>`).join("")}</tbody></table></div>` : emptyState("No reports sent yet.", "chart")}</div></div>`;
    $$("[data-narrow]", el).forEach((i) => (i.style.width = i.tagName === "SELECT" ? "150px" : "110px"));
    bind(el, {
      save: async (b) => { const f = formValues(el); await busy(b, async () => { await api.put("/api/reports/settings", { daily: { enabled: f.daily_enabled, time: f.daily_time }, weekly: { enabled: f.weekly_enabled, weekday: Number(f.weekly_weekday), time: f.weekly_time }, monthly: { enabled: f.monthly_enabled, day: f.monthly_day, time: f.monthly_time }, channels: f.channels || [], only_on_problems: f.only_on_problems }); toast("Report settings saved", "success"); this.render(el); }).catch((e) => toast(e.message, "error")); },
      preview: async (b) => { const p = $("select[name=period]", el).value; await busy(b, async () => { const r = await api.get(`/api/reports/preview?period=${p}`); $("[data-preview]", el).innerHTML = `<div class="mb"><b>${esc(r.subject)}</b></div><div class="log">${esc(r.text)}</div>`; }).catch((e) => toast(e.message, "error")); },
      send: async (b) => { const p = $("select[name=period]", el).value; await busy(b, async () => { const r = await api.post("/api/reports/send", { period: p }); toast(`Report: ${r.status}`, r.status === "sent" ? "success" : "warning", 7000); this.render(el); }).catch((e) => toast(e.message, "error")); },
      view: async (b) => { const r = await api.get(`/api/reports/${b.dataset.id}`); modal({ title: r.subject, size: "wide", body: `<div class="muted small mb">${esc(fmtTime(r.generated_at))} · ${esc(r.period)} · ${esc(r.status)}</div><div class="log">${esc(r.text)}</div>`, footer: `<button class="btn" data-close>Close</button>` }); },
    });
  },
};

// ---------------------------------------------------------------- activity
export const activity = {
  title: "Activity",
  actions: () => `<a class="btn" href="/api/logs/download">${icon("download")} Raw log</a>`,
  filters: { level: "", kind: "", volume: "", q: "" },
  async render(el) {
    const seq = (this._seq = (this._seq || 0) + 1);
    const f = this.filters;
    const qs = Object.entries({ ...f, limit: 100 }).filter(([, v]) => v !== "").map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join("&");
    const events = await api.get(`/api/events?${qs}`);
    if (seq !== this._seq) return;
    el.innerHTML = `
      <div class="card"><div class="card-c"><div class="flex flex-wrap">
        <div class="search grow">${icon("search", "sm")}<input type="search" placeholder="Search messages…" value="${esc(f.q)}" data-action="q" data-on="input"></div>
        <select data-action="level" data-narrow><option value="">All levels</option>${["info", "success", "warning", "error"].map((l) => `<option value="${l}" ${f.level === l ? "selected" : ""}>${l}</option>`).join("")}</select>
        <select data-action="kind" data-narrow><option value="">All kinds</option>${["backup", "upload", "restore", "prune", "sync", "recovery", "remote", "schedule", "report", "auth", "settings", "app"].map((k) => `<option value="${k}" ${f.kind === k ? "selected" : ""}>${k}</option>`).join("")}</select>
        <input type="text" placeholder="Volume" value="${esc(f.volume)}" data-action="volume" data-on="input" data-narrow>
      </div></div>
      <div class="card-c tight">${events.length ? `<div class="table-wrap"><table><thead><tr><th>Time</th><th>Level</th><th>Event</th><th>Volume</th><th>Message</th></tr></thead><tbody>
        ${events.map((e) => `<tr><td class="nowrap" title="${esc(fmtTime(e.ts))}">${esc(fmtTime(e.ts))}</td><td>${levelBadge(e.level)}</td><td class="mono small">${esc(e.kind)}</td><td class="small">${esc(e.volume || "")}</td><td>${esc(e.message)}${e.backup ? `<div class="muted small mono">${esc(e.backup)}</div>` : ""}</td></tr>`).join("")}</tbody></table></div>
        ${events.length >= 100 ? `<div class="card-c center"><button class="btn" data-action="more" data-before="${events[events.length - 1].id}">Load older</button></div>` : ""}` : emptyState("No events match.", "activity")}</div></div>`;
    $$("[data-narrow]", el).forEach((i) => (i.style.width = "150px"));
    const rerender = () => { clearTimeout(this._t); this._t = setTimeout(() => this.render(el), 250); };
    bind(el, {
      q: (i) => { f.q = i.value; rerender(); }, level: (i) => { f.level = i.value; rerender(); }, kind: (i) => { f.kind = i.value; rerender(); }, volume: (i) => { f.volume = i.value; rerender(); },
      more: async (b) => { const older = await api.get(`/api/events?${qs}&before=${b.dataset.before}`); const tbody = $("tbody", el); older.forEach((e) => tbody.insertAdjacentHTML("beforeend", `<tr><td class="nowrap">${esc(fmtTime(e.ts))}</td><td>${levelBadge(e.level)}</td><td class="mono small">${esc(e.kind)}</td><td class="small">${esc(e.volume || "")}</td><td>${esc(e.message)}</td></tr>`)); if (older.length < 100) b.remove(); else b.dataset.before = older[older.length - 1].id; },
    });
  },
};

// ---------------------------------------------------------------- settings
export const settings = {
  title: "Settings",
  actions: () => "",
  tab: "general",
  async render(el, sub) {
    const seq = (this._seq = (this._seq || 0) + 1);
    if (sub) this.tab = sub;
    const s = await api.get("/api/settings");
    const tabs = [["general", "General"], ["notifications", "Notifications"], ["config", "Config backup"], ["security", "Security"], ["environment", "Environment"]];
    if (seq !== this._seq) return;
    el.innerHTML = `<div class="tabs">${tabs.map(([k, l]) => `<button class="${this.tab === k ? "active" : ""}" data-action="tab" data-tab="${k}">${l}</button>`).join("")}</div><div data-tab-content></div>`;
    const c = $("[data-tab-content]", el);
    bind(el, { tab: (b) => { this.tab = b.dataset.tab; location.hash = `#settings/${this.tab}`; } });
    if (this.tab === "general") renderGeneral(c, s, () => this.render(el));
    else if (this.tab === "notifications") renderNotifications(c, s);
    else if (this.tab === "config") await renderConfig(c, s);
    else if (this.tab === "security") await renderSecurity(c, s);
    else renderEnvironment(c, s);
  },
};

function renderGeneral(c, s, rerender) {
  const g = s.general;
  const tzs = ["", "UTC", "Europe/Berlin", "Europe/London", "Europe/Paris", "Europe/Madrid", "Europe/Zurich", "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles", "America/Sao_Paulo", "Asia/Tokyo", "Asia/Singapore", "Asia/Kolkata", "Australia/Sydney"];
  const curTz = localStorage.getItem("dockbare.tz") || "";
  c.innerHTML = `<div class="grid cols-2">
    <div class="card"><div class="card-h"><h2>Backups</h2></div><div class="card-c">
      <div class="form-group">${switchHtml("encrypt_default", g.encrypt_default, "Encrypt new backups by default")}${s.encryption.configured ? `<div class="help">Key fingerprint <code>${esc(s.encryption.fingerprint)}</code>. Keep the key safe: without it encrypted backups cannot be restored.</div>` : `<div class="help">No key configured. Set <code>DOCKBARE_ENCRYPTION_KEY</code> in <code>.env</code> and restart.</div>`}</div>
      <div class="form-group">${switchHtml("stop_containers_default", g.stop_containers_default, "Stop containers during backup by default")}<div class="help">Databases and apps with open files need this for consistent snapshots. Can be overridden per schedule.</div></div>
      <div class="form-row"><div><label class="field">Compression level (1 fast – 9 small)</label><input type="number" name="compression_level" min="1" max="9" value="${g.compression_level}"></div><div><label class="field">Parallel backups</label><input type="number" name="max_parallel_backups" min="1" max="8" value="${g.max_parallel_backups}"></div></div>
      <div class="form-row mt"><div><label class="field">Keep activity events (days)</label><input type="number" name="events_keep_days" min="7" value="${g.events_keep_days}"></div><div><label class="field">Remote health check (minutes)</label><input type="number" name="remote_health_interval_minutes" min="1" value="${g.remote_health_interval_minutes}"></div></div>
      <div class="mt"><button class="btn btn-primary" data-action="save">${icon("check")} Save</button></div></div></div>
    <div class="stack">
      <div class="card"><div class="card-h"><h2>Display</h2></div><div class="card-c">
        <div class="form-group"><label class="field">Time zone for dates in this browser</label><select name="tz">${tzs.map((t) => `<option value="${t}" ${t === curTz ? "selected" : ""}>${t || "Browser default"}</option>`).join("")}</select><div class="help">Only affects how times are shown here. Schedules run in the server time zone (${esc(s.env.timezone)}).</div></div></div></div>
      <div class="card"><div class="card-h"><h2>Instance</h2></div><div class="card-c">
        <div class="form-group"><label class="field">Instance name</label><div class="flex"><input type="text" name="instance_name" value="${esc(s.instance.name)}" ${s.instance.from_env ? "readonly disabled" : ""}>${s.instance.from_env ? `<span class="badge info nowrap">${icon("lock", "sm")} from .env</span>` : `<button class="btn" data-action="saveInstance">${icon("check")} Save</button>`}</div>
        <div class="help">Identifies this installation on shared remotes (<code>&lt;base&gt;/${esc(s.instance.name)}/&lt;volume&gt;</code>) and in peer monitoring. Set <code>DOCKBARE_INSTANCE_NAME</code> in <code>.env</code> to make it survive a reinstall. Renaming moves future uploads to a new folder; existing ones stay where they are.</div></div></div></div>
      <div class="card"><div class="card-h"><h2>Docker</h2></div><div class="card-c"><dl class="kv"><dt>Engine</dt><dd>${s.docker.ok ? `${esc(s.docker.version)} (API ${esc(s.docker.api)})` : `<span class="badge danger">unreachable</span> ${esc(s.docker.error || "")}`}</dd><dt>Helper image</dt><dd class="mono">${esc(s.helper_image || s.env.helper_image || "auto (own image)")}</dd><dt>Backup dir</dt><dd class="mono">${esc(s.env.backup_dir)}</dd><dt>Data dir</dt><dd class="mono">${esc(s.env.data_dir)}</dd></dl></div></div>
    </div></div>`;
  bind(c, {
    save: async (b) => { const f = formValues(c); delete f.instance_name; delete f.tz; await busy(b, async () => { await api.put("/api/settings/general", f); toast("Settings saved", "success"); }).catch((e) => toast(e.message, "error")); },
    saveInstance: async (b) => { const name = $("input[name=instance_name]", c).value; await busy(b, async () => { await api.put("/api/settings/general", { instance_name: name }); toast("Instance name saved", "success"); rerender(); }).catch((e) => toast(e.message, "error")); },
  });
  $("select[name=tz]", c).addEventListener("change", (e) => { e.target.value ? localStorage.setItem("dockbare.tz", e.target.value) : localStorage.removeItem("dockbare.tz"); toast("Display time zone updated", "success"); });
}

function renderNotifications(c, s) {
  const n = s.notifications, ev = s.notify_events, locked = n.env_locked || {};
  const isLocked = (name) => { const [sec, key] = name.split("."); return (locked[sec] || []).includes(key); };
  const lock = (name) => (isLocked(name) ? " readonly disabled" : "");
  const secret = (name, set) => `<input type="password" name="${name}" autocomplete="new-password" placeholder="${isLocked(name) ? "set via .env" : set ? "•••••••• (stored, blank keeps it)" : ""}"${lock(name)}>`;
  const envBadge = (sec) => ((locked[sec] || []).length ? `<span class="badge info" title="${esc((locked[sec] || []).join(", "))} come from .env">${icon("lock", "sm")} from .env</span>` : "");
  c.innerHTML = `<div class="grid cols-2">
    <div class="stack">
      <div class="card"><div class="card-h"><h2>${icon("mail")} Email (SMTP)</h2>${envBadge("email")}<div class="actions">${switchHtml("email.enabled", n.email.enabled, "", lock("email.enabled"))}</div></div><div class="card-c">
        <div class="form-row"><div><label class="field">SMTP host</label><input type="text" name="email.host" value="${esc(n.email.host)}" placeholder="smtp.example.com"${lock("email.host")}></div><div><label class="field">Port</label><input type="number" name="email.port" value="${esc(n.email.port)}"${lock("email.port")}></div></div>
        <div class="form-row mt"><div><label class="field">Security</label><select name="email.security"${lock("email.security")}><option value="starttls" ${n.email.security === "starttls" ? "selected" : ""}>STARTTLS (587)</option><option value="ssl" ${n.email.security === "ssl" ? "selected" : ""}>SSL/TLS (465)</option><option value="none" ${n.email.security === "none" ? "selected" : ""}>None</option></select></div><div><label class="field">Username</label><input type="text" name="email.username" value="${esc(n.email.username)}" autocomplete="off"${lock("email.username")}></div></div>
        <div class="form-row mt"><div><label class="field">Password</label>${secret("email.password", n.email.password_set)}</div><div><label class="field">From</label><input type="email" name="email.from_addr" value="${esc(n.email.from_addr)}" placeholder="dockbare@example.com"${lock("email.from_addr")}></div></div>
        <div class="form-group mt"><label class="field">Recipients (comma separated)</label><input type="text" name="email.to_addrs" value="${esc(n.email.to_addrs)}" placeholder="you@example.com"${lock("email.to_addrs")}></div>
        <div class="flex"><button class="btn btn-sm" data-action="test" data-ch="email">${icon("send")} Send test email</button></div></div></div>
      <div class="card"><div class="card-h"><h2>${icon("bell")} ntfy</h2>${envBadge("ntfy")}<div class="actions">${switchHtml("ntfy.enabled", n.ntfy.enabled, "", lock("ntfy.enabled"))}</div></div><div class="card-c">
        <div class="form-row"><div><label class="field">Server</label><input type="url" name="ntfy.base_url" value="${esc(n.ntfy.base_url)}"${lock("ntfy.base_url")}></div><div><label class="field">Topic</label><input type="text" name="ntfy.topic" value="${esc(n.ntfy.topic)}" placeholder="my-backups"${lock("ntfy.topic")}></div></div>
        <div class="form-group mt"><label class="field">Access token (optional)</label>${secret("ntfy.token", n.ntfy.token_set)}</div>
        <div class="flex"><button class="btn btn-sm" data-action="test" data-ch="ntfy">${icon("send")} Send test</button></div></div></div>
      <div class="card"><div class="card-h"><h2>${icon("globe")} Webhook</h2>${envBadge("webhook")}<div class="actions">${switchHtml("webhook.enabled", n.webhook.enabled, "", lock("webhook.enabled"))}</div></div><div class="card-c">
        <div class="form-group"><label class="field">URL</label><input type="url" name="webhook.url" value="${esc(n.webhook.url)}" placeholder="https://hooks.example.com/…"${lock("webhook.url")}></div>
        <div class="form-group"><label class="field">Signing secret (optional, HMAC-SHA256 in X-Dockbare-Signature)</label>${secret("webhook.secret", n.webhook.secret_set)}</div>
        <div class="flex"><button class="btn btn-sm" data-action="test" data-ch="webhook">${icon("send")} Send test</button></div></div></div>
    </div>
    <div class="stack">
      <div class="card"><div class="card-h"><h2>Events</h2></div><div class="card-c"><div class="stack gap-2">${Object.entries(ev).map(([k, v]) => switchHtml(`events.${k}`, n.events[k] ?? v.default, v.label)).join("")}</div><div class="help">Reports are configured under <a href="#reports">Reports</a>. Channel settings given in <code>.env</code> (DOCKBARE_SMTP_*, DOCKBARE_NTFY_*, DOCKBARE_WEBHOOK_*) override what is saved here.</div></div></div>
      <div><button class="btn btn-primary" data-action="save">${icon("check")} Save notification settings</button></div>
    </div></div>`;
  const collect = () => { const out = { ntfy: {}, email: {}, webhook: {}, events: {} }; Object.entries(formValues(c)).forEach(([k, v]) => { const [sec, key] = k.split("."); if (out[sec]) out[sec][key] = v; }); return out; };
  bind(c, {
    save: async (b) => { await busy(b, async () => { await api.put("/api/settings/notifications", collect()); toast("Notification settings saved", "success"); }).catch((e) => toast(e.message, "error")); },
    test: async (b) => { const ch = b.dataset.ch; const o = collect()[ch]; await busy(b, async () => { const r = await api.post("/api/settings/notifications/test", { channel: ch, override: o }); toast(r.ok ? "Test notification sent" : r.message, r.ok ? "success" : "error", 8000); }).catch((e) => toast(e.message, "error")); },
  });
}

async function renderConfig(c, s) {
  const st = await api.get("/api/config/status");
  const lp = st.last_push;
  const rname = (id) => (state.remotes.find((r) => r.id === id) || {}).name || id;
  await loadRemotes();
  c.innerHTML = `<div class="grid cols-2">
    <div class="card"><div class="card-h"><h2>Configuration snapshot</h2></div><div class="card-c">
      <p class="small">Everything that is not in <code>.env</code> (schedules, remotes, notification channels, peers, container monitors, report and uptime settings) is bundled into one file${st.encrypted ? ", encrypted with your backup key," : ""} and pushed to every remote as <code>_dockbare/config.json.enc</code> whenever you change something and once a day. Recovery can import it with one click.</p>
      ${st.encrypted ? "" : `<div class="banner warn">${icon("unlock")}<div>No encryption key configured: snapshots are <b>not pushed</b> because they contain remote passwords. Set <code>DOCKBARE_ENCRYPTION_KEY</code>.</div></div>`}
      <div class="form-group">${switchHtml("config_autopush", st.autopush, "Push automatically after changes and daily")}</div>
      <div class="flex flex-wrap"><button class="btn btn-primary" data-action="push" ${st.encrypted ? "" : "disabled"}>${icon("cloud-upload")} Push now</button><button class="btn" data-action="saveAuto">${icon("check")} Save</button></div>
      <div class="help mt">${lp ? `Last push ${esc(ago(lp.at))}${lp.error ? ` · ${esc(lp.error)}` : ""}${lp.results ? ": " + Object.entries(lp.results).map(([k, v]) => `${esc(rname(k))} ${v === "ok" ? "✓" : "✗ " + esc(v)}`).join(", ") : ""}` : "Not pushed yet in this session."}</div>
      <dl class="kv mt"><dt>Contents</dt><dd>${st.snapshot.counts.schedules} schedules · ${st.snapshot.counts.remotes} remotes · ${st.snapshot.counts.peers} peers · ${st.snapshot.counts.monitors} monitors${st.snapshot.channels.length ? ` · channels: ${esc(st.snapshot.channels.join(", "))}` : ""}</dd></dl></div></div>
    <div class="stack">
      <div class="card"><div class="card-h"><h2>Export</h2></div><div class="card-c"><p class="small">Download the snapshot to keep it with your <code>.env</code>.</p><div class="flex flex-wrap"><a class="btn btn-primary" href="/api/config/export" ${st.encrypted ? "" : 'class="btn btn-primary disabled"'}>${icon("lock")} Encrypted (.json.enc)</a><a class="btn" href="/api/config/export?plain=1" data-action="plainWarn">${icon("unlock")} Plain JSON</a></div><div class="help">The plain export contains remote passwords and tokens in clear text.</div></div></div>
      <div class="card"><div class="card-h"><h2>Import</h2></div><div class="card-c"><p class="small">Import a snapshot file (encrypted or plain). Existing entries with the same name/id are overwritten; anything from <code>.env</code> is left alone.</p>${fileInput("file", ".enc,.json,application/json")}<div class="flex mt"><button class="btn btn-primary" data-action="import">${icon("upload")} Import</button></div></div></div>
    </div></div>`;
  bind(c, {
    saveAuto: async (b) => { await busy(b, async () => { await api.put("/api/settings/general", { config_autopush: formValues(c).config_autopush }); toast("Saved", "success"); }).catch((e) => toast(e.message, "error")); },
    push: async (b) => { await busy(b, async () => { const r = await api.post("/api/config/push", {}); const bad = Object.values(r.results || {}).filter((v) => v !== "ok").length; toast(bad ? `Pushed with ${bad} failure(s)` : `Pushed to ${Object.keys(r.results || {}).length} remote(s)`, bad ? "warning" : "success"); renderConfig(c, s); }).catch((e) => toast(e.message, "error")); },
    import: async (b) => { const f = $("input[type=file]", c).files[0]; if (!f) return toast("Choose a file", "warning"); const fd = new FormData(); fd.append("file", f); await busy(b, async () => { const r = await api.post("/api/config/import", fd); toast(`Imported snapshot of ${r.snapshot.instance} (${Object.entries(r.summary).map(([k, v]) => `${k} ${v === true ? "✓" : v}`).join(", ")})`, "success", 9000); renderConfig(c, s); }).catch((e) => toast(e.message, "error")); },
    plainWarn: (a, e) => { if (!confirm("The plain export contains passwords and tokens in clear text. Continue?")) e.preventDefault(); else location.href = a.href; },
  });
}

async function renderSecurity(c, s) {
  const sec = await api.get("/api/security");
  const st = sec.settings;
  c.innerHTML = `<div class="grid cols-2">
    <div class="stack">
      <div class="card"><div class="card-h"><h2>${icon("shield-check")} Protection</h2></div><div class="card-c"><dl class="kv">
        <dt>Admin user</dt><dd><code>${esc(st.admin_user)}</code>${sec.weak_password ? ` <span class="badge danger">${icon("shield-alert", "sm")} weak password: ${esc(sec.weak_password)}</span>` : ` <span class="badge ok">${icon("shield-check", "sm")} password ok</span>`}</dd>
        <dt>Two-factor (TOTP)</dt><dd>${sec.totp.enabled ? `<span class="badge ok">${icon("shield-check", "sm")} enabled${sec.totp.source === "env" ? " · from .env" : ""}</span> <span class="muted small">${sec.totp.recovery_left} recovery code(s) left</span><div class="flex flex-wrap mt"><button class="btn btn-sm" data-action="totpQr">${icon("eye")} QR for another device</button>${sec.totp.source === "env" ? "" : `<button class="btn btn-sm" data-action="totpRecovery">${icon("key")} New recovery codes</button><button class="btn btn-sm btn-danger-ghost" data-action="totpDisable">${icon("x")} Disable</button>`}</div>` : `<span class="badge warn">${icon("shield-alert", "sm")} off</span> <button class="btn btn-sm btn-primary" data-action="totpEnable">${icon("shield-check")} Enable two-factor</button><div class="help">Adds a 6-digit code from an authenticator app to the login.</div>`}</dd>
        <dt>Login throttling</dt><dd>lockout after ${st.login_max_attempts} failures within ${st.login_window_minutes} min, escalating up to 1 h</dd>
        <dt>Session</dt><dd>expires after ${Math.round(st.session_idle_minutes / 60)} h idle, max ${Math.round(st.session_max_hours / 24)} days</dd>
        <dt>Cookies</dt><dd>HttpOnly · SameSite=Strict · Secure ${esc(st.secure_cookies)}${st.behind_proxy ? " · proxy headers trusted" : ""}</dd>
        <dt>Secret key</dt><dd>${st.generated_secret ? `<span class="badge warn">auto-generated</span> <span class="muted small">put <code>DOCKBARE_SECRET_KEY</code> into .env</span>` : `<span class="badge ok">from .env</span>`}</dd>
        <dt>This IP</dt><dd>${esc(sec.this_ip.ip)} · ${sec.this_ip.failures} recent failures</dd></dl>
        <div class="help mt">Credentials, TOTP secret and keys come from <code>.env</code>; change them there and restart.</div></div></div>
      <div class="card"><div class="card-h"><h2>Recent logins</h2></div><div class="card-c tight"><div class="table-wrap"><table><thead><tr><th>Time</th><th>IP</th><th>User</th><th>Result</th></tr></thead><tbody>${sec.attempts.map((a) => `<tr><td class="nowrap">${esc(fmtTime(a.ts))}</td><td class="mono">${esc(a.ip)}</td><td>${esc(a.username)}</td><td>${a.success ? `<span class="badge ok">ok</span>` : `<span class="badge danger">failed</span>`}</td></tr>`).join("") || `<tr><td colspan="4" class="muted">No attempts recorded.</td></tr>`}</tbody></table></div></div></div>
    </div>
    <div class="card"><div class="card-h"><h2>Active sessions</h2><div class="actions"><button class="btn btn-sm btn-danger-ghost" data-action="revokeAll">${icon("log-out")} Sign out other sessions</button></div></div><div class="card-c tight"><div class="table-wrap"><table><thead><tr><th>Started</th><th>Last seen</th><th>IP</th><th>Client</th><th></th></tr></thead><tbody>
      ${sec.sessions.map((x) => `<tr><td class="nowrap">${esc(fmtTime(x.created_at))}</td><td class="nowrap">${esc(ago(x.last_seen))}</td><td class="mono">${esc(x.ip)}</td><td class="small ellipsis" title="${esc(x.user_agent)}">${esc((x.user_agent || "").slice(0, 40))}${x.current ? ` <span class="badge info">this</span>` : ""}</td><td><div class="actions">${x.current ? "" : `<button class="btn btn-sm btn-ghost btn-icon" data-action="revoke" data-id="${esc(x.id)}" title="Revoke">${icon("x")}</button>`}</div></td></tr>`).join("")}</tbody></table></div></div></div></div>`;
  const codesHtml = (codes) => `<div class="banner warn">${icon("key")}<div><b>Save these recovery codes now.</b> Each one works once instead of an authenticator code. They are not shown again.</div></div><div class="log" data-codes>${codes.map(esc).join("\n")}</div><div class="flex mt"><button class="btn btn-sm" data-action="copyCodes">${icon("copy")} Copy</button><a class="btn btn-sm" download="dockbare-recovery-codes.txt" href="data:text/plain;charset=utf-8,${encodeURIComponent(codes.join("\n") + "\n")}">${icon("download")} Download</a></div>`;
  bind(c, {
    totpEnable: async () => {
      let d;
      try { d = await api.post("/api/security/totp/setup", {}); } catch (e) { return toast(e.message, "error"); }
      const m = modal({ title: "Enable two-factor authentication", size: "wide", body: `
        <div class="form-row"><div class="center"><img alt="QR code" width="220" height="220" src="data:image/svg+xml;base64,${btoa(unescape(encodeURIComponent(d.qr_svg)))}"></div>
          <div><p class="small">1. Scan the code with Google Authenticator, Aegis, Bitwarden, 1Password or any TOTP app, or enter the secret manually:</p><div class="input-group"><input type="text" value="${esc(d.secret)}" readonly><button type="button" class="btn btn-ghost btn-icon" data-action="copySecret" title="Copy">${icon("copy", "sm")}</button></div>
          <p class="small mt">2. Enter the 6-digit code the app shows now:</p><input type="text" name="code" inputmode="numeric" placeholder="123 456" autocomplete="one-time-code"></div></div>`,
        footer: `<button class="btn" data-close>Cancel</button><button class="btn btn-primary" data-action="activate">${icon("shield-check")} Activate</button>` });
      bind(m.el, {
        copySecret: () => copyText(d.secret),
        activate: async (b) => { await busy(b, async () => { const r = await api.post("/api/security/totp/enable", { code: $("input[name=code]", m.el).value }); $(".modal-h h2", m.el).textContent = "Two-factor is on"; $(".modal-c", m.el).innerHTML = `<p>From now on the login asks for a code from your app.</p>${codesHtml(r.recovery_codes)}`; $(".modal-f", m.el).innerHTML = `<button class="btn btn-primary" data-close>Done</button>`; bind(m.el, { copyCodes: () => copyText(r.recovery_codes.join("\n")) }); $$("[data-close]", m.el).forEach((x) => x.addEventListener("click", () => { m.close(); renderSecurity(c, s); })); }).catch((e) => toast(e.message, "error")); },
      });
    },
    totpDisable: async () => {
      const m = modal({ title: "Disable two-factor", body: `<p class="small">Confirm with your password and a current authenticator (or recovery) code.</p><div class="form-group"><label class="field">Password</label><input type="password" name="password" autocomplete="current-password"></div><div class="form-group"><label class="field">Code</label><input type="text" name="code" inputmode="numeric" autocomplete="one-time-code"></div>`, footer: `<button class="btn" data-close>Cancel</button><button class="btn btn-danger" data-action="go">${icon("x")} Disable</button>` });
      bind(m.el, { go: async (b) => { const f = formValues(m.el); await busy(b, async () => { await api.post("/api/security/totp/disable", f); toast("Two-factor disabled", "success"); m.close(); renderSecurity(c, s); }).catch((e) => toast(e.message, "error")); } });
    },
    totpRecovery: async () => {
      const m = modal({ title: "New recovery codes", body: `<p class="small">This replaces all existing recovery codes. Confirm with a current authenticator code.</p><div class="form-group"><label class="field">Code</label><input type="text" name="code" inputmode="numeric" autocomplete="one-time-code"></div>`, footer: `<button class="btn" data-close>Cancel</button><button class="btn btn-primary" data-action="go">${icon("key")} Generate</button>` });
      bind(m.el, { go: async (b) => { await busy(b, async () => { const r = await api.post("/api/security/totp/recovery", formValues(m.el)); $(".modal-c", m.el).innerHTML = codesHtml(r.recovery_codes); $(".modal-f", m.el).innerHTML = `<button class="btn btn-primary" data-close>Done</button>`; bind(m.el, { copyCodes: () => copyText(r.recovery_codes.join("\n")) }); $$("[data-close]", m.el).forEach((x) => x.addEventListener("click", () => { m.close(); renderSecurity(c, s); })); }).catch((e) => toast(e.message, "error")); } });
    },
    totpQr: () => { modal({ title: "Authenticator QR code", body: `<p class="small muted">Scan with Google Authenticator, Aegis, Bitwarden, 1Password… Anyone who sees this code can generate your login codes.</p><div class="center"><img src="/api/security/totp-qr?t=${Date.now()}" alt="TOTP QR code" width="260" height="260"></div>`, footer: `<button class="btn" data-close>Close</button>` }); },
    revokeAll: async (b) => { const r = await api.del("/api/security/sessions"); toast(`${r.revoked} session(s) revoked`, "success"); renderSecurity(c, s); },
    revoke: async (b) => { await api.del(`/api/security/sessions/${b.dataset.id}`); renderSecurity(c, s); },
  });
}

function renderEnvironment(c, s) {
  const e = s.env;
  c.innerHTML = `<div class="grid cols-2"><div class="card"><div class="card-h"><h2>Runtime</h2></div><div class="card-c"><dl class="kv">
      <dt>.env file</dt><dd class="mono">${esc(e.env_file || "none found (using environment only)")}</dd>
      <dt>Data directory</dt><dd class="mono">${esc(e.data_dir)}</dd><dt>Backup directory</dt><dd class="mono">${esc(e.backup_dir)}</dd>
      <dt>Time zone</dt><dd>${esc(e.timezone)}</dd><dt>Behind proxy</dt><dd>${e.behind_proxy ? "yes (X-Forwarded-* trusted)" : "no"}</dd>
      <dt>Encryption</dt><dd>${e.encryption_configured ? `configured · fingerprint <code>${esc(s.encryption.fingerprint)}</code>` : "not configured"}</dd>
      <dt>Log file</dt><dd class="mono">${esc(s.log_file || "")}</dd></dl></div></div>
    <div class="card"><div class="card-h"><h2>.env reference</h2></div><div class="card-c"><div class="log">${esc(`DOCKBARE_ADMIN_USER=admin
DOCKBARE_ADMIN_PASSWORD=change-me        # or DOCKBARE_ADMIN_PASSWORD_HASH
DOCKBARE_TOTP_SECRET=                    # optional 2FA (python -m dockbare totp-setup)
DOCKBARE_SECRET_KEY=                     # python -m dockbare gen-secret
DOCKBARE_ENCRYPTION_KEY=                 # system-wide backup encryption key
DOCKBARE_BEHIND_PROXY=false              # true when behind Traefik/Caddy/nginx
DOCKBARE_SECURE_COOKIES=auto             # auto | true | false
DOCKBARE_SESSION_IDLE_MINUTES=720
DOCKBARE_SESSION_MAX_HOURS=168
DOCKBARE_LOGIN_MAX_ATTEMPTS=5
DOCKBARE_LOGIN_WINDOW_MINUTES=15
DOCKBARE_HELPER_IMAGE=                   # defaults to the Dockbare image itself
DOCKBARE_PORT=5000
TZ=Europe/Berlin`)}</div><div class="help">Changes require a restart of the container.</div></div></div></div>`;
}


// ------------------------------------------------------------------- peers
async function peerDialog(existing = null) {
  const p = existing || { name: "", url: "", enabled: true, interval_minutes: 5, failure_threshold: 3, alert_on_problems: true, verify_tls: true, timeout: 15 };
  const body = `
    <div class="form-row"><div><label class="field">Name</label><input type="text" name="name" value="${esc(p.name)}" placeholder="Server B"></div><div><label class="field">URL</label><input type="url" name="url" value="${esc(p.url)}" placeholder="https://backup.otherserver.com"></div></div>
    <div class="form-group mt"><label class="field">Peer token of that instance ${p.token_set ? `<span class="muted">(stored; leave blank to keep)</span>` : ""}</label><div class="input-group"><input type="password" name="token" autocomplete="new-password" placeholder="${p.token_set ? "••••••••" : "copied from the other instance's Peers page"}"><button type="button" class="btn btn-ghost btn-icon" data-action="eye">${icon("eye", "sm")}</button></div></div>
    <div class="form-row cols-3"><div><label class="field">Check every (min)</label><input type="number" name="interval_minutes" min="1" max="1440" value="${p.interval_minutes}"></div><div><label class="field">Alert after N failures</label><input type="number" name="failure_threshold" min="1" max="100" value="${p.failure_threshold}"></div><div><label class="field">Timeout (s)</label><input type="number" name="timeout" min="3" max="120" value="${p.timeout}"></div></div>
    <div class="stack gap-2 mt">${switchHtml("alert_on_problems", p.alert_on_problems, "Also alert when the peer reports problems (failed backups, overdue schedules, Docker down, remotes down, low disk)")}${switchHtml("verify_tls", p.verify_tls, "Verify TLS certificate")}${switchHtml("enabled", p.enabled, "Enabled")}</div>
    <div class="mt" data-result></div>`;
  return new Promise((resolve) => {
    const m = modal({ title: existing ? `Edit peer: ${existing.name}` : "Add peer instance", body, size: "wide", footer: `<button class="btn left" data-action="test">${icon("zap")} Test</button><button class="btn" data-close>Cancel</button><button class="btn btn-primary" data-action="save">${icon("check")} Save</button>`, onClose: () => resolve(false) });
    const payload = () => ({ ...formValues(m.el), id: existing?.id });
    bind(m.el, {
      eye: (b) => { const i = $("input[name=token]", m.el); i.type = i.type === "password" ? "text" : "password"; b.innerHTML = icon(i.type === "password" ? "eye" : "eye-off", "sm"); },
      test: async (btn) => { const out = $("[data-result]", m.el); await busy(btn, async () => { const r = await api.post("/api/peers/test", payload()); out.innerHTML = `<div class="banner ${r.ok ? "success" : "danger"}">${icon(r.ok ? "circle-check" : "circle-x")}<div>${esc(r.message)}${r.status?.problems?.length ? `<br>Problems: ${esc(r.status.problems.join("; "))}` : ""}</div></div>`; }).catch((e) => { out.innerHTML = `<div class="banner danger">${icon("circle-x")}<div>${esc(e.message)}</div></div>`; }); },
      save: async (btn) => { await busy(btn, async () => { existing ? await api.put(`/api/peers/${existing.id}`, payload()) : await api.post("/api/peers", payload()); toast("Peer saved", "success"); resolve(true); m.close(); }).catch((e) => toast(e.message, "error")); },
    });
  });
}

export const peers = {
  title: "Peers",
  actions: () => `<button class="btn btn-primary" data-action="add">${icon("plus")} Add peer</button>`,
  async render(el) {
    const seq = (this._seq = (this._seq || 0) + 1);
    const d = await api.get("/api/peers");
    const me = d.self;
    const statusBadgeP = (p) => { const st = p.status; if (!p.enabled) return `<span class="badge outline">disabled</span>`; if (!st) return `<span class="badge">${icon("loader", "spin sm")} not checked yet</span>`; if (p.alert_active) return `<span class="badge danger">${icon("x", "sm")} DOWN</span>`; if (!st.ok) return `<span class="badge warn">${icon("alert", "sm")} check failed (${p.consecutive_failures}/${p.failure_threshold})</span>`; return st.problems.length ? `<span class="badge warn">${icon("alert", "sm")} reachable, ${st.problems.length} problem(s)</span>` : `<span class="badge ok">${icon("check", "sm")} healthy</span>`; };
    if (seq !== this._seq) return;
    el.innerHTML = `
      <div class="banner info">${icon("heart-pulse")}<div><b>Mutual watchdog.</b> Point this instance at other Dockbare instances (and them at this one). Each side polls the other's status endpoint; after the configured number of failed checks it raises an urgent notification, and again when the peer is back. Peers can watch each other even though they back up different volumes. Peers can also be defined in <code>.env</code> (<code>DOCKBARE_PEER_1_URL</code>, <code>_TOKEN</code>, …).</div></div>
      <div class="card mb"><div class="card-h"><h2>This instance</h2><span class="muted small">${esc(me.hostname)} · ${esc(d.token_source === "env" ? "token from .env" : "generated token")}</span></div><div class="card-c">
        <div class="form-row"><div><label class="field">Peer token (give this to the other instances)</label><div class="input-group"><input type="password" value="${esc(d.token)}" readonly data-token><button type="button" class="btn btn-ghost btn-icon" data-action="eyeToken">${icon("eye", "sm")}</button></div>
          <div class="flex mt"><button class="btn btn-sm" data-action="copyToken">${icon("copy")} Copy</button>${d.token_source === "env" ? `<span class="muted small">set by <code>DOCKBARE_PEER_TOKEN</code></span>` : `<button class="btn btn-sm btn-danger-ghost" data-action="regen">${icon("refresh")} Regenerate</button>`}</div></div>
          <div><label class="field">What peers see</label><div class="pills">${me.problems.length ? me.problems.map((x) => `<span class="badge warn">${esc(x)}</span>`).join("") : `<span class="badge ok">${icon("check", "sm")} no problems</span>`}</div><div class="help">${me.backups.count} backups · last ${esc(ago(me.backups.last_at))} · ${me.schedules.active} active schedules · Docker ${me.docker_ok ? "ok" : "down"}. Endpoint: <code>${esc(location.origin)}/api/peer/status</code></div></div></div></div></div>
      ${d.peers.length ? `<div class="grid cols-2">${d.peers.map((p) => { const st = p.status || {}, r = st.remote || {}, env = p.source === "env"; return `<div class="card"><div class="card-h"><h2 class="ellipsis">${esc(p.name)}</h2>${statusBadgeP(p)}${env ? `<span class="badge info" title="Defined by DOCKBARE_PEER_* in .env">${icon("lock", "sm")} .env</span>` : ""}<div class="actions"><label class="switch"><input type="checkbox" data-action="toggle" data-id="${esc(p.id)}" ${p.enabled ? "checked" : ""} ${env ? "disabled" : ""}><span class="track"></span></label></div></div>
        <div class="card-c"><dl class="kv"><dt>URL</dt><dd class="mono">${esc(p.url)}</dd><dt>Checked</dt><dd>${st.checked_at ? `${esc(ago(st.checked_at))} · every ${p.interval_minutes} min` : "—"}${p.last_ok_at ? `<div class="muted small">last ok ${esc(ago(p.last_ok_at))}</div>` : ""}</dd>
          ${st.error ? `<dt>Error</dt><dd class="danger">${esc(st.error)}</dd>` : ""}
          ${r.hostname ? `<dt>Remote</dt><dd>${esc(r.hostname)} · Dockbare ${esc(r.version)} · up ${esc(dur(r.uptime || 0))}<div class="muted small">${r.backups?.count ?? "?"} backups, last ${esc(ago(r.backups?.last_at))} · ${r.schedules?.active ?? "?"} schedules · ${r.remotes?.length ?? 0} remotes · ${r.encryption ? "encrypted" : "no encryption"}</div></dd>` : ""}
          ${(st.problems || []).length ? `<dt>Problems</dt><dd><div class="stack gap-2">${st.problems.map((x) => `<span class="badge warn">${esc(x)}</span>`).join("")}</div></dd>` : ""}</dl>
          <div class="flex mt"><button class="btn btn-sm" data-action="check" data-id="${esc(p.id)}">${icon("zap")} Check now</button><span class="grow"></span>${env ? `<span class="muted small">edit in .env</span>` : `<button class="btn btn-sm btn-ghost" data-action="edit" data-id="${esc(p.id)}">${icon("pencil")} Edit</button><button class="btn btn-sm btn-danger-ghost" data-action="del" data-id="${esc(p.id)}">${icon("trash")}</button>`}</div></div></div>`; }).join("")}</div>` : `<div class="card">${emptyState("No peers yet. Add another Dockbare instance to watch it.", "heart-pulse")}</div>`}`;
    bind(el, {
      add: () => peerDialog().then((ch) => ch && this.render(el)),
      edit: (b) => peerDialog(d.peers.find((p) => p.id === b.dataset.id)).then((ch) => ch && this.render(el)),
      del: async (b) => { const p = d.peers.find((x) => x.id === b.dataset.id); if (!(await confirmDialog({ title: "Remove peer", text: `Stop watching <b>${esc(p.name)}</b>?`, confirmLabel: "Remove", danger: true }))) return; try { await api.del(`/api/peers/${p.id}`); this.render(el); } catch (e) { toast(e.message, "error"); } },
      check: async (b) => { await busy(b, async () => { const p = await api.post(`/api/peers/${b.dataset.id}/check`, {}); toast(p.status?.ok ? "Peer reachable" : `Check failed: ${p.status?.error}`, p.status?.ok ? "success" : "error", 6000); this.render(el); }).catch((e) => toast(e.message, "error")); },
      toggle: async (sw) => { const p = d.peers.find((x) => x.id === sw.dataset.id); try { await api.put(`/api/peers/${p.id}`, { name: p.name, url: p.url, enabled: sw.checked, interval_minutes: p.interval_minutes, failure_threshold: p.failure_threshold, alert_on_problems: p.alert_on_problems, verify_tls: p.verify_tls, timeout: p.timeout }); this.render(el); } catch (e) { toast(e.message, "error"); } },
      eyeToken: (b) => { const i = $("[data-token]", el); i.type = i.type === "password" ? "text" : "password"; b.innerHTML = icon(i.type === "password" ? "eye" : "eye-off", "sm"); },
      copyToken: () => copyText(d.token),
      regen: async (b) => { if (!(await confirmDialog({ title: "Regenerate peer token", text: "Other instances watching this one will fail their checks until you give them the new token.", confirmLabel: "Regenerate", danger: true }))) return; await busy(b, async () => { await api.post("/api/peers/token/regenerate", {}); toast("Token regenerated", "success"); this.render(el); }).catch((e) => toast(e.message, "error")); },
    });
  },
  onAction: { add: (btn, ctx) => peerDialog().then((ch) => ch && ctx.refresh()) },
};


// ------------------------------------------------------------------ uptime
function monitorDialog(row, defaults) {
  const m = row.monitor || defaults;
  const body = `
    <p class="muted small">${esc(row.name)}${row.image ? ` · <span class="mono">${esc(row.image)}</span>` : ""}${row.project ? ` · project ${esc(row.project)}` : ""}</p>
    <div class="stack gap-2">
      ${switchHtml("enabled", m.enabled, "Monitor this container")}
      ${switchHtml("notify", m.notify, "Send notifications on outage and recovery")}
      ${switchHtml("require_healthy", m.require_healthy, "Treat an unhealthy health check as an outage")}
      ${switchHtml("alert_on_stop", m.alert_on_stop, "Treat a clean stop (exit code 0) as an outage")}
      ${switchHtml("auto_restart", m.auto_restart, "Restart automatically when it fails")}
    </div>
    <div class="form-row cols-3 mt"><div><label class="field">Max restarts</label><input type="number" name="max_restarts" min="0" max="100" value="${m.max_restarts}"></div><div><label class="field">per (minutes)</label><input type="number" name="restart_window_minutes" min="1" max="1440" value="${m.restart_window_minutes}"></div><div><label class="field">Grace period (s)</label><input type="number" name="grace_seconds" min="0" max="3600" value="${m.grace_seconds}"></div></div>
    <div class="help">After the restart budget is used up Dockbare stops restarting and sends a "giving up" notification. The grace period avoids alerts for quick restarts.</div>
    <hr><div class="form-row"><div class="stack gap-2"><label class="field">Public status page</label>${switchHtml("public", m.public, "Show on the public status page")}</div><div><label class="field">Display name</label><input type="text" name="display_name" value="${esc(m.display_name || "")}" placeholder="${esc(row.name)}"></div></div>`;
  return new Promise((resolve) => {
    const d = modal({ title: `Monitor: ${row.name}`, body, footer: `${m.explicit ? `<button class="btn btn-ghost left" data-action="reset">${icon("refresh")} Use defaults</button>` : ""}<button class="btn" data-close>Cancel</button><button class="btn btn-primary" data-action="save">${icon("check")} Save</button>`, onClose: () => resolve(false) });
    bind(d.el, {
      save: async (b) => { await busy(b, async () => { await api.put(`/api/uptime/monitors/${encodeURIComponent(row.name)}`, formValues(d.el)); toast("Monitor saved", "success"); resolve(true); d.close(); }).catch((e) => toast(e.message, "error")); },
      reset: async (b) => { await busy(b, async () => { await api.del(`/api/uptime/monitors/${encodeURIComponent(row.name)}`); resolve(true); d.close(); }).catch((e) => toast(e.message, "error")); },
    });
  });
}

let pastOpen = false;
export const uptime = {
  title: "Uptime",
  pollMs: 10000,
  actions: () => `<a class="btn" href="/uptime" target="_blank" rel="noopener" data-public-link>${icon("globe")} Public page</a><button class="btn" data-action="poll">${icon("refresh")} Check now</button>`,
  async render(el) {
    const seq = (this._seq = (this._seq || 0) + 1);
    const d = await api.get("/api/uptime");
    const s = d.settings, sum = d.summary;
    const rows = d.containers;
    const open = d.incidents.filter((i) => !i.ended_at), closed = d.incidents.filter((i) => i.ended_at);
    const stateBadge = (r) => {
      if (r.missing) return `<span class="badge danger">${icon("x", "sm")} missing</span>`;
      if (r.failure) return `<span class="badge danger">${icon("x", "sm")} ${esc(r.failure)}${r.incident ? "" : " (grace)"}</span>`;
      if (r.state === "running") return `<span class="badge ok">${icon("check", "sm")} running${r.health ? " · " + esc(r.health) : ""}</span>`;
      if (r.state === "exited") return `<span class="badge outline" title="${esc(r.ignored || "")}">stopped (exit ${r.exit_code ?? "?"})${r.ignored ? ` · <span title="Stopped before Dockbare saw it running; enable the monitor explicitly to alert on it.">not alerting</span>` : ""}</span>`;
      return `<span class="badge outline">${esc(r.state)}</span>`;
    };
    const bar = (v) => `<span class="uptime-bar" title="${v.toFixed(3)}%"><i data-w="${v}"></i></span>${v >= 99.995 ? "100" : v.toFixed(2)}%`;
    if (seq !== this._seq) return;
    el.innerHTML = `
      ${sum.error ? `<div class="banner danger">${icon("alert")}<div>Docker not reachable: ${esc(sum.error)}</div></div>` : ""}
      <div class="grid cols-4 mb">
        <div class="card stat"><div class="label">${icon("container", "sm")} Monitored</div><div class="value">${sum.monitored} <span class="muted">/ ${sum.total}</span></div><div class="sub">containers</div></div>
        <div class="card stat"><div class="label">${icon("radar", "sm")} Down now</div><div class="value ${sum.down.length ? "danger" : ""}">${sum.down.length}</div><div class="sub">${esc(sum.down.join(", ") || "all good")}</div></div>
        <div class="card stat"><div class="label">${icon("activity", "sm")} Incidents 24h</div><div class="value">${sum.incidents_24h}</div><div class="sub">last check ${esc(ago(sum.last_poll))}</div></div>
        <div class="card stat"><div class="label">${icon("globe", "sm")} Public page</div><div class="value">${s.public_enabled ? "On" : "Off"}</div><div class="sub">${rows.filter((r) => r.monitor?.public).length} container(s) listed</div></div>
      </div>
      ${open.length ? `<div class="card mb"><div class="card-h"><h2>${icon("radar", "sm")} Detected outages</h2><span class="muted small">from container monitoring</span></div><div class="card-c tight">${open.map((i) => { const f = i.facts || {}; return `<div class="inc-row"><div class="inc-head"><span class="dot bad"></span><b>${esc(i.container)}</b><span class="badge solid-danger">${esc({ down: "exited", oom: "out of memory", unhealthy: "unhealthy", "restart-loop": "restart loop", missing: "missing", dead: "dead" }[i.kind] || i.kind)}</span>${f.exit_code != null ? `<span class="badge outline">exit ${esc(f.exit_code)}</span>` : ""}${f.oom ? `<span class="badge outline">OOM</span>` : ""}<span class="muted small nowrap">${icon("clock", "sm")} ${esc(ago(i.started_at))}</span><span class="actions">${i.actions.length ? i.actions.map((a) => `<span class="badge ${a.ok === false ? "danger" : "outline"}">${icon(a.type === "restart" ? "refresh" : "alert", "sm")} ${esc(a.note)}</span>`).join("") : ""}<button class="btn btn-sm" data-action="restart" data-name="${esc(i.container)}">${icon("refresh")} Restart</button></span></div>${f.health || f.error ? `<div class="inc-out"><code>${esc(f.health || f.error)}</code></div>` : ""}${f.status ? `<div class="muted small">${esc(f.status)}</div>` : ""}</div>`; }).join("")}</div></div>` : ""}
      <div class="card mb"><div class="card-h"><h2>Announcements</h2><span class="muted small">manual incidents and maintenance windows for the status page</span><div class="actions"><button class="btn btn-sm" data-action="newMaint">${icon("wrench")} Schedule maintenance</button><button class="btn btn-sm btn-danger" data-action="newInc">${icon("alert")} Declare incident</button></div></div>
        <div class="card-c tight">${(() => {
          const act = d.announcements.filter((a) => a.active || a.upcoming), past = d.announcements.filter((a) => !(a.active || a.upcoming));
          const STEPS = ["investigating", "identified", "monitoring", "resolved"], MSTEPS = ["scheduled", "in_progress", "completed"];
          const SICON = { investigating: "search", identified: "eye", monitoring: "activity", resolved: "check", scheduled: "calendar", in_progress: "wrench", completed: "check" };
          const row = (a) => { const isInc = a.type === "incident"; const closed = isInc ? a.status === "resolved" : a.status === "completed"; const ups = a.updates || []; const steps = isInc ? STEPS : MSTEPS; const cur = steps.indexOf(a.status);
            return `<div class="ann-row ${closed ? "closed" : ""}">
              <div class="ann-head"><span class="badge ${!isInc ? "solid-info" : closed ? "ok" : a.severity === "major" ? "solid-danger" : "solid-warn"}">${icon(isInc ? "alert" : "wrench", "sm")} ${isInc ? esc(a.severity) : "maintenance"}</span><b class="ann-title">${esc(a.title)}</b>${a.public ? "" : `<span class="badge outline" title="not on the public page">${icon("eye-off", "sm")} private</span>`}
                <span class="muted small nowrap">${icon(isInc ? "clock" : "calendar", "sm")} ${esc(fmtTime(a.starts_at))}${a.ends_at ? ` → ${esc(fmtTime(a.ends_at))}` : ""}</span><span class="muted small">${icon("container", "sm")} ${a.services.length ? esc(a.services.join(", ")) : "all"}</span>
                <span class="actions"><span class="badge ${closed ? "ok" : "info"}">${icon(SICON[a.status] || "info", "sm")} ${esc(a.status.replace("_", " "))}</span><button class="btn btn-sm btn-primary" data-action="updAnn" data-id="${a.id}">${icon("send")} Post update</button><button class="btn btn-sm btn-ghost btn-icon" data-action="editAnn" data-id="${a.id}" title="Edit title, message, affected containers">${icon("pencil")}</button><button class="btn btn-sm btn-danger-ghost btn-icon" data-action="delAnn" data-id="${a.id}" title="Delete">${icon("trash")}</button></span></div>
              ${ups.length ? `<div class="ann-updates">${ups.map((u) => `<button type="button" class="ann-upd" data-action="editUpd" data-id="${a.id}" data-uid="${u.id}" title="Click to edit"><span class="badge outline">${icon(SICON[u.status] || "info", "sm")} ${esc(u.status.replace("_", " "))}</span><span class="muted small nowrap">${esc(fmtTime(u.ts))}</span><span class="grow">${esc(u.message || "")}</span>${icon("pencil", "sm")}</button>`).join("")}</div>` : ""}
            </div>`; };
          return `${act.length ? act.map(row).join("") : `<div class="empty small">No active announcements. Declare an incident when something is broken that monitoring cannot see, or schedule a maintenance window to suppress alerts for the affected containers.</div>`}
            ${past.length ? `<details class="ann-past" ${pastOpen ? "open" : ""}><summary>${icon("clock", "sm")} Past · ${past.length}</summary>${past.map(row).join("")}</details>` : ""}`;
        })()}</div></div>
      <div class="card mb"><div class="card-h"><h2>Containers</h2><span class="muted small">${s.monitor_new ? "new containers are monitored automatically" : "only containers with explicit settings are monitored"}</span></div><div class="card-c tight"><div class="table-wrap"><table><thead><tr><th>Container</th><th>State</th><th>Availability (last 30 days)</th><th>Monitor</th><th></th></tr></thead><tbody>
        ${rows.map((r) => { const m = r.monitor || {}; return `<tr class="${r.monitored ? "" : "muted"}"><td><div class="flex"><span class="dot state-dot ${r.failure ? "bad" : r.state === "running" ? "ok" : "off"}"></span><b>${esc(r.name)}</b>${r.self ? `<span class="badge info">this app</span>` : ""}</div><div class="muted small">${esc(r.status || "")}${r.project ? ` · ${esc(r.project)}/${esc(r.service || "")}` : ""}</div></td>
          <td>${stateBadge(r)}${r.incident ? `<div class="muted small">since ${esc(ago(r.incident.started_at))}</div>` : ""}${r.maintenance ? `<div class="badge info" title="alerts suppressed by maintenance window">${icon("wrench", "sm")} ${esc(r.maintenance)}</div>` : ""}</td>
          <td class="small"><div class="ubars-sm-wrap">${historyBars(r.history || [], { height: 14 }).replace('class="ubars"', 'class="ubars ubars-sm"')}</div><div class="muted nowrap" title="availability over the last 24 hours, 7 days and 30 days"><span class="win">24h</span> ${r.uptime.d1 >= 99.995 ? "100" : r.uptime.d1.toFixed(2)}% · <span class="win">7d</span> ${r.uptime.d7 >= 99.995 ? "100" : r.uptime.d7.toFixed(2)}% · <span class="win">30d</span> ${r.uptime.d30 >= 99.995 ? "100" : r.uptime.d30.toFixed(2)}%</div></td>
          <td><div class="pills">${r.monitored ? `<span class="badge ok" title="monitored">${icon("radar", "sm")}</span>` : `<span class="badge outline">off</span>`}${m.notify && r.monitored ? `<span class="badge outline" title="notifications">${icon("bell", "sm")}</span>` : ""}${m.auto_restart ? `<span class="badge outline" title="auto-restart ${r.restarts_in_window}/${m.max_restarts} used">${icon("refresh", "sm")} ${r.restarts_in_window}/${m.max_restarts}</span>` : ""}${m.public ? `<span class="badge outline" title="on public page">${icon("globe", "sm")}</span>` : ""}${m.explicit ? "" : `<span class="muted small">defaults</span>`}</div></td>
          <td><div class="actions">${r.self || r.missing ? "" : `<button class="btn btn-sm btn-ghost btn-icon" data-action="restart" data-name="${esc(r.name)}" title="Restart container">${icon("refresh")}</button>`}<button class="btn btn-sm btn-ghost btn-icon" data-action="edit" data-name="${esc(r.name)}" title="Monitor settings">${icon("settings")}</button></div></td></tr>`; }).join("") || `<tr><td colspan="5" class="muted">No containers found.</td></tr>`}</tbody></table></div></div></div>
      <div class="mb">${barsLegend()}</div>
      <div class="grid cols-2">
        <div class="card"><div class="card-h"><h2>Settings</h2></div><div class="card-c">
          <div class="form-row"><div><label class="field">Check interval (seconds)</label><input type="number" name="poll_seconds" min="5" max="600" value="${s.poll_seconds}"></div><div><label class="field">Keep incidents (days)</label><input type="number" name="incident_keep_days" min="7" value="${s.incident_keep_days}"></div></div>
          <div class="stack gap-2 mt">${switchHtml("monitor_new", s.monitor_new, "Monitor every container by default (defaults: notify, no auto-restart)")}${switchHtml("public_enabled", s.public_enabled, "Enable the public status page at /uptime (no login)")}${switchHtml("public_show_peers", s.public_show_peers, "Include peers and their public services on it")}</div>
          <div class="form-group mt"><label class="field">Public page title</label><input type="text" name="public_title" value="${esc(s.public_title)}"></div>
          <div class="help mb">Docker events trigger an immediate check; the interval is the fallback. Containers mark as public in their monitor settings.</div>
          <button class="btn btn-primary" data-action="save">${icon("check")} Save</button></div></div>
        <div class="card"><div class="card-h"><h2>Incident history</h2></div><div class="card-c tight">${closed.length ? `<div class="table-wrap"><table><thead><tr><th>Container</th><th>Kind</th><th>Started</th><th>Duration</th><th>Actions</th></tr></thead><tbody>${closed.slice(0, 30).map((i) => `<tr><td>${esc(i.container)}</td><td><span class="badge outline">${esc(i.kind)}</span></td><td class="nowrap">${esc(fmtTime(i.started_at))}</td><td class="nowrap">${esc(dur(i.duration))}</td><td class="small">${i.actions.length ? i.actions.map((a) => esc(a.type)).join(", ") : "—"}</td></tr>`).join("")}</tbody></table></div>` : emptyState("No incidents recorded.", "radar")}</div></div>
      </div>`;
    applyWidths(el);
    enableBarTooltips();
    $$("[data-narrow]", el).forEach((i) => (i.style.width = "140px"));
    $$(".ann-past", el).forEach((dt) => dt.addEventListener("toggle", () => (pastOpen = dt.open)));
    const defaults = { ...d.defaults, explicit: false };
    const names = rows.map((r) => r.name);
    bind(el, {
      newInc: () => announcementDialog("incident", null, names).then((ch) => ch && this.render(el)),
      newMaint: () => announcementDialog("maintenance", null, names).then((ch) => ch && this.render(el)),
      editAnn: (b) => announcementDialog(null, d.announcements.find((a) => a.id === Number(b.dataset.id)), names).then((ch) => ch && this.render(el)),
      updAnn: (b) => updateDialog(d.announcements.find((a) => a.id === Number(b.dataset.id))).then((ch) => ch && this.render(el)),
      delAnn: async (b) => { if (!(await confirmDialog({ title: "Delete announcement", text: "Remove it from the status page and history?", confirmLabel: "Delete", danger: true }))) return; try { await api.del(`/api/uptime/announcements/${b.dataset.id}`); this.render(el); } catch (e) { toast(e.message, "error"); } },
      editUpd: (b) => { const a = d.announcements.find((x) => x.id === Number(b.dataset.id)); const u = a.updates.find((x) => x.id === Number(b.dataset.uid)); editUpdateDialog(a, u).then((ch) => ch && this.render(el)); },
      edit: (b) => monitorDialog(rows.find((r) => r.name === b.dataset.name), defaults).then((ch) => ch && this.render(el)),
      restart: async (b) => { if (!(await confirmDialog({ title: "Restart container", text: `Restart <b>${esc(b.dataset.name)}</b> now?`, confirmLabel: "Restart" }))) return; try { await api.post(`/api/uptime/containers/${encodeURIComponent(b.dataset.name)}/restart`, {}); toast("Restart requested", "success"); setTimeout(() => this.render(el), 2500); } catch (e) { toast(e.message, "error"); } },
      save: async (b) => { await busy(b, async () => { await api.put("/api/uptime/settings", formValues(el)); toast("Uptime settings saved", "success"); this.render(el); }).catch((e) => toast(e.message, "error")); },
    });
  },
  onAction: { poll: async (b, ctx) => { await api.post("/api/uptime/poll", {}); ctx.refresh(); } },
};


const toLocalInput = (ts) => { const d = new Date((ts || Date.now() / 1000) * 1000); const p = (n) => String(n).padStart(2, "0"); return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`; };
const fromLocalInput = (v) => (v ? new Date(v).getTime() / 1000 : null);

function announcementDialog(type, existing, names) {
  type = existing ? existing.type : type;
  const a = existing || { title: "", message: "", services: [], severity: "minor", status: type === "incident" ? "investigating" : "scheduled", starts_at: Date.now() / 1000, ends_at: type === "maintenance" ? Date.now() / 1000 + 3600 : null, public: true };
  const isInc = type === "incident";
  const body = `
    <div class="form-group"><label class="field">Title</label><input type="text" name="title" value="${esc(a.title)}" placeholder="${isInc ? "Storage array degraded" : "Database upgrade"}"></div>
    <div class="form-group"><label class="field">Message (shown on the status page)</label><textarea name="message" rows="3">${esc(a.message || "")}</textarea></div>
    ${isInc ? `<div class="form-row"><div><label class="field">Severity</label><select name="severity"><option value="minor" ${a.severity === "minor" ? "selected" : ""}>Minor (partial outage)</option><option value="major" ${a.severity === "major" ? "selected" : ""}>Major (outage)</option></select></div><div><label class="field">Status</label><select name="status">${["investigating", "identified", "monitoring", "resolved"].map((x) => `<option value="${x}" ${a.status === x ? "selected" : ""}>${x}</option>`).join("")}</select></div></div>
      <div class="form-group mt"><label class="field">Started</label><input type="datetime-local" name="starts_at" value="${toLocalInput(a.starts_at)}"></div>`
    : `<div class="form-row"><div><label class="field">Start</label><input type="datetime-local" name="starts_at" value="${toLocalInput(a.starts_at)}"></div><div><label class="field">End</label><input type="datetime-local" name="ends_at" value="${toLocalInput(a.ends_at)}"></div></div><div class="help">During the window, outages of the affected containers neither alert nor count as downtime.</div>`}
    <div class="form-group mt"><label class="field">Affected containers (none = all)</label><div class="check-list">${names.map((n) => `<label class="check"><input type="checkbox" name="services" data-multi="1" value="${esc(n)}" ${(a.services || []).includes(n) ? "checked" : ""}> ${esc(n)}</label>`).join("")}</div></div>
    <div class="form-group">${switchHtml("public", a.public !== false, "Show on the public status page")}</div>`;
  return new Promise((resolve) => {
    const m = modal({ title: existing ? `Edit ${type}` : isInc ? "Declare incident" : "Schedule maintenance", body, size: "wide", footer: `<button class="btn" data-close>Cancel</button><button class="btn ${isInc ? "btn-danger" : "btn-primary"}" data-action="save">${icon("check")} ${existing ? "Save" : isInc ? "Declare" : "Schedule"}</button>`, onClose: () => resolve(false) });
    bind(m.el, {
      save: async (b) => { const f = formValues(m.el); const payload = { type, title: f.title, message: f.message, services: f.services || [], public: f.public, severity: f.severity, status: f.status, starts_at: fromLocalInput(f.starts_at), ends_at: f.ends_at ? fromLocalInput(f.ends_at) : undefined };
        await busy(b, async () => { existing ? await api.put(`/api/uptime/announcements/${existing.id}`, payload) : await api.post("/api/uptime/announcements", payload); toast(existing ? "Saved" : isInc ? "Incident declared" : "Maintenance scheduled", "success"); resolve(true); m.close(); }).catch((e) => toast(e.message, "error")); },
    });
  });
}


const ANN_STATUSES = { incident: ["investigating", "identified", "monitoring", "resolved"], maintenance: ["scheduled", "in_progress", "completed"] };
const ANN_SICON = { investigating: "search", identified: "eye", monitoring: "activity", resolved: "check", scheduled: "calendar", in_progress: "wrench", completed: "check" };
function statusPicker(type, current) {
  return `<div class="status-pick">${ANN_STATUSES[type].map((st) => `<label class="sp ${st === current ? "cur" : ""}"><input type="radio" name="status" value="${st}" ${st === current ? "checked" : ""}>${icon(ANN_SICON[st], "sm")} ${st.replace("_", " ")}</label>`).join("")}</div>`;
}
function updateDialog(a) {
  const body = `
    <div class="muted small mb">${esc(a.title)}</div>
    <div class="form-group"><label class="field">Status</label>${statusPicker(a.type, a.status)}<div class="help">${a.type === "incident" ? "Choosing “resolved” closes the incident and moves it to the history." : "Choosing “completed” ends the window and resumes alerts."}</div></div>
    <div class="form-group"><label class="field">Message</label><textarea name="message" rows="4" placeholder="What is happening, what is affected, what is next" autofocus></textarea></div>`;
  return new Promise((resolve) => {
    const m = modal({ title: "Post update", body, size: "wide", footer: `<button class="btn" data-close>Cancel</button><button class="btn btn-primary" data-action="save">${icon("send")} Post</button>`, onClose: () => resolve(false) });
    bind(m.el, {
      save: async (b) => { const f = formValues(m.el); if (!(f.message || "").trim() && f.status === a.status) return toast("Write a message or change the status", "warning"); await busy(b, async () => { await api.post(`/api/uptime/announcements/${a.id}/updates`, { status: f.status, message: f.message }); toast("Update posted", "success"); resolve(true); m.close(); }).catch((e) => toast(e.message, "error")); },
    });
  });
}
function editUpdateDialog(a, u) {
  const body = `
    <div class="muted small mb">${esc(a.title)} · ${esc(fmtTime(u.ts))}</div>
    <div class="form-group"><label class="field">Status</label>${statusPicker(a.type, u.status)}</div>
    <div class="form-group"><label class="field">Message</label><textarea name="message" rows="4">${esc(u.message || "")}</textarea></div>
    <div class="help">If this is the newest update, the incident takes its status.</div>`;
  return new Promise((resolve) => {
    const m = modal({ title: "Edit update", body, footer: `${a.updates.length > 1 ? `<button class="btn btn-danger-ghost left" data-action="del">${icon("trash")} Delete update</button>` : ""}<button class="btn" data-close>Cancel</button><button class="btn btn-primary" data-action="save">${icon("check")} Save</button>`, onClose: () => resolve(false) });
    bind(m.el, {
      save: async (b) => { const f = formValues(m.el); await busy(b, async () => { await api.put(`/api/uptime/announcements/${a.id}/updates/${u.id}`, { status: f.status, message: f.message }); toast("Update saved", "success"); resolve(true); m.close(); }).catch((e) => toast(e.message, "error")); },
      del: async (b) => { await busy(b, async () => { await api.del(`/api/uptime/announcements/${a.id}/updates/${u.id}`); toast("Update deleted", "success"); resolve(true); m.close(); }).catch((e) => toast(e.message, "error")); },
    });
  });
}
