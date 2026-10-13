// Daily availability strip: one bar per day, status-coloured, with a per-bar tooltip.
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const dayLabel = (ts) => new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
const downLabel = (s) => (s < 60 ? `${s}s` : s < 3600 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`);

export function cls(pct) {
  if (pct == null) return "na";
  if (pct >= 99.9) return "ok";
  if (pct >= 97) return "warn";
  return "bad";
}

export function historyBars(history, { height = 24 } = {}) {
  const n = history.length || 1;
  const bw = 6, gap = 2, w = n * (bw + gap) - gap;
  const rects = history.map((h, i) => {
    const title = h.pct == null ? `${dayLabel(h.day)}\nNo data` : `${dayLabel(h.day)}\n${h.pct >= 99.995 ? "100" : h.pct.toFixed(2)}% available${h.down ? `\n${downLabel(h.down)} downtime` : ""}${h.kinds && h.kinds.length ? ` (${h.kinds.join(", ")})` : ""}`;
    return `<rect class="ub ub-${cls(h.pct)}" x="${i * (bw + gap)}" y="0" width="${bw}" height="${height}" rx="1.5" data-tip="${esc(title)}" aria-label="${esc(title.replace(/\n/g, ", "))}"></rect>`;
  }).join("");
  return `<svg class="ubars" viewBox="0 0 ${w} ${height}" preserveAspectRatio="none" role="img" aria-label="Daily availability, last ${n} days">${rects}</svg>`;
}

let tipEl = null;
export function enableBarTooltips() {
  if (tipEl) return;
  tipEl = document.createElement("div");
  tipEl.className = "st-tip";
  document.body.appendChild(tipEl);
  const move = (e) => {
    const x = Math.min(e.clientX + 14, window.innerWidth - tipEl.offsetWidth - 8);
    const y = e.clientY - tipEl.offsetHeight - 12;
    tipEl.style.left = `${Math.max(8, x)}px`;
    tipEl.style.top = `${y < 8 ? e.clientY + 18 : y}px`;
  };
  document.addEventListener("mouseover", (e) => { const t = e.target.closest?.("[data-tip]"); if (!t) return; tipEl.textContent = t.dataset.tip; tipEl.classList.add("show"); move(e); });
  document.addEventListener("mousemove", (e) => { if (tipEl.classList.contains("show")) move(e); });
  document.addEventListener("mouseout", (e) => { if (e.target.closest?.("[data-tip]")) tipEl.classList.remove("show"); });
}

export function combine(histories) {
  // Worst availability per day across several histories (null when none has data).
  const n = Math.max(0, ...histories.map((h) => h.length));
  const out = [];
  for (let i = 0; i < n; i++) {
    const vals = histories.map((h) => h[h.length - n + i]).filter((x) => x && x.pct != null);
    if (!vals.length) { const any = histories.find((h) => h[h.length - n + i]); out.push({ day: any ? any[any.length - n + i].day : 0, pct: null, down: 0, kinds: [] }); continue; }
    const worst = vals.reduce((a, b) => (b.pct < a.pct ? b : a));
    out.push({ day: worst.day, pct: worst.pct, down: Math.max(...vals.map((v) => v.down || 0)), kinds: [...new Set(vals.flatMap((v) => v.kinds || []))] });
  }
  return out;
}

export function legend() {
  return `<div class="ub-legend"><span title="99.9% or better"><i class="ub-dot ub-ok"></i> up</span><span title="below 99.9%"><i class="ub-dot ub-warn"></i> degraded</span><span title="below 97%"><i class="ub-dot ub-bad"></i> outage</span><span><i class="ub-dot ub-na"></i> no data</span></div>`;
}

export function avgPct(items, key = "d30") {
  const vals = items.map((i) => i.uptime && i.uptime[key]).filter((v) => typeof v === "number");
  if (!vals.length) return null;
  return vals.reduce((a, b) => a + b, 0) / vals.length;
}

export function axis(days) {
  return `<div class="ub-axis"><span>−${days} d</span><span>today</span></div>`;
}
