// System page: host and container resources, disks, alert rules, and per-container charts and logs.
import { $, $$, esc, icon, api, toast, modal, confirmDialog, fmtBytes, ago, bind, formValues, busy, switchHtml, emptyState, applyWidths } from "./ui.js";
import { lineChart, legendHtml, miniSpark } from "./charts.js";

const RANGES = ["1h", "24h", "7d", "30d", "90d"];
const store = {
  get(k, d) { try { return localStorage.getItem(`dockbare.system.${k}`) || d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(`dockbare.system.${k}`, v); } catch { /* private mode */ } },
};
let range = RANGES.includes(store.get("range", "1h")) ? store.get("range", "1h") : "1h";
let sort = store.get("sort", "cpu");
let sortDir = store.get("dir", "desc");

const pct = (v, dec = 1) => `${(v ?? 0).toFixed(dec)}%`;
// whole percent on axes with a large scale, one decimal otherwise (and in tooltips)
const pctFmt = (v, axisMax) => pct(v, axisMax != null && axisMax >= 3 ? 0 : 1);
const rate = (v) => `${fmtBytes(Math.round(v || 0))}/s`;
const rateShort = (v) => fmtBytes(Math.round(v || 0)).replace(" ", "");
const bytesAxis = (v) => fmtBytes(Math.round(v), 0);
const num = (v, dec = 1) => (v == null ? "—" : Number(v).toFixed(dec));

function stateBadge(c) {
  if (c.failure) return `<span class="badge danger">${icon("x", "sm")} ${esc(c.failure)}</span>`;
  if (c.state === "running") return c.health === "unhealthy" ? `<span class="badge danger">${icon("x", "sm")} unhealthy</span>`
    : `<span class="badge ok">${icon("check", "sm")} ${c.health ? esc(c.health) : "running"}</span>`;
  if (c.state === "gone") return `<span class="badge outline" title="No longer running; history only">gone</span>`;
  return `<span class="badge outline">${esc(c.state || "stopped")}</span>`;
}

function rangeSeg() {
  return `<div class="seg" role="group" aria-label="Time range">${RANGES.map((r) => `<button type="button" data-action="range" data-range="${r}" class="${r === range ? "active" : ""}" aria-pressed="${r === range}">${r}</button>`).join("")}</div>`;
}

function memLimitOf(c, host) { return c.latest && c.latest.mem_limited ? c.latest.mem_limit : host.mem_total; }

const SORTS = {
  name: (c) => c.name.toLowerCase(),
  cpu: (c) => c.latest?.cpu ?? -1,
  mem: (c) => c.latest?.mem ?? -1,
  net: (c) => (c.latest ? c.latest.net_rx + c.latest.net_tx : -1),
  blk: (c) => (c.latest ? c.latest.blk_r + c.latest.blk_w : -1),
  pids: (c) => c.latest?.pids ?? -1,
};

function sorted(list) {
  const f = SORTS[sort] || SORTS.cpu, dir = sortDir === "asc" ? 1 : -1;
  return [...list].sort((a, b) => { const x = f(a), y = f(b); return (x < y ? -1 : x > y ? 1 : 0) * dir || a.name.localeCompare(b.name); });
}

function th(key, label, cls = "") {
  const on = sort === key;
  return `<th class="sortable ${cls}" data-action="sort" data-sort="${key}" aria-sort="${on ? (sortDir === "asc" ? "ascending" : "descending") : "none"}">${label}${on ? ` ${icon(sortDir === "asc" ? "chevron-up" : "chevron-down", "sm")}` : ""}</th>`;
}

function level(v, warn, bad) { return v == null ? "" : v >= bad ? "danger" : v >= warn ? "warn" : ""; }

// --------------------------------------------------------------- disks
function forecastBadge(d) {
  if (d.days_to_full != null) {
    const cls = d.days_to_full < 7 ? "danger" : d.days_to_full < 30 ? "warn" : "outline";
    return `<span class="badge ${cls}" title="Linear trend over the last 7 days">full in ~${d.days_to_full < 1 ? "<1" : Math.round(d.days_to_full)} days</span>`;
  }
  if (d.growth_per_day != null) return `<span class="badge outline">${d.growth_per_day <= 0 ? "not growing" : "stable"}</span>`;
  return `<span class="muted small" title="Needs at least 6 hours of samples">collecting trend…</span>`;
}

