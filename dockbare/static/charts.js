// Small SVG time-series charts (no library, CSP-safe: only element.style from JS, no inline style attributes).
import { esc } from "./ui.js";

const NS = "http://www.w3.org/2000/svg";
const PAD = { l: 52, r: 10, t: 10, b: 22 };

function niceMax(v) {
  if (!(v > 0)) return 1;
  const p = 10 ** Math.floor(Math.log10(v)), n = v / p;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * p;
}

function timeLabel(ts, span) {
  const d = new Date(ts * 1000);
  return span > 2 * 86400 ? d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) : d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

// Render a line chart into `el`.
// opts: { since, now, bucket, series: [{ label, cls, dashed, area, points: [[ts, value|null], ...] }],
//         minMax?: smallest axis maximum (keeps all-zero charts readable),
//         fmt: (v, axisMax?) => string (axisMax is set for axis labels, not tooltips), max?: number, marks?: [{ start, end, ok, volume }], height? }
export function lineChart(el, opts) {
  el.classList.add("chart");
  el._chart = opts;
  draw(el);
  if (!el._ro && typeof ResizeObserver !== "undefined") {
    let w = el.clientWidth;
    el._ro = new ResizeObserver(() => {
      if (!el.isConnected) { el._ro.disconnect(); return; }  // chart was replaced by a re-render
      if (Math.abs(el.clientWidth - w) > 4) { w = el.clientWidth; draw(el); }
    });
    el._ro.observe(el);
  }
}

