// Utilities page: cleanup, image updates, volume browser and copy, engine health, scheduled report.
// Cleanup never pre-selects anything and every delete is confirmed by typing "delete".
import { $, $$, esc, icon, api, toast, modal, fmtBytes, fmtTime, ago, bind, busy, switchHtml, emptyState, taskLogModal, applyWidths } from "./ui.js";

const TABS = [["cleanup", "Cleanup", "trash"], ["updates", "Image updates", "download"], ["volumes", "Volumes", "database"], ["engine", "Docker engine", "server"], ["report", "Report", "mail"]];

// ------------------------------------------------------------------ helpers
const created = (ts) => (ts ? ago(typeof ts === "number" ? ts : Date.parse(ts) / 1000) : "—");
const sizeOf = (v) => (v == null ? `<span class="muted" title="Docker did not report a size">—</span>` : esc(fmtBytes(v)));
const currentTab = () => { const t = location.hash.replace(/^#\/?/, "").split("/")[1]; return TABS.some(([k]) => k === t) ? t : "cleanup"; };

function tabsHtml(active) {
  return `<div class="tabs" role="tablist">${TABS.map(([k, label, ic]) => `<button type="button" role="tab" aria-selected="${k === active}" class="${k === active ? "active" : ""}" data-tab="${k}">${icon(ic, "sm")} ${label}</button>`).join("")}</div>`;
}

// Re-render once a background task has finished (the task log dialog shows progress meanwhile).
function afterTask(id, then) {
  const tick = async () => {
    try { const t = await api.get(`/api/tasks/${id}`); if (["queued", "running"].includes(t.status)) { setTimeout(tick, 1500); return; } } catch { /* gone */ }
    then();
  };
  setTimeout(tick, 1500);
}

// ================================================================== cleanup
const sel = { images: new Set(), volumes: new Set(), containers: new Set(), cache: false };
let backupFirst = true;

function selectedTotals(d) {
  const imgs = d.images.filter((i) => sel.images.has(i.id));
  const vols = d.volumes.filter((v) => sel.volumes.has(v.name));
  const cons = d.containers.filter((c) => sel.containers.has(c.id));
  const bytes = imgs.reduce((a, i) => a + (i.size || 0), 0) + vols.reduce((a, v) => a + (v.size || 0), 0) + cons.reduce((a, c) => a + (c.size || 0), 0)
    + (sel.cache ? d.build_cache.reclaimable : 0);
  return { imgs, vols, cons, bytes, count: imgs.length + vols.length + cons.length + (sel.cache ? 1 : 0) };
}

function diskCard(d) {
  const k = d.disk;
  if (!k) return "";
  const parts = [["Images", k.images, k.images_unused, "s1"], ["Containers", k.containers, k.containers_unused, "s3"], ["Volumes", k.volumes, k.volumes_unused, "s2"], ["Build cache", k.build_cache, k.build_cache_unused, "s4"]];
  const total = parts.reduce((a, p) => a + (p[1] || 0), 0) || 1;
  const unused = parts.reduce((a, p) => a + (p[2] || 0), 0);
  return `<div class="card mb"><div class="card-h"><h2>${icon("hard-drive", "sm")} Docker disk usage</h2><span class="muted small">${esc(fmtBytes(total))} in total · about ${esc(fmtBytes(unused))} unused</span></div><div class="card-c">
    <div class="du-bar" role="img" aria-label="Docker disk usage by type">${parts.map(([n, size, un, cls]) => (size ? `<span class="du-seg ${cls}" data-w="${((size / total) * 100).toFixed(2)}" title="${esc(n)}: ${esc(fmtBytes(size))} (${esc(fmtBytes(un || 0))} unused)"><i data-w="${size ? Math.min(100, ((un || 0) / size) * 100).toFixed(1) : 0}"></i></span>` : "")).join("")}</div>
    <div class="du-legend">${parts.map(([n, size, un, cls]) => `<span><span class="du-sw ${cls}"></span><b>${esc(n)}</b> ${esc(fmtBytes(size || 0))}${un ? ` <span class="muted">· ${esc(fmtBytes(un))} unused</span>` : ""}</span>`).join("")}</div>
    <div class="help mt">The hatched part of each bar is what this page could free. Shared image layers are counted once in the bar but per image in the tables.</div></div></div>`;
}

function containerRows(d) {
  if (!d.containers.length) return emptyState("No stopped containers.", "container");
  return `<div class="table-wrap"><table class="cleanup-table"><thead><tr><th class="chk"></th><th>Container</th><th>Image</th><th>Stopped</th><th class="num">Size</th><th>Notes</th></tr></thead><tbody>
    ${d.containers.map((c) => `<tr class="${c.protected ? "is-protected" : ""}">
      <td class="chk"><input type="checkbox" data-con="${esc(c.id)}" ${sel.containers.has(c.id) ? "checked" : ""} ${c.protected ? "disabled" : ""} aria-label="Select ${esc(c.name)}"></td>
      <td><b>${esc(c.name)}</b>${c.project ? `<div class="muted small">compose project ${esc(c.project)}</div>` : ""}</td>
      <td class="mono small">${esc(c.image || "")}</td>
      <td class="nowrap">${c.finished_at ? esc(ago(c.finished_at)) : `<span class="muted">${esc(c.state)}</span>`}${c.exit_code ? ` <span class="badge outline">exit ${esc(c.exit_code)}</span>` : ""}</td>
      <td class="num">${sizeOf(c.size)}</td>
      <td>${c.protected ? `<span class="badge outline">${icon("lock", "sm")} ${esc(c.protected)}</span>`
        : c.volumes.length ? `<span class="muted small" title="${esc(c.volumes.join(", "))}">keeps its ${c.volumes.length} volume${c.volumes.length === 1 ? "" : "s"}</span>` : ""}</td></tr>`).join("")}
  </tbody></table></div>`;
}

function imageRows(d) {
  if (!d.images.length) return emptyState("No unused images. Every image is used by at least one container.", "layers");
  return `<div class="table-wrap"><table class="cleanup-table"><thead><tr><th class="chk"><input type="checkbox" data-all="images" aria-label="Select all unused images that are not protected"></th><th>Image</th><th class="num">Size</th><th>Created</th><th>Notes</th></tr></thead><tbody>
    ${d.images.map((i) => `<tr class="${i.protected ? "is-protected" : ""}">
      <td class="chk"><input type="checkbox" data-img="${esc(i.id)}" ${sel.images.has(i.id) ? "checked" : ""} ${i.protected ? "disabled" : ""} aria-label="Select ${esc(i.tags[0] || i.short_id)}"></td>
      <td>${i.tags.length ? i.tags.map((t) => `<div class="mono">${esc(t)}</div>`).join("") : `<span class="muted">untagged</span> <span class="mono small">${esc(i.short_id)}</span>`}</td>
      <td class="num">${esc(fmtBytes(i.size))}${i.shared_size ? `<div class="muted small" title="Layers shared with other images are only freed when those go too">${esc(fmtBytes(i.shared_size))} shared</div>` : ""}</td>
      <td class="nowrap">${esc(created(i.created))}</td>
      <td>${i.protected ? `<span class="badge outline">${icon("lock", "sm")} ${esc(i.protected)}</span>`
        : i.dangling ? `<span class="badge outline">old layer, safe to remove</span>`
        : i.local_only ? `<span class="badge warn" title="This image has no registry copy. Removing it means building it again.">${icon("alert", "sm")} built locally, cannot be downloaded again</span>`
        : `<span class="muted small">can be downloaded again</span>`}</td></tr>`).join("")}
  </tbody></table></div>`;
}

function volumeRows(d) {
  if (!d.volumes.length) return emptyState("No unused volumes. Every volume is mounted by at least one container.", "database");
  return `<div class="table-wrap"><table class="cleanup-table"><thead><tr><th class="chk"></th><th>Volume</th><th class="num">Size</th><th>Created</th><th>Backups</th><th>Notes</th></tr></thead><tbody>
    ${d.volumes.map((v) => `<tr class="${v.protected ? "is-protected" : ""}">
      <td class="chk"><input type="checkbox" data-vol="${esc(v.name)}" ${sel.volumes.has(v.name) ? "checked" : ""} ${v.protected ? "disabled" : ""} aria-label="Select ${esc(v.name)}"></td>
      <td><div class="mono ${v.anonymous ? "small" : ""}">${esc(v.anonymous ? v.name.slice(0, 16) + "…" : v.name)}</div>${v.project ? `<div class="muted small">compose project ${esc(v.project)}</div>` : ""}</td>
      <td class="num">${sizeOf(v.size)}</td>
      <td class="nowrap">${esc(created(v.created))}</td>
      <td class="nowrap">${v.backups ? `${v.backups} · last ${esc(ago(v.last_backup))}` : `<span class="muted">none</span>`}</td>
      <td>${v.protected ? `<span class="badge outline">${icon("lock", "sm")} ${esc(v.protected)}</span>`
        : v.project ? `<span class="badge warn" title="docker compose up for this project would use it again">${icon("alert", "sm")} belongs to a compose project</span>`
        : v.anonymous ? `<span class="muted small">anonymous volume of a removed container</span>` : ""}</td></tr>`).join("")}
  </tbody></table></div>`;
}

function actionBar(d) {
  const t = selectedTotals(d);
  return `<div class="cleanup-bar card" data-bar>
    <div class="grow"><b>${t.count ? `${t.count} selected` : "Nothing selected"}</b>${t.count ? ` <span class="muted">· up to ${esc(fmtBytes(t.bytes))}</span>` : ""}
      ${t.vols.length ? `<div class="mt-xs">${switchHtml("backup_first", backupFirst, "Back up volumes before deleting them (recommended)", 'data-backup')}</div>` : ""}</div>
    <button class="btn btn-danger" data-review ${t.count ? "" : "disabled"}>${icon("trash")} Review and delete</button></div>`;
}

function reviewDialog(d, onDone) {
  const t = selectedTotals(d);
  const noBackup = t.vols.length && !backupFirst;
  const list = [
    ...t.cons.map((c) => `<li>${icon("container", "sm")} container <b>${esc(c.name)}</b>${c.volumes.length ? ` <span class="muted">(its volumes are kept)</span>` : ""}</li>`),
    ...t.imgs.map((i) => `<li>${icon("layers", "sm")} image <b>${esc(i.tags.join(", ") || "untagged " + i.short_id)}</b> · ${esc(fmtBytes(i.size))}${i.local_only ? ` <span class="badge warn">built locally</span>` : ""}</li>`),
    ...t.vols.map((v) => `<li>${icon("database", "sm")} volume <b>${esc(v.name)}</b>${v.size != null ? ` · ${esc(fmtBytes(v.size))}` : ""}${v.project ? ` <span class="badge warn">compose: ${esc(v.project)}</span>` : ""}</li>`),
    ...(sel.cache ? [`<li>${icon("box", "sm")} unused build cache · ${esc(fmtBytes(d.build_cache.reclaimable))}</li>`] : []),
  ].join("");
  const body = `
    <p>These will be deleted from Docker. Each one is checked again right before it is removed; anything that came into use in the meantime is skipped.</p>
    <ul class="cleanup-list">${list}</ul>
    ${t.vols.length ? (backupFirst
      ? `<div class="banner info">${icon("shield-check")}<div>Each volume is backed up first (local copy and its remotes). A volume is only deleted when its backup and all uploads succeeded. The backup stays available under Volumes &amp; Backups and in Recovery.</div></div>`
      : `<div class="banner danger">${icon("alert")}<div><b>No backup.</b> The volumes' data is gone for good after this. Turn "Back up volumes before deleting them" on unless you are sure.</div></div>`) : ""}
    <div class="form-group mt"><label class="field">Type <code>delete</code> to confirm</label><input type="text" data-confirm autocomplete="off" spellcheck="false" placeholder="delete"></div>
    <div class="small danger hidden" data-err>Type delete to confirm.</div>`;
  const m = modal({ title: `Delete ${t.count} item${t.count === 1 ? "" : "s"}?`, body, footer: `<button class="btn" data-close>Cancel</button><button class="btn btn-danger" data-go>${icon("trash")} Delete${noBackup ? " without backup" : ""}</button>` });
  const input = $("[data-confirm]", m.el), errEl = $("[data-err]", m.el);
  input.addEventListener("input", () => errEl.classList.add("hidden"));
  input.focus();
  $("[data-go]", m.el).addEventListener("click", async (e) => {
    if (input.value.trim().toLowerCase() !== "delete") { errEl.classList.remove("hidden"); input.focus(); return; }
    await busy(e.currentTarget, async () => {
      const r = await api.post("/api/cleanup/run", {
        containers: t.cons.map((c) => c.id), images: t.imgs.map((i) => i.id), volumes: t.vols.map((v) => ({ name: v.name, created: v.created })),
        backup_first: backupFirst, build_cache: sel.cache,
      });
      m.close();
      sel.images.clear(); sel.volumes.clear(); sel.containers.clear(); sel.cache = false;
      toast("Cleanup started", "info");
      taskLogModal(r.task.id);
      afterTask(r.task.id, onDone);
    }).catch((err) => toast(err.message, "error", 8000));
  });
}

async function renderCleanup(el, rerender) {
  const d = await api.get("/api/cleanup");
  const keep = (set, ok) => [...set].forEach((x) => ok.has(x) || set.delete(x));
  keep(sel.images, new Set(d.images.filter((i) => !i.protected).map((i) => i.id)));
  keep(sel.volumes, new Set(d.volumes.filter((v) => !v.protected).map((v) => v.name)));
  keep(sel.containers, new Set(d.containers.filter((c) => !c.protected).map((c) => c.id)));
  el.innerHTML = `
    <div class="banner info mb">${icon("shield-check")}<div><b>Cleanup only deletes what you tick.</b> "Unused" means no container uses it, running or stopped. Dockbare's own data, volumes with a backup schedule, monitored containers and images Dockbare needs are protected. Every deletion is listed under Activity.</div></div>
    ${diskCard(d)}
    <div class="card mb"><div class="card-h wrap"><h2>${icon("container", "sm")} Stopped containers</h2><span class="muted small hide-sm">removing one keeps its volumes; scan again afterwards to see what it freed</span><div class="actions"><button class="btn btn-sm" data-rescan>${icon("refresh")} Scan again</button></div></div><div class="card-c tight">${containerRows(d)}</div></div>
    <div class="card mb"><div class="card-h wrap"><h2>${icon("layers", "sm")} Unused images</h2><span class="muted small hide-sm">not used by any container; most can simply be downloaded again</span></div><div class="card-c tight">${imageRows(d)}</div></div>
    <div class="card mb"><div class="card-h wrap"><h2>${icon("database", "sm")} Unused volumes</h2><span class="muted small hide-sm">data that no container mounts any more; nothing is selected for you</span></div><div class="card-c tight">${volumeRows(d)}</div></div>
    <div class="card mb"><div class="card-h"><h2>${icon("box", "sm")} Build cache</h2></div><div class="card-c">
      ${d.build_cache.reclaimable ? `<label class="check"><input type="checkbox" data-cache ${sel.cache ? "checked" : ""}> Clear unused build cache (${esc(fmtBytes(d.build_cache.reclaimable))}). Only slows down the next image build.</label>` : `<span class="muted">No unused build cache.</span>`}</div></div>
    ${actionBar(d)}`;
  applyWidths(el);
  const refreshBar = () => { $("[data-bar]", el).outerHTML = actionBar(d); bindBar(); };
  const bindBar = () => {
    $("[data-backup]", el)?.addEventListener("change", (e) => { backupFirst = e.target.checked; });
    $("[data-review]", el)?.addEventListener("click", () => reviewDialog(d, rerender));
  };
  const toggle = (attr, set) => $$(`[data-${attr}]`, el).forEach((cb) => cb.addEventListener("change", () => { cb.checked ? set.add(cb.dataset[attr]) : set.delete(cb.dataset[attr]); refreshBar(); }));
  toggle("img", sel.images); toggle("vol", sel.volumes); toggle("con", sel.containers);
  $("[data-all=images]", el)?.addEventListener("change", (e) => {
    $$("[data-img]:not(:disabled)", el).forEach((cb) => { cb.checked = e.target.checked; e.target.checked ? sel.images.add(cb.dataset.img) : sel.images.delete(cb.dataset.img); });
    refreshBar();
  });
  $("[data-cache]", el)?.addEventListener("change", (e) => { sel.cache = e.target.checked; refreshBar(); });
  $("[data-rescan]", el)?.addEventListener("click", (e) => busy(e.currentTarget, rerender));
  bindBar();
}

// ================================================================== updates
const UPD = { update: ["warn", "download", "update available"], current: ["ok", "check", "up to date"], local: ["outline", "box", "built locally"],
  pinned: ["outline", "lock", "pinned"], skipped: ["outline", "info", "skipped"], unknown: ["outline", "help", "unknown"] };

async function renderUpdates(el, rerender) {
  const { last } = await api.get("/api/utilities/updates");
  const items = last ? [...last.items].sort((a, b) => ["update", "unknown", "current", "local", "pinned", "skipped"].indexOf(a.status) - ["update", "unknown", "current", "local", "pinned", "skipped"].indexOf(b.status)) : [];
  el.innerHTML = `
    <div class="card mb"><div class="card-h wrap"><h2>${icon("download", "sm")} Image updates</h2><span class="muted small">${last ? `checked ${esc(ago(last.checked_at))} · ${last.updates} update${last.updates === 1 ? "" : "s"} available` : "not checked yet"}</span><div class="actions"><button class="btn btn-sm btn-primary" data-check>${icon("refresh")} Check now</button></div></div>
      <div class="card-c tight">${items.length ? `<div class="table-wrap"><table><thead><tr><th>Image</th><th>Used by</th><th>Status</th></tr></thead><tbody>
        ${items.map((i) => { const [cls, ic, label] = UPD[i.status] || UPD.unknown; return `<tr><td class="mono">${esc(i.ref)}</td><td class="small">${esc(i.containers.join(", "))}</td><td><span class="badge ${cls}">${icon(ic, "sm")} ${label}</span>${i.status !== "current" && i.detail ? `<div class="muted small">${esc(i.detail)}</div>` : ""}</td></tr>`; }).join("")}
      </tbody></table></div>` : emptyState(last ? "No running containers." : "Check which running containers have a newer image in their registry.", "download")}</div></div>
    <div class="help">${icon("info", "sm")} Dockbare only reports; it never pulls images or recreates containers. To update one, pull and recreate it where you manage it, for example <code>docker compose pull &amp;&amp; docker compose up -d</code> in its project folder. Private registries need the Docker daemon to be logged in.</div>`;
  $("[data-check]", el).addEventListener("click", (e) => busy(e.currentTarget, async () => {
    const r = await api.post("/api/utilities/updates", {});
    taskLogModal(r.task.id);
    afterTask(r.task.id, rerender);
  }).catch((err) => toast(err.message, "error")));
}

// ================================================================== volumes
const browse = { source: "volume", volume: "", backup: "", path: "" };

async function renderVolumes(el, rerender) {
  const vols = await api.get("/api/volumes");
  const live = vols.filter((v) => !v.missing), all = vols;
  if (!browse.volume || !all.some((v) => v.name === browse.volume)) { browse.volume = (live[0] || all[0] || {}).name || ""; browse.path = ""; browse.backup = ""; }
  const vol = all.find((v) => v.name === browse.volume);
  const backups = browse.volume ? await api.get(`/api/backups?volume=${encodeURIComponent(browse.volume)}`) : [];
  if (vol && vol.missing) browse.source = "backup";
  if (browse.source === "backup" && (!browse.backup || !backups.some((b) => b.rel_path === browse.backup))) browse.backup = backups[0]?.rel_path || "";
  let listing = null, error = null;
  try {
    if (browse.source === "volume" && browse.volume && !vol?.missing) listing = await api.get(`/api/browse/volume/${encodeURIComponent(browse.volume)}?path=${encodeURIComponent(browse.path)}`);
    else if (browse.source === "backup" && browse.backup) listing = await api.get(`/api/browse/backup?backup=${encodeURIComponent(browse.backup)}&path=${encodeURIComponent(browse.path)}`);
  } catch (e) { error = e.message; }
  const crumbs = ["", ...browse.path.split("/").filter(Boolean)];
  const dl = (name) => {
    const p = browse.path ? `${browse.path}/${name}` : name;
    return browse.source === "volume" ? `/api/browse/volume/${encodeURIComponent(browse.volume)}/download?path=${encodeURIComponent(p)}`
      : `/api/browse/backup/download?backup=${encodeURIComponent(browse.backup)}&path=${encodeURIComponent(p)}`;
  };
  el.innerHTML = `
    <div class="card mb"><div class="card-h wrap"><h2>${icon("folder", "sm")} Browse</h2><span class="muted small hide-sm">read-only; download single files${browse.source === "volume" ? " or folders (as .tar)" : ""}</span></div>
      <div class="card-c">
        <div class="form-row cols-3">
          <div><label class="field">Volume</label><select data-vol>${all.map((v) => `<option value="${esc(v.name)}" ${v.name === browse.volume ? "selected" : ""}>${esc(v.name)}${v.missing ? " (deleted, backups only)" : ""}</option>`).join("")}</select></div>
          <div><label class="field">Source</label><select data-src><option value="volume" ${browse.source === "volume" ? "selected" : ""} ${vol?.missing ? "disabled" : ""}>Live volume</option><option value="backup" ${browse.source === "backup" ? "selected" : ""} ${backups.length ? "" : "disabled"}>A backup (${backups.length})</option></select></div>
          <div class="${browse.source === "backup" ? "" : "hidden"}"><label class="field">Backup</label><select data-backup>${backups.map((b) => `<option value="${esc(b.rel_path)}" ${b.rel_path === browse.backup ? "selected" : ""}>${esc(fmtTime(b.created_at))} · ${esc(fmtBytes(b.size))}</option>`).join("")}</select></div>
        </div>
        <div class="crumbs mt">${crumbs.map((c, i) => `<button type="button" class="btn btn-sm btn-ghost" data-crumb="${esc(crumbs.slice(1, i + 1).join("/"))}">${i === 0 ? icon("database", "sm") + " " + esc(browse.volume || "—") : esc(c)}</button>`).join(`<span class="muted">/</span>`)}</div>
      </div>
      <div class="card-c tight">${error ? `<div class="banner danger m-12">${icon("alert")}<div>${esc(error)}</div></div>` : !listing ? emptyState(all.length ? "Choose a volume." : "No volumes.", "folder") : listing.entries.length ? `<div class="table-wrap"><table><thead><tr><th>Name</th><th class="num">Size</th><th>Modified</th><th></th></tr></thead><tbody>
        ${listing.entries.map((e) => `<tr>
          <td>${e.type === "dir" ? `<a href="#" data-open="${esc(e.name)}">${icon("folder", "sm")} ${esc(e.name)}</a>` : `${icon(e.type === "link" ? "external-link" : "file-text", "sm")} ${esc(e.name)}${e.type === "link" ? ` <span class="muted small">link</span>` : ""}`}</td>
          <td class="num">${e.type === "file" ? esc(fmtBytes(e.size)) : ""}</td>
          <td class="nowrap small">${e.mtime ? esc(fmtTime(e.mtime)) : ""}</td>
          <td><div class="actions">${e.type === "file" || (e.type === "dir" && browse.source === "volume") ? `<a class="btn btn-sm btn-ghost btn-icon" href="${dl(e.name)}" download title="Download${e.type === "dir" ? " as .tar" : ""}" aria-label="Download ${esc(e.name)}">${icon("download")}</a>` : ""}</div></td></tr>`).join("")}
        </tbody></table></div>${listing.truncated ? `<div class="help p-list">Only the first 2000 entries are shown.</div>` : ""}` : emptyState("This folder is empty.", "folder")}</div></div>
    <div class="card"><div class="card-h"><h2>${icon("copy", "sm")} Copy a volume</h2><span class="muted small hide-sm">Docker cannot rename volumes; copy, switch your containers over, then delete the old one in Cleanup</span></div>
      <div class="card-c"><div class="form-row cols-3" data-copy>
        <div><label class="field">From</label><select name="source">${live.map((v) => `<option value="${esc(v.name)}" ${v.name === browse.volume ? "selected" : ""}>${esc(v.name)}</option>`).join("")}</select></div>
        <div><label class="field">To (new volume)</label><input type="text" name="target" placeholder="${esc((browse.volume || "data") + "-copy")}" autocomplete="off" spellcheck="false"><div class="small danger hidden" data-copyerr></div></div>
        <div class="stack gap-2"><label class="field">Options</label>${switchHtml("stop_containers", true, "Stop containers using it during the copy")}</div>
      </div><div class="help">The target must not exist yet; nothing is ever overwritten, and the source stays as it is.</div>
      <div class="mt"><button class="btn btn-primary" data-docopy>${icon("copy")} Copy volume</button></div></div></div>`;
  const go = () => rerender();
  $("[data-vol]", el).addEventListener("change", (e) => { browse.volume = e.target.value; browse.path = ""; browse.backup = ""; browse.source = "volume"; go(); });
  $("[data-src]", el).addEventListener("change", (e) => { browse.source = e.target.value; browse.path = ""; go(); });
  $("[data-backup]", el)?.addEventListener("change", (e) => { browse.backup = e.target.value; browse.path = ""; go(); });
  $$("[data-crumb]", el).forEach((b) => b.addEventListener("click", () => { browse.path = b.dataset.crumb; go(); }));
  $$("[data-open]", el).forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); browse.path = browse.path ? `${browse.path}/${a.dataset.open}` : a.dataset.open; go(); }));
  const copyBox = $("[data-copy]", el), errC = $("[data-copyerr]", el), tgt = $("[name=target]", copyBox);
  tgt.addEventListener("input", () => errC.classList.add("hidden"));
  $("[data-docopy]", el).addEventListener("click", (e) => {
    const source = $("[name=source]", copyBox).value, target = tgt.value.trim();
    if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$/.test(target)) { errC.textContent = "Enter a name: letters, digits, _ . - (no spaces)."; errC.classList.remove("hidden"); tgt.focus(); return; }
    if (target === source) { errC.textContent = "Choose a different name than the source."; errC.classList.remove("hidden"); tgt.focus(); return; }
    busy(e.currentTarget, async () => {
      const r = await api.post("/api/volumes/copy", { source, target, stop_containers: $("[name=stop_containers]", copyBox).checked });
      toast(`Copying ${source} to ${target}`, "info");
      taskLogModal(r.task.id);
      afterTask(r.task.id, rerender);
    }).catch((err) => { errC.textContent = err.message; errC.classList.remove("hidden"); });
  });
}

// =================================================================== engine
async function renderEngine(el) {
  const d = await api.get("/api/utilities/engine");
  const e = d.engine;
  const lvl = { warn: ["warn", "alert"], info: ["info", "info"], ok: ["ok", "check"] };
  el.innerHTML = `
    <div class="grid cols-2 mb">
      <div class="card"><div class="card-h"><h2>${icon("server", "sm")} Docker engine</h2></div><div class="card-c"><dl class="kv">
        <dt>Version</dt><dd>${esc(e.version || "?")} <span class="muted">(API ${esc(e.api || "?")})</span></dd>
        <dt>Host</dt><dd>${esc(e.os || "?")} · ${esc(e.arch || "")} · kernel ${esc(e.kernel || "?")}</dd>
        <dt>Resources</dt><dd>${esc(e.cpus ?? "?")} CPUs · ${esc(e.memory ? fmtBytes(e.memory) : "?")}</dd>
        <dt>Containers</dt><dd>${esc(e.running ?? "?")} running of ${esc(e.containers ?? "?")} · ${esc(e.images ?? "?")} images</dd>
        <dt>Storage driver</dt><dd>${esc(e.storage_driver || "?")}</dd>
        <dt>Logging driver</dt><dd>${esc(e.logging_driver || "?")}</dd>
        <dt>cgroup</dt><dd>${esc(e.cgroup || "?")}</dd>
        <dt>Data directory</dt><dd class="mono">${esc(e.root_dir || "?")}</dd>
        <dt>Live restore</dt><dd>${e.live_restore ? "on" : "off"}</dd></dl></div></div>
      <div class="card"><div class="card-h"><h2>${icon("shield-check", "sm")} Checks</h2></div><div class="card-c tight"><div class="rule-list">
        ${d.checks.map((c) => `<div class="check-row"><span class="badge ${lvl[c.level][0]}">${icon(lvl[c.level][1], "sm")} ${c.level === "ok" ? "ok" : c.level === "warn" ? "warning" : "note"}</span><div><b>${esc(c.title)}</b><div class="muted small">${esc(c.detail || "")}</div>${c.items ? `<div class="small mt-xs">${c.items.map((n) => `<span class="tag">${esc(n)}</span>`).join(" ")}</div>` : ""}</div></div>`).join("")}
      </div></div></div>
    </div>
    <div class="card"><div class="card-h"><h2>${icon("file-text", "sm")} Container logging</h2><span class="muted small hide-sm">how each container keeps its logs</span></div><div class="card-c tight">
      ${d.logs.length ? `<div class="table-wrap"><table><thead><tr><th>Container</th><th>State</th><th>Driver</th><th>Rotation</th></tr></thead><tbody>
      ${d.logs.map((c) => { const unl = c.driver === "json-file" && !c.options["max-size"]; return `<tr><td>${esc(c.name)}</td><td class="small">${esc(c.state)}</td><td class="mono small">${esc(c.driver)}</td><td>${unl ? `<span class="badge warn">${icon("alert", "sm")} no size limit</span>` : c.options["max-size"] ? `<span class="badge ok">max ${esc(c.options["max-size"])}${c.options["max-file"] ? ` × ${esc(c.options["max-file"])}` : ""}</span>` : `<span class="muted small">${c.driver === "local" ? "rotated by default" : "handled by the driver"}</span>`}</td></tr>`; }).join("")}
      </tbody></table></div>` : emptyState("No containers.", "container")}</div></div>`;
}

// =================================================================== report
async function renderReport(el) {
  const s = await api.get("/api/utilities/settings");
  el.innerHTML = `
    <div class="card"><div class="card-h"><h2>${icon("mail", "sm")} Scheduled cleanup report</h2></div><div class="card-c" data-form>
      <p class="muted">A notification with how much space could be freed (unused images, volumes, stopped containers, build cache) and which images have updates. It is only sent when there is something to report. <b>It never deletes anything.</b> It goes through the channels in Settings (event "Scheduled cleanup and image update report").</p>
      <div class="form-row cols-3 mt">
        <div><label class="field">Send</label><select name="report">${[["off", "Never"], ["daily", "Every day"], ["weekly", "Every Monday"]].map(([v, l]) => `<option value="${v}" ${s.report === v ? "selected" : ""}>${l}</option>`).join("")}</select></div>
        <div><label class="field">At</label><input type="time" name="report_time" value="${esc(s.report_time)}"></div>
        <div><label class="field">Only when at least (GB) can be freed</label><input type="number" name="report_min_gb" min="0" step="0.5" value="${esc(s.report_min_gb)}"><div class="help">Image updates are always reported.</div></div>
      </div>
      <div class="mt">${switchHtml("report_updates", s.report_updates, "Check images for updates for the report")}</div>
      <div class="flex mt"><button class="btn btn-primary" data-save>${icon("check")} Save</button><button class="btn" data-send>${icon("send")} Send a report now</button></div></div></div>`;
  const f = $("[data-form]", el);
  const vals = () => ({ report: $("[name=report]", f).value, report_time: $("[name=report_time]", f).value, report_min_gb: Number($("[name=report_min_gb]", f).value || 0), report_updates: $("[name=report_updates]", f).checked });
  $("[data-save]", el).addEventListener("click", (e) => busy(e.currentTarget, async () => { await api.put("/api/utilities/settings", vals()); toast("Report settings saved", "success"); }).catch((err) => toast(err.message, "error")));
  $("[data-send]", el).addEventListener("click", (e) => busy(e.currentTarget, async () => {
    const r = await api.post("/api/utilities/report", {});
    const ch = Object.keys(r.sent || {});
    toast(ch.length ? `Report sent via ${ch.join(", ")}` : "No notification channel is enabled (Settings → Notifications)", ch.length ? "success" : "warning", 6000);
  }).catch((err) => toast(err.message, "error")));
}

// ===================================================================== page
const RENDER = { cleanup: renderCleanup, updates: renderUpdates, volumes: renderVolumes, engine: renderEngine, report: renderReport };

export const utilities = {
  title: "Utilities",
  pollMs: 10 * 60 * 1000,  // scans compute sizes; refresh rarely (selections are kept anyway)
  async render(el) {
    const seq = (this._seq = (this._seq || 0) + 1);
    const tab = currentTab();
    if (!$("[data-utabs]", el) || el.dataset.tab !== tab) {
      el.innerHTML = `<div data-utabs>${tabsHtml(tab)}</div><div data-ubody><div class="muted flex">${icon("loader", "spin")} Loading…</div></div>`;
      el.dataset.tab = tab;
      $$("[data-tab]", el).forEach((b) => b.addEventListener("click", () => { location.hash = `#utilities/${b.dataset.tab}`; }));
    }
    // each tab renders straight into the body (its handlers look elements up there later)
    const body = $("[data-ubody]", el);
    const rerender = () => this.render(el);
    try {
      await RENDER[tab](body, rerender);
    } catch (e) {
      if (seq === this._seq) body.innerHTML = `<div class="banner danger">${icon("alert")}<div>${esc(e.message)}</div></div>`;
    }
  },
};