function disksCard(disks) {
  if (!disks.length) return "";
  return `<div class="card mb"><div class="card-h"><h2>${icon("hard-drive", "sm")} Disks</h2><span class="muted small">filesystems holding Dockbare's data and backups${disks.length > 1 ? " and extra paths" : ""}</span></div>
    <div class="card-c tight"><div class="table-wrap"><table class="metrics-table"><thead><tr><th>Filesystem</th><th>Used</th><th class="num">Free</th><th class="num">Growth / day</th><th>Forecast</th></tr></thead><tbody>
    ${disks.map((d) => `<tr><td><b>${esc(d.name)}</b><div class="muted small mono">${esc(d.mount)}</div></td>
      <td class="mem-cell"><div class="flex small"><span>${esc(fmtBytes(d.used))} of ${esc(fmtBytes(d.total))}</span><span class="muted">${pct(d.pct, 0)}</span></div><div class="mbar"><i class="${d.pct >= 90 ? "bad" : d.pct >= 80 ? "warn" : ""}" data-w="${d.pct}"></i></div></td>
      <td class="num">${esc(fmtBytes(d.free))}</td>
      <td class="num">${d.growth_per_day == null ? "—" : `${d.growth_per_day < 0 ? "−" : "+"}${esc(fmtBytes(Math.abs(d.growth_per_day)))}`}</td>
      <td>${forecastBadge(d)}</td></tr>`).join("")}
    </tbody></table></div></div></div>`;
}

// -------------------------------------------------------------- alerts
let alertMeta = null;
async function loadAlertMeta() { if (!alertMeta) alertMeta = await api.get("/api/alerts"); return alertMeta; }

function ruleText(r, metrics) {
  const m = metrics[r.metric] || { label: r.metric, unit: "" };
  const unit = m.unit === "%" ? "%" : m.unit ? ` ${m.unit}` : "";
  const who = m.scope === "host" ? "" : r.target === "*" ? (m.scope === "disk" ? " · any disk" : " · any container") : ` · ${r.target}`;
  const pat = r.metric === "container.log_matches" ? ` /${r.pattern}/ in ${r.window_minutes} min` : "";
  return `${m.label}${pat} ${r.op} ${r.threshold}${unit}${r.for_minutes ? ` for ${r.for_minutes} min` : ""}${who}`;
}

function alertBadge(st) {
  const map = { firing: ["danger", "alert", "firing"], pending: ["warn", "clock", "pending"], ok: ["ok", "check", "ok"], disabled: ["outline", "pause", "off"] };
  const [cls, ic, label] = map[st] || ["outline", "info", st];
  return `<span class="badge ${cls}">${icon(ic, "sm")} ${label}</span>`;
}

function alertsCard(rules, metrics) {
  return `<div class="card mb"><div class="card-h"><h2>${icon("bell", "sm")} Alerts</h2><span class="muted small">notified through the channels in Settings (event "Resource and log alerts")</span><div class="actions"><button class="btn btn-sm" data-action="addRule">${icon("plus")} Add rule</button></div></div>
    <div class="card-c tight">${rules.length ? `<div class="table-wrap"><table><thead><tr><th>Status</th><th>Rule</th><th>Now</th><th></th></tr></thead><tbody>
    ${rules.map((r) => `<tr class="${r.enabled ? "" : "muted"}"><td>${alertBadge(r.status)}</td>
      <td><b>${esc(r.name)}</b>${r.severity === "error" ? ` <span class="badge outline">error</span>` : ""}<div class="muted small">${esc(ruleText(r, metrics))}</div></td>
      <td class="small">${r.targets.length ? r.targets.map((t) => `<div><span class="dot ${t.status === "firing" ? "bad" : "warn"}"></span> ${t.target ? `${esc(t.target)}: ` : ""}<b>${num(t.value, t.value >= 100 ? 0 : 1)}${esc(r.unit === "%" ? "%" : r.unit ? " " + r.unit : "")}</b> <span class="muted">since ${esc(ago(t.since))}</span></div>`).join("") : `<span class="muted">—</span>`}</td>
      <td><div class="actions"><label class="check" title="${r.enabled ? "Disable" : "Enable"}"><span class="switch"><input type="checkbox" data-action="toggleRule" data-id="${esc(r.id)}" ${r.enabled ? "checked" : ""}><span class="track"></span></span></label><button class="btn btn-sm btn-ghost btn-icon" data-action="editRule" data-id="${esc(r.id)}" title="Edit">${icon("pencil")}</button><button class="btn btn-sm btn-danger-ghost btn-icon" data-action="delRule" data-id="${esc(r.id)}" title="Delete">${icon("trash")}</button></div></td></tr>`).join("")}
    </tbody></table></div>` : emptyState("No alert rules. Add one to get notified about CPU, memory, disk space or log messages.", "bell")}</div></div>`;
}