function draw(el) {
  const o = el._chart;
  const W = Math.max(240, el.clientWidth || 600), H = o.height || 170;
  const iw = W - PAD.l - PAD.r, ih = H - PAD.t - PAD.b;
  const span = Math.max(1, o.now - o.since);
  const x = (t) => PAD.l + ((t - o.since) / span) * iw;
  const all = o.series.flatMap((s) => s.points.map((p) => p[1])).filter((v) => v != null);
  const ymax = o.max ?? niceMax(Math.max(o.minMax || 0, ...all) * 1.08);
  const y = (v) => PAD.t + ih - (Math.min(v, ymax) / ymax) * ih;
  const gap = (o.bucket || 60) * 2.5;
  const parts = [];
  for (const m of o.marks || []) {
    const x0 = x(Math.max(m.start, o.since)), x1 = Math.max(x0 + 2, x(Math.min(m.end || o.now, o.now)));
    parts.push(`<rect class="bk-band ${m.ok === false ? "bad" : ""}" x="${x0.toFixed(1)}" y="${PAD.t}" width="${(x1 - x0).toFixed(1)}" height="${ih}"><title>${esc(`Backup ${m.volume || ""}${m.ok === false ? " (failed)" : m.end ? "" : " (running)"}`)}</title></rect>`);
  }
  for (let i = 0; i <= 3; i++) {
    const v = (ymax / 3) * i, yy = y(v).toFixed(1);
    parts.push(`<line class="grid-l" x1="${PAD.l}" x2="${W - PAD.r}" y1="${yy}" y2="${yy}"/>${all.length ? `<text class="ax" x="${PAD.l - 6}" y="${(+yy + 4).toFixed(1)}" text-anchor="end">${esc(o.fmt(v, ymax))}</text>` : ""}`);
  }
  const ticks = Math.max(2, Math.min(6, Math.floor(iw / 90)));
  for (let i = 0; i <= ticks; i++) {
    const t = o.since + (span / ticks) * i;
    parts.push(`<text class="ax" x="${x(t).toFixed(1)}" y="${H - 6}" text-anchor="${i === 0 ? "start" : i === ticks ? "end" : "middle"}">${esc(timeLabel(t, span))}</text>`);
  }
  o.series.forEach((s) => {
    let d = "", area = "", prev = null, segStart = null;
    for (const [t, v] of s.points) {
      if (v == null) { prev = null; continue; }
      const px = x(t).toFixed(1), py = y(v).toFixed(1);
      const brk = prev == null || t - prev > gap;
      if (brk && s.area && segStart != null) area += `L${segStart.end},${PAD.t + ih} L${segStart.x},${PAD.t + ih} Z `;
      if (brk) { d += `M${px},${py} `; area += `M${px},${PAD.t + ih} L${px},${py} `; segStart = { x: px, end: px }; }
      else { d += `L${px},${py} `; area += `L${px},${py} `; segStart.end = px; }
      prev = t;
    }
    if (s.area && segStart != null) area += `L${segStart.end},${PAD.t + ih} L${segStart.x},${PAD.t + ih} Z`;
    if (s.area && area) parts.push(`<path class="ar ${s.cls}" d="${area}"/>`);
    if (d) parts.push(`<path class="ln ${s.cls} ${s.dashed ? "dash" : ""}" d="${d}"/>`);
    // points with no neighbour inside the gap draw no line segment; show them as dots
    const pts = s.points.filter((p) => p[1] != null);
    pts.forEach(([t, v], i) => {
      const lone = (i === 0 || t - pts[i - 1][0] > gap) && (i === pts.length - 1 || pts[i + 1][0] - t > gap);
      if (lone && !s.dashed) parts.push(`<circle class="pt ${s.cls}" cx="${x(t).toFixed(1)}" cy="${y(v).toFixed(1)}" r="2.5"/>`);
    });
  });
  if (!all.length) parts.push(`<text class="ax" x="${PAD.l + iw / 2}" y="${PAD.t + ih / 2}" text-anchor="middle">No data in this range yet</text>`);
  parts.push(`<line class="cross" x1="0" x2="0" y1="${PAD.t}" y2="${PAD.t + ih}" visibility="hidden"/>`);
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="${esc(o.series.map((s) => s.label).join(", "))} over time">${parts.join("")}</svg><div class="chart-tip" hidden></div>`;
  const svg = el.querySelector("svg"), cross = svg.querySelector(".cross"), tip = el.querySelector(".chart-tip");
  const times = [...new Set(o.series.flatMap((s) => s.points.map((p) => p[0])))].sort((a, b) => a - b);
  if (!times.length) return;
  const lookup = o.series.map((s) => new Map(s.points.map((p) => [p[0], p[1]])));
  svg.addEventListener("mousemove", (e) => {
    const r = svg.getBoundingClientRect();
    const mx = ((e.clientX - r.left) / r.width) * W;
    const t = o.since + ((mx - PAD.l) / iw) * span;
    let best = times[0];
    for (const tt of times) if (Math.abs(tt - t) < Math.abs(best - t)) best = tt;
    if (Math.abs(best - t) > gap) { cross.setAttribute("visibility", "hidden"); tip.hidden = true; return; }
    const cx = x(best);
    cross.setAttribute("x1", cx); cross.setAttribute("x2", cx); cross.setAttribute("visibility", "visible");
    tip.innerHTML = `<div class="muted">${esc(new Date(best * 1000).toLocaleString(undefined, { dateStyle: "short", timeStyle: "short" }))}</div>` +
      o.series.map((s, i) => { const v = lookup[i].get(best); return `<div><span class="sw ${s.cls} ${s.dashed ? "dash" : ""}"></span>${esc(s.label)} <b>${v == null ? "—" : esc(o.fmt(v))}</b></div>`; }).join("");
    tip.hidden = false;
    const left = (cx / W) * r.width;
    tip.style.left = `${Math.min(Math.max(0, left + 12), r.width - tip.offsetWidth)}px`;
    tip.style.top = `${PAD.t}px`;
  });
  svg.addEventListener("mouseleave", () => { cross.setAttribute("visibility", "hidden"); tip.hidden = true; });
}

export function legendHtml(series) {
  return `<span class="chart-legend">${series.map((s) => `<span><span class="sw ${s.cls} ${s.dashed ? "dash" : ""}"></span>${esc(s.label)}</span>`).join("")}</span>`;
}

// Tiny inline trend line (no axes) for table rows.
export function miniSpark(values, { w = 90, h = 22, max } = {}) {
  const vs = values.filter((v) => v != null);
  if (vs.length < 2) return `<span class="muted small">—</span>`;
  const m = max ?? Math.max(...vs, 0.0001);
  const pts = values.map((v, i) => `${((i / (values.length - 1)) * (w - 2) + 1).toFixed(1)},${(h - 1 - (Math.min(v ?? 0, m) / m) * (h - 2)).toFixed(1)}`).join(" ");
  return `<svg class="mini-spark" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" aria-hidden="true"><polyline points="${pts}"/></svg>`;
}