function ruleDialog(existing, meta, names) {
  const r = existing || { ...meta.defaults };
  const groups = { host: "Host", disk: "Disks", container: "Containers" };
  const opts = Object.entries(groups).map(([scope, label]) => `<optgroup label="${label}">${Object.entries(meta.metrics).filter(([, m]) => m.scope === scope).map(([k, m]) => `<option value="${k}" ${k === r.metric ? "selected" : ""}>${esc(m.label)}</option>`).join("")}</optgroup>`).join("");
  const body = `
    <div class="form-row"><div><label class="field">Name</label><input type="text" name="name" value="${esc(r.name || "")}" maxlength="80" placeholder="Shown in notifications"></div>
      <div><label class="field">Metric</label><select name="metric">${opts}</select></div></div>
    <div class="form-group mt" data-for="target"><label class="field">Applies to</label><input type="text" name="target" value="${esc(r.target ?? "*")}" list="rule-targets" placeholder="*"><datalist id="rule-targets"><option value="*">${names.map((n) => `<option value="${esc(n)}">`).join("")}</datalist><div class="help">A container or disk name, or <code>*</code> for each of them separately.</div></div>
    <div data-for="log"><div class="form-row mt"><div><label class="field">Pattern (regular expression)</label><input type="text" name="pattern" value="${esc(r.pattern || "")}" placeholder="panic|fatal|out of memory"></div><div><label class="field">Count over (minutes)</label><input type="number" name="window_minutes" min="1" max="1440" value="${r.window_minutes}"></div></div>
      <div class="mt">${switchHtml("ignore_case", r.ignore_case, "Ignore upper/lower case")}</div></div>
    <div class="form-row cols-3 mt"><div><label class="field">Condition</label><select name="op"><option value=">" ${r.op === ">" ? "selected" : ""}>above</option><option value="<" ${r.op === "<" ? "selected" : ""}>below</option></select></div>
      <div><label class="field">Threshold <span class="muted" data-unit></span></label><input type="number" name="threshold" step="any" value="${r.threshold}"></div>
      <div><label class="field">For (minutes)</label><input type="number" name="for_minutes" min="0" max="1440" value="${r.for_minutes}"><div class="help">0 = at the first sample.</div></div></div>
    <div class="form-row cols-3 mt"><div><label class="field">Severity</label><select name="severity"><option value="warning" ${r.severity !== "error" ? "selected" : ""}>warning</option><option value="error" ${r.severity === "error" ? "selected" : ""}>error</option></select></div>
      <div><label class="field">Repeat every (minutes)</label><input type="number" name="repeat_minutes" min="0" max="10080" value="${r.repeat_minutes}"><div class="help">While firing. 0 = once.</div></div>
      <div class="stack gap-2"><label class="field">Options</label>${switchHtml("notify_resolved", r.notify_resolved, "Notify when resolved")}${switchHtml("enabled", r.enabled, "Enabled")}</div></div>`;
  return new Promise((resolve) => {
    const d = modal({ title: existing ? `Alert rule: ${existing.name}` : "Add alert rule", body, size: "wide", footer: `<button class="btn" data-close>Cancel</button><button class="btn btn-primary" data-action="save">${icon("check")} Save</button>`, onClose: () => resolve(false) });
    const sync = () => {
      const m = meta.metrics[$("[name=metric]", d.el).value] || {};
      $("[data-for=target]", d.el).classList.toggle("hidden", m.scope === "host");
      $("[data-for=log]", d.el).classList.toggle("hidden", $("[name=metric]", d.el).value !== "container.log_matches");
      $("[data-unit]", d.el).textContent = m.unit ? `(${m.unit})` : "";
    };
    $("[name=metric]", d.el).addEventListener("change", () => {
      const m = meta.metrics[$("[name=metric]", d.el).value];
      if (m) $("[name=op]", d.el).value = m.op;
      if (m && !existing) $("[name=threshold]", d.el).value = m.default;
      sync();
    });
    sync();
    bind(d.el, {
      save: async (b) => { await busy(b, async () => { const v = formValues(d.el); existing ? await api.put(`/api/alerts/${existing.id}`, v) : await api.post("/api/alerts", v); toast("Alert rule saved", "success"); resolve(true); d.close(); }).catch((e) => toast(e.message, "error")); },
    });
  });
}

// ------------------------------------------------------------ settings
function settingsCard(s, host) {
  return `<div class="card mt"><div class="card-h"><h2>${icon("settings", "sm")} Collection</h2><span class="muted small">raw samples 24 h · 5-minute averages 7 days · hourly averages 90 days</span></div>
    <div class="card-c"><div class="form-row" data-settings>
      <div class="stack gap-2"><label class="field">Metrics</label>${switchHtml("enabled", s.enabled, "Collect host and container metrics")}${switchHtml("public_host", s.public_host, "Show CPU, memory and disk on the public status page")}${switchHtml("notify_logs", s.notify_logs, "Put the last log lines of a crashed container into its \"down\" notification")}<div class="help">Off by default: logs can contain secrets, and notifications leave this server (e.g. ntfy.sh).</div></div>
      <div><label class="field">Sample every (seconds)</label><input type="number" name="interval_seconds" min="10" max="300" step="1" value="${s.interval_seconds}"><div class="help">10–300. Each sample is one <code>/stats</code> call per running container.</div></div>
    </div>
    ${host.network ? "" : `<div class="help mt">${icon("info", "sm")} Host network traffic needs the host's <code>/proc</code>: add <code>- /proc:/host/proc:ro</code> to the volumes in your compose file. Until then the network figures are the sum of the containers.</div>`}
    <div class="mt"><button class="btn btn-primary" data-action="saveSettings">${icon("check")} Save</button></div></div></div>`;
}

async function saveSettings(btn, el, after) {
  await busy(btn, async () => {
    await api.put("/api/metrics/settings", formValues($("[data-settings]", el)));
    toast("Metrics settings saved", "success");
    after();
  }).catch((e) => toast(e.message, "error"));
}

// ------------------------------------------------------------ overview
async function renderOverview(page, el, live = () => true) {
  const [d, meta] = await Promise.all([api.get(`/api/metrics?range=${range}`), loadAlertMeta().catch(() => null)]);
  if (!live()) return;
  page.pollMs = Math.max(10, d.interval_seconds) * 1000;
  const h = d.host, hn = d.host_now, cur = d.current;
  const cores = h.cpus || 1;
  const rows = sorted(d.containers);
  const worst = d.disks.reduce((a, b) => (!a || (b.pct || 0) > (a.pct || 0) ? b : a), null);
  const soonest = d.disks.filter((x) => x.days_to_full != null).sort((a, b) => a.days_to_full - b.days_to_full)[0];
  const netRx = hn && hn.net_rx != null ? hn.net_rx : cur.net_rx, netTx = hn && hn.net_tx != null ? hn.net_tx : cur.net_tx;
  const hostNet = !!(hn && hn.net_rx != null);
  const memPct = hn && hn.mem_total ? (hn.mem_used / hn.mem_total) * 100 : null;
  const banner = !d.enabled ? `<div class="banner warn mb">${icon("pause")}<div>Metrics collection is off. Turn it on under Collection below.</div></div>`
    : d.error ? `<div class="banner danger mb">${icon("alert")}<div>Docker stats not readable: ${esc(d.error)}</div></div>`
    : !hn ? `<div class="banner info mb">${icon("loader", "spin")}<div>Collecting the first samples. Values show up after two sampling rounds (about ${d.interval_seconds * 2} s).</div></div>` : "";
  const firing = d.alerts.filter((a) => a.status === "firing");
  el.innerHTML = `${banner}
    ${firing.length ? `<div class="banner danger mb">${icon("bell")}<div><b>${firing.length} alert${firing.length === 1 ? "" : "s"} firing:</b> ${firing.map((a) => esc(a.name)).join(", ")}</div></div>` : ""}
    <div class="grid cols-5 mb">
      <div class="card stat"><div class="label">${icon("cpu", "sm")} CPU</div><div class="value ${level(hn?.cpu, 80, 95)}">${hn ? pct(hn.cpu) : "—"}</div><div class="sub">${hn && hn.load1 != null ? `load ${num(hn.load1, 2)} · ` : ""}${cores} cores · containers ${pct(cur.cpu / cores)}</div></div>
      <div class="card stat"><div class="label">${icon("layers", "sm")} Memory</div><div class="value ${level(memPct, 85, 95)}">${memPct == null ? "—" : pct(memPct)}</div><div class="sub">${hn ? `${esc(fmtBytes(hn.mem_used))} of ${esc(fmtBytes(hn.mem_total))}` : ""}${hn && hn.swap_total ? ` · swap ${pct((hn.swap_used / hn.swap_total) * 100, 0)}` : ""}<br>containers ${esc(fmtBytes(cur.mem))}</div></div>
      <div class="card stat"><div class="label">${icon("hard-drive", "sm")} Disk</div><div class="value ${level(worst?.pct, 80, 90)}">${worst ? pct(worst.pct, 0) : "—"}</div><div class="sub">${worst ? `${esc(worst.name)} · ${esc(fmtBytes(worst.free))} free` : "no data yet"}${soonest ? `<br><span class="${soonest.days_to_full < 7 ? "danger" : ""}">full in ~${Math.max(1, Math.round(soonest.days_to_full))} days</span>` : ""}</div></div>
      <div class="card stat"><div class="label">${icon("globe", "sm")} Network${hostNet ? "" : " (containers)"}</div><div class="value">${esc(rate(netRx + netTx))}</div><div class="sub">↓ ${esc(rate(netRx))} · ↑ ${esc(rate(netTx))}${hn && hn.disk_r != null ? `<br>disk ${esc(rateShort(hn.disk_r))} r · ${esc(rateShort(hn.disk_w))} w /s` : ""}</div></div>
      ${hn && hn.temp != null
        ? `<div class="card stat"><div class="label">${icon("zap", "sm")} Temperature</div><div class="value ${level(hn.temp, 75, 90)}">${Math.round(hn.temp)} °C</div><div class="sub">hottest sensor · ${cur.running} containers</div></div>`
        : `<div class="card stat"><div class="label">${icon("container", "sm")} Containers</div><div class="value">${cur.running}</div><div class="sub">running and sampled</div></div>`}
    </div>
    <div class="card mb"><div class="card-h"><h2>Host</h2><span class="muted small">${esc(h.name || "")}${h.os ? ` · ${esc(h.os)}` : ""}${d.last_collect ? ` · sampled ${esc(ago(d.last_collect))}` : ""}</span><div class="actions">${d.backups.length ? legendHtml([{ label: "Backup run", cls: "bk" }]) : ""}${rangeSeg()}</div></div>
      <div class="card-c"><div class="grid cols-2">
        ${[["cpu", "CPU"], ["mem", "Memory"], ["io", "Disk I/O"], ["net", hostNet ? "Network" : "Network (containers)"]].map(([k, t]) => `<div><div class="chart-h flex"><b>${t}</b><span class="grow"></span><span data-legend="${k}"></span></div><div data-chart="${k}"></div></div>`).join("")}
      </div></div></div>
    ${disksCard(d.disks)}
    ${meta ? alertsCard(d.alerts, meta.metrics) : ""}
    <div class="card"><div class="card-h"><h2>Containers</h2><span class="muted small">click a row for its charts and logs</span></div>
      <div class="card-c tight">${rows.length ? `<div class="table-wrap"><table class="metrics-table"><thead><tr>${th("name", "Container")}${th("cpu", "CPU", "num")}${th("mem", "Memory")}${th("net", "Net ↓ / ↑", "num")}${th("blk", "Disk r / w", "num")}${th("pids", "PIDs", "num")}<th>CPU ${esc(range)}</th><th>Status</th></tr></thead><tbody>
        ${rows.map((c) => { const l = c.latest, lim = memLimitOf(c, h), mp = l && lim ? (l.mem / lim) * 100 : 0;
          return `<tr class="clickable" tabindex="0" data-action="open" data-name="${esc(c.name)}">
            <td><b>${esc(c.name)}</b>${c.project ? `<div class="muted small">${esc(c.project)}${c.service && c.service !== c.name ? ` · ${esc(c.service)}` : ""}</div>` : ""}</td>
            <td class="num">${l ? pct(l.cpu) : "—"}</td>
            <td class="mem-cell">${l ? `<div class="flex small"><span>${esc(fmtBytes(l.mem))}</span><span class="muted">${l.mem_limited ? `/ ${esc(fmtBytes(l.mem_limit))}` : ""}</span></div><div class="mbar"><i class="${mp >= 90 ? "bad" : mp >= 75 ? "warn" : ""}" data-w="${mp.toFixed(1)}"></i></div>` : "—"}</td>
            <td class="num small">${l ? `${esc(rateShort(l.net_rx))} / ${esc(rateShort(l.net_tx))}` : "—"}</td>
            <td class="num small">${l ? `${esc(rateShort(l.blk_r))} / ${esc(rateShort(l.blk_w))}` : "—"}</td>
            <td class="num">${l ? l.pids : "—"}</td>
            <td>${miniSpark(c.spark.map((p) => p[1]))}</td>
            <td>${stateBadge(c)}</td></tr>`; }).join("")}
        </tbody></table></div>` : emptyState(d.enabled ? "No samples yet. Running containers appear here after the first two sampling rounds." : "Metrics collection is off.", "cpu")}</div></div>
    ${settingsCard(d.settings, h)}`;
  applyWidths(el);
  const base = { since: d.since, now: d.now, bucket: d.bucket, marks: d.backups };
  const H = (k) => d.host_series.map((r) => [r.t, r[k]]);
  const T = (k, f = (v) => v) => d.totals.map((t) => [t.t, f(t[k])]);
  const charts = {
    cpu: { fmt: pctFmt, minMax: 1, max: 100, series: [{ label: "Host", cls: "s1", area: true, points: H("cpu") }, ...(range === "1h" || range === "24h" ? [] : [{ label: "Host peak", cls: "s1", dashed: true, points: H("cpu_max") }]), { label: "Containers", cls: "s2", dashed: true, points: T("cpu", (v) => v / cores) }] },
    mem: { fmt: bytesAxis, minMax: 1024 ** 2, series: [{ label: "Host used", cls: "s1", area: true, points: H("mem_used") }, { label: "Containers", cls: "s2", dashed: true, points: T("mem") }] },
    io: { fmt: (v) => rateShort(v), minMax: 1024, series: [{ label: "Read", cls: "s1", points: H("disk_r") }, { label: "Write", cls: "s3", dashed: true, points: H("disk_w") }] },
    net: hostNet
      ? { fmt: (v) => rateShort(v), minMax: 1024, series: [{ label: "Received", cls: "s1", points: H("net_rx") }, { label: "Sent", cls: "s3", dashed: true, points: H("net_tx") }] }
      : { fmt: (v) => rateShort(v), minMax: 1024, series: [{ label: "Received", cls: "s1", points: T("net_rx") }, { label: "Sent", cls: "s3", dashed: true, points: T("net_tx") }] },
  };
  for (const [k, c] of Object.entries(charts)) {
    $(`[data-legend=${k}]`, el).innerHTML = legendHtml(c.series);
    lineChart($(`[data-chart=${k}]`, el), { ...base, ...c });
  }
  const names = [...d.containers.map((c) => c.name), ...d.disks.map((x) => x.name)];
  const rerender = () => renderOverview(page, el);
  bind(el, {
    range: (b) => { range = b.dataset.range; store.set("range", range); rerender(); },
    sort: (b) => { const k = b.dataset.sort; if (sort === k) sortDir = sortDir === "asc" ? "desc" : "asc"; else { sort = k; sortDir = k === "name" ? "asc" : "desc"; } store.set("sort", sort); store.set("dir", sortDir); rerender(); },
    open: (r) => { location.hash = `#system/${encodeURIComponent(r.dataset.name)}`; },
    saveSettings: (b) => saveSettings(b, el, rerender),
    addRule: () => ruleDialog(null, meta, names).then((ok) => ok && rerender()),
    editRule: (b) => ruleDialog(d.alerts.find((r) => r.id === b.dataset.id), meta, names).then((ok) => ok && rerender()),
    delRule: async (b) => {
      const r = d.alerts.find((x) => x.id === b.dataset.id);
      if (!(await confirmDialog({ title: "Delete alert rule", text: `Delete <b>${esc(r.name)}</b>? A firing alert is dropped without a "resolved" message.`, confirmLabel: "Delete", danger: true }))) return;
      try { await api.del(`/api/alerts/${r.id}`); rerender(); } catch (e) { toast(e.message, "error"); }
    },
    toggleRule: async (sw) => { try { await api.put(`/api/alerts/${sw.dataset.id}`, { enabled: sw.checked }); rerender(); } catch (e) { toast(e.message, "error"); sw.checked = !sw.checked; } },
  });
  $$("tr.clickable", el).forEach((tr) => tr.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); tr.click(); } }));
}

// ---------------------------------------------------------------- logs
const LEVEL_RE = { error: /\b(error|err|fatal|panic|crit(ical)?|exception|emerg|alert)\b/i, warn: /\b(warn(ing)?)\b/i };
const MAX_LINES = 3000;
const logState = { name: null, lines: [], lastTs: null, filter: "", problems: false, follow: true, timer: null, error: null };

function lineLevel(t) { return LEVEL_RE.error.test(t) ? "error" : LEVEL_RE.warn.test(t) ? "warn" : ""; }

function logMatcher() {
  const f = logState.filter.trim();
  if (!f) return null;
  const m = f.match(/^\/(.+)\/([a-z]*)$/);
  if (m) { try { return new RegExp(m[1], m[2].includes("i") ? m[2] : m[2] + "i"); } catch { return null; } }
  const low = f.toLowerCase();
  return { test: (t) => t.toLowerCase().includes(low) };
}

function renderLogLines(box) {
  const out = $("[data-loglines]", box);
  if (!out) return;
  const rx = logMatcher();
  const shown = logState.lines.filter(([, t]) => (!logState.problems || lineLevel(t)) && (!rx || rx.test(t)));
  const atBottom = out.scrollHeight - out.scrollTop - out.clientHeight < 40;
  out.innerHTML = shown.length ? shown.slice(-1500).map(([ts, t]) => `<div class="ll ${lineLevel(t)}"><span class="lt">${esc(new Date(ts * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" }))}</span>${esc(t)}</div>`).join("")
    : `<div class="muted">${logState.error ? esc(logState.error) : logState.lines.length ? "No lines match the filter." : "No log lines."}</div>`;
  $("[data-logcount]", box).textContent = `${shown.length} of ${logState.lines.length} lines`;
  if (logState.follow && (atBottom || !out.dataset.scrolled)) { out.scrollTop = out.scrollHeight; out.dataset.scrolled = "1"; }
}

async function fetchLogs(box, initial) {
  const name = logState.name;
  try {
    const q = initial ? "tail=1000" : `tail=1000&since=${logState.lastTs ?? 0}`;
    const r = await api.get(`/api/logs/${encodeURIComponent(name)}?${q}`);
    if (name !== logState.name) return;
    logState.error = null;
    if (initial) logState.lines = r.lines; else logState.lines.push(...r.lines);
    if (logState.lines.length > MAX_LINES) logState.lines = logState.lines.slice(-MAX_LINES);
    if (r.last_ts) logState.lastTs = r.last_ts;
  } catch (e) { logState.error = `Could not read logs: ${e.message}`; }
  renderLogLines(box);
}

function stopFollow() { clearInterval(logState.timer); logState.timer = null; }

function startFollow(box) {
  stopFollow();
  if (!logState.follow) return;
  logState.timer = setInterval(() => {
    if (!document.body.contains(box) || logState.name == null) { stopFollow(); return; }
    if (document.visibilityState === "visible") fetchLogs(box, false);
  }, 3000);
}

function logsCard(name) {
  return `<div class="card mt" data-logs="${esc(name)}"><div class="card-h"><h2>${icon("terminal", "sm")} Logs</h2><span class="muted small" data-logcount></span>
      <div class="actions"><div class="search"><input type="search" data-logfilter placeholder="Filter, or /regex/" value="${esc(logState.filter)}" aria-label="Filter log lines"></div>
      ${switchHtml("problems", logState.problems, "Errors and warnings", 'data-logproblems')}${switchHtml("follow", logState.follow, "Follow", 'data-logfollow')}
      <a class="btn btn-sm" href="/api/logs/${encodeURIComponent(name)}?download=1&amp;tail=20000" download>${icon("download")} Download</a></div></div>
    <div class="card-c tight"><div class="logview" data-loglines><div class="muted">${icon("loader", "spin")} Loading…</div></div></div></div>`;
}

function mountLogs(el, name) {
  const box = $(`[data-logs]`, el);
  if (logState.name !== name) Object.assign(logState, { name, lines: [], lastTs: null, error: null });
  const filter = $("[data-logfilter]", box);
  let t = null;
  filter.addEventListener("input", () => { clearTimeout(t); t = setTimeout(() => { logState.filter = filter.value; renderLogLines(box); }, 150); });
  filter.addEventListener("keydown", (e) => { if (e.key === "Escape") { filter.value = ""; logState.filter = ""; renderLogLines(box); filter.blur(); } });
  $("[data-logproblems]", box).addEventListener("change", (e) => { logState.problems = e.target.checked; renderLogLines(box); });
  $("[data-logfollow]", box).addEventListener("change", (e) => { logState.follow = e.target.checked; if (logState.follow) { fetchLogs(box, false); startFollow(box); } else stopFollow(); });
  fetchLogs(box, true).then(() => startFollow(box));
}

// ----------------------------------------------------------- container
async function renderContainer(page, el, name, live = () => true) {
  let d;
  try { d = await api.get(`/api/metrics/containers/${encodeURIComponent(name)}?range=${range}`); }
  catch (e) {
    if (e.status !== 404) throw e;
    if (!live()) return;
    d = null;
  }
  if (!live()) return;
  page.pollMs = 30000;
  // Keep the log viewer (scroll position, filter, follow timer) across background refreshes.
  const keepLogs = $(`[data-logs]`, el)?.dataset.logs === name;
  if (!keepLogs) {
    stopFollow();
    el.innerHTML = `<div data-top></div>${logsCard(name)}`;
    mountLogs(el, name);
  }
  const top = $("[data-top]", el);
  if (!d) {
    top.innerHTML = `<div class="flex mb flex-wrap"><a class="btn btn-sm" href="#system">${icon("arrow-right", "sm flip")} All containers</a><h2 class="grow">${esc(name)}</h2></div>${emptyState(`No metrics for '${name}' in the last ${range}. Logs are below if the container exists.`, "cpu")}`;
    return;
  }
  const l = d.latest, h = d.host, s = d.series;
  const limited = l && h.mem_total && l.mem_limit && l.mem_limit < h.mem_total;
  const limit = limited ? l.mem_limit : null;
  const base = { since: d.since, now: d.now, bucket: d.bucket, marks: d.backups };
  top.innerHTML = `
    <div class="flex mb flex-wrap"><a class="btn btn-sm" href="#system">${icon("arrow-right", "sm flip")} All containers</a><h2 class="grow">${esc(d.name)}</h2>${stateBadge(d)}${d.image ? `<span class="tag">${esc(d.image)}</span>` : ""}${rangeSeg()}</div>
    <div class="grid cols-4 mb">
      <div class="card stat"><div class="label">${icon("cpu", "sm")} CPU</div><div class="value">${l ? pct(l.cpu) : "—"}</div><div class="sub">100% = one core · host has ${h.cpus || "?"}</div></div>
      <div class="card stat"><div class="label">${icon("layers", "sm")} Memory</div><div class="value">${l ? esc(fmtBytes(l.mem)) : "—"}</div><div class="sub">${!l ? "" : limited ? `limit ${esc(fmtBytes(l.mem_limit))} · ${pct((l.mem / l.mem_limit) * 100)}` : "no limit set"}</div></div>
      <div class="card stat"><div class="label">${icon("globe", "sm")} Network</div><div class="value">${l ? esc(rate(l.net_rx + l.net_tx)) : "—"}</div><div class="sub">${l ? `↓ ${esc(rate(l.net_rx))} · ↑ ${esc(rate(l.net_tx))}` : d.state === "running" ? "waiting for the next sample" : "not running"}</div></div>
      <div class="card stat"><div class="label">${icon("activity", "sm")} Processes</div><div class="value">${l ? l.pids : "—"}</div><div class="sub">${l ? `disk ${esc(rateShort(l.blk_r))} r · ${esc(rateShort(l.blk_w))} w /s` : ""}</div></div>
    </div>
    <div class="grid cols-2">
      ${[["cpu", "CPU"], ["mem", "Memory"], ["net", "Network"], ["blk", "Disk I/O"]].map(([k, t]) => `<div class="card"><div class="card-h"><h2>${t}</h2><div class="actions" data-legend="${k}"></div></div><div class="card-c"><div data-chart="${k}"></div></div></div>`).join("")}
    </div>`;
  const P = (key) => s.map((r) => [r.t, r[key]]);
  const charts = {
    cpu: { fmt: pctFmt, minMax: 1, series: [{ label: "Average", cls: "s1", area: true, points: P("cpu") }, { label: "Peak", cls: "s1", dashed: true, points: P("cpu_max") }] },
    mem: { fmt: bytesAxis, minMax: 1024 ** 2, series: [{ label: "Average", cls: "s2", area: true, points: P("mem") }, { label: "Peak", cls: "s2", dashed: true, points: P("mem_max") }, ...(limit ? [{ label: "Limit", cls: "lim", dashed: true, points: s.map((r) => [r.t, r.mem_limit]) }] : [])] },
    net: { fmt: (v) => rateShort(v), minMax: 1024, series: [{ label: "Received", cls: "s1", points: P("net_rx") }, { label: "Sent", cls: "s3", dashed: true, points: P("net_tx") }] },
    blk: { fmt: (v) => rateShort(v), minMax: 1024, series: [{ label: "Read", cls: "s1", points: P("blk_r") }, { label: "Write", cls: "s3", dashed: true, points: P("blk_w") }] },
  };
  for (const [k, c] of Object.entries(charts)) {
    $(`[data-legend=${k}]`, top).innerHTML = legendHtml(d.backups.length ? [...c.series, { label: "Backup run", cls: "bk" }] : c.series);
    lineChart($(`[data-chart=${k}]`, top), { ...base, ...c });
  }
  bind(top, { range: (b) => { range = b.dataset.range; store.set("range", range); renderContainer(page, el, name); } });
}

export const system = {
  title: "System",
  pollMs: 30000,
  async render(el) {
    // The container name comes from the hash (#system/<name>) rather than the router argument,
    // so the periodic background refresh stays on the detail view.
    const sub = location.hash.replace(/^#\/?/, "").split("/")[1];
    const seq = (this._seq = (this._seq || 0) + 1);
    const live = () => seq === this._seq;
    if (sub) await renderContainer(this, el, decodeURIComponent(sub), live);
    else { stopFollow(); logState.name = null; await renderOverview(this, el, live); }
  },
};
