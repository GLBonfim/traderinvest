// Native SVG charts (no library). Each series is ONE <path> whatever the number of sessions
// (wicks, up bodies, down bodies, volume, every line), so a 5-year chart stays a few dozen
// elements. Gaps (null) break lines instead of being drawn as zero. Colours only describe
// candle direction (close vs open), never buy/sell.

import { formatCompact, formatDate, formatNumber } from "./format.js";

const NS = "http://www.w3.org/2000/svg"; // XML namespace identifier, never fetched
const PAD = { top: 10, right: 58, bottom: 22, left: 8 };

function s(tag, attrs = {}) {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, String(v));
  return el;
}

const finite = (v) => typeof v === "number" && Number.isFinite(v);

function extent(arrays) {
  let lo = Infinity;
  let hi = -Infinity;
  for (const a of arrays) for (const v of a) if (finite(v)) {
    if (v < lo) lo = v;
    if (v > hi) hi = v;
  }
  return lo === Infinity ? null : [lo, hi];
}

function niceTicks(lo, hi, count = 5) {
  const span = hi - lo || Math.abs(hi) || 1;
  const raw = span / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((x) => x >= raw) || raw;
  const out = [];
  for (let t = Math.ceil(lo / step) * step; t <= hi + step * 1e-9; t += step) out.push(+t.toFixed(10));
  return out;
}

function linePath(values, x, y) {
  let d = "";
  let pen = false;
  values.forEach((v, i) => {
    if (!finite(v)) {
      pen = false;
      return;
    }
    d += `${pen ? "L" : "M"}${x(i).toFixed(1)} ${y(v).toFixed(1)}`;
    pen = true;
  });
  return d;
}

function xLabels(sessions, x, top) {
  const g = s("g", { class: "axis axis-x" });
  const n = sessions.length;
  const k = Math.min(6, n);
  for (let j = 0; j < k; j++) {
    const i = Math.round((j * (n - 1)) / Math.max(1, k - 1));
    const t = s("text", { x: x(i), y: top, "text-anchor": j === 0 ? "start" : j === k - 1 ? "end" : "middle" });
    t.textContent = formatDate(sessions[i]);
    g.append(t);
  }
  return g;
}

function yAxis(ticks, y, width, fmt) {
  const g = s("g", { class: "axis axis-y" });
  for (const t of ticks) {
    g.append(s("line", { x1: PAD.left, x2: width - PAD.right, y1: y(t), y2: y(t), class: "grid" }));
    const label = s("text", { x: width - PAD.right + 6, y: y(t) + 4 });
    label.textContent = fmt(t);
    g.append(label);
  }
  return g;
}

// Shared frame: sizing to the container, crosshair, pointer and keyboard inspection.
function frame(container, { height, sessions, ariaLabel, draw, describe }) {
  const width = Math.max(280, Math.floor(container.clientWidth || 600));
  const n = sessions.length;
  const step = (width - PAD.left - PAD.right) / Math.max(1, n);
  const x = (i) => PAD.left + step * (i + 0.5);

  const svg = s("svg", {
    width,
    height,
    viewBox: `0 0 ${width} ${height}`,
    class: "chart-svg",
    role: "img",
    tabindex: "0",
    "aria-label": ariaLabel,
  });
  draw(svg, { width, height, x, step });

  const cross = s("line", { x1: 0, x2: 0, y1: PAD.top, y2: height - PAD.bottom, class: "crosshair" });
  svg.append(cross);
  const readout = document.createElement("div");
  readout.className = "chart-readout";
  readout.setAttribute("aria-live", "polite");

  let cursor = n - 1;
  const show = (i) => {
    cursor = Math.max(0, Math.min(n - 1, i));
    cross.setAttribute("x1", x(cursor));
    cross.setAttribute("x2", x(cursor));
    readout.textContent = describe(cursor);
  };
  svg.addEventListener("pointermove", (e) => {
    const r = svg.getBoundingClientRect();
    show(Math.floor((e.clientX - r.left - PAD.left) / step));
  });
  svg.addEventListener("pointerleave", () => show(n - 1));
  svg.addEventListener("keydown", (e) => {
    const moves = { ArrowLeft: -1, ArrowRight: 1, PageUp: -20, PageDown: 20 };
    if (e.key in moves) show(cursor + moves[e.key]);
    else if (e.key === "Home") show(0);
    else if (e.key === "End") show(n - 1);
    else return;
    e.preventDefault();
  });
  show(n - 1);
  container.replaceChildren(readout, svg);
}

function tooFew(container, sessions, what) {
  if (sessions.length >= 2) return false;
  const p = document.createElement("p");
  p.className = "chart-empty";
  p.textContent = `Not enough sessions to draw the ${what}.`;
  container.replaceChildren(p);
  return true;
}

export const OVERLAY_STYLES = {
  sma_20: { label: "SMA 20", cls: "ln-sma20" },
  sma_50: { label: "SMA 50", cls: "ln-sma50" },
  sma_200: { label: "SMA 200", cls: "ln-sma200" },
  bb_upper: { label: "Bollinger upper", cls: "ln-bb" },
  bb_middle: { label: "Bollinger middle", cls: "ln-bb ln-bb-mid" },
  bb_lower: { label: "Bollinger lower", cls: "ln-bb" },
};

// Candlesticks + volume, with the selected overlays (keys of data.overlays).
export function drawPriceChart(container, data, overlays = []) {
  const { sessions, open, high, low, close, volume } = data;
  if (tooFew(container, sessions, "price chart")) return;
  const height = 380;
  const volH = 70;
  const priceBottom = height - PAD.bottom - volH - 10;
  const shown = overlays.filter((k) => data.overlays[k]);
  const pr = extent([low, high, ...shown.map((k) => data.overlays[k])]);
  if (!pr) return tooFew(container, [], "price chart");
  const padY = (pr[1] - pr[0]) * 0.05 || 1;
  const [lo, hi] = [pr[0] - padY, pr[1] + padY];
  const y = (v) => PAD.top + ((hi - v) / (hi - lo)) * (priceBottom - PAD.top);
  const vmax = extent([volume])?.[1] || 1;
  const vy = (v) => height - PAD.bottom - (v / vmax) * volH;

  frame(container, {
    height,
    sessions,
    ariaLabel: `SPY daily candlestick chart, ${sessions.length} sessions from ${formatDate(sessions[0])} to ${formatDate(sessions.at(-1))}. Use the arrow keys to inspect sessions.`,
    draw(svg, { width, x, step }) {
      svg.append(yAxis(niceTicks(lo, hi), y, width, (t) => formatNumber(t, t >= 100 ? 0 : 2)));
      const bw = Math.max(1, step * 0.7);
      let wicks = "";
      let up = "";
      let down = "";
      let vUp = "";
      let vDown = "";
      for (let i = 0; i < sessions.length; i++) {
        const [o, h, l, c] = [open[i], high[i], low[i], close[i]];
        if (![o, h, l, c].every(finite)) continue;
        const cx = x(i);
        wicks += `M${cx.toFixed(1)} ${y(h).toFixed(1)}V${y(l).toFixed(1)}`;
        const top = y(Math.max(o, c));
        const bh = Math.max(1, y(Math.min(o, c)) - top);
        const rect = `M${(cx - bw / 2).toFixed(1)} ${top.toFixed(1)}h${bw.toFixed(1)}v${bh.toFixed(1)}h${(-bw).toFixed(1)}Z`;
        if (c >= o) up += rect;
        else down += rect;
        if (finite(volume[i])) {
          const vt = vy(volume[i]);
          const vr = `M${(cx - bw / 2).toFixed(1)} ${vt.toFixed(1)}h${bw.toFixed(1)}V${height - PAD.bottom}h${(-bw).toFixed(1)}Z`;
          if (c >= o) vUp += vr;
          else vDown += vr;
        }
      }
      svg.append(
        s("path", { d: vUp, class: "vol-up" }),
        s("path", { d: vDown, class: "vol-down" }),
        s("path", { d: wicks, class: "wick" }),
        s("path", { d: up, class: "body-up" }),
        s("path", { d: down, class: "body-down" }),
      );
      for (const k of shown) {
        svg.append(s("path", { d: linePath(data.overlays[k], x, y), class: `line ${OVERLAY_STYLES[k].cls}` }));
      }
      svg.append(xLabels(sessions, x, height - 6));
      const vl = s("text", { x: PAD.left + 2, y: height - PAD.bottom - volH + 10, class: "pane-label" });
      vl.textContent = "Volume";
      svg.append(vl);
    },
    describe: (i) =>
      `${formatDate(sessions[i])} · O ${formatNumber(open[i])} · H ${formatNumber(high[i])} · ` +
      `L ${formatNumber(low[i])} · C ${formatNumber(close[i])} · Vol ${formatCompact(volume[i])}` +
      shown.map((k) => ` · ${OVERLAY_STYLES[k].label} ${formatNumber(data.overlays[k][i])}`).join(""),
  });
}

// Display-only point reduction (the data itself is never changed). Sessions are split into
// consecutive buckets of `bucketSize`; per bucket the indices of the minimum and the maximum
// finite value are kept (ties: earliest), plus the first and last session. Every extreme at
// bucket resolution — therefore the global peak and trough, e.g. the deepest drawdown — is kept
// exactly, and nothing is averaged. The first null of a bucket is kept too, so gaps still break
// the line. Deterministic: same input, same indices. Returns ascending original indices.
export function reduceMinMax(values, bucketSize) {
  const n = values.length;
  if (!(bucketSize > 1) || n <= 2) return Array.from({ length: n }, (_, i) => i);
  const keep = new Set([0, n - 1]);
  for (let start = 0; start < n; start += bucketSize) {
    const end = Math.min(n, start + bucketSize);
    let lo = -1;
    let hi = -1;
    let gap = -1;
    for (let i = start; i < end; i++) {
      const v = values[i];
      if (!finite(v)) {
        if (gap < 0) gap = i;
        continue;
      }
      if (lo < 0 || v < values[lo]) lo = i;
      if (hi < 0 || v > values[hi]) hi = i;
    }
    for (const i of [lo, hi, gap]) if (i >= 0) keep.add(i);
  }
  return [...keep].sort((a, b) => a - b);
}

// Session count above which line charts are reduced for display, and the bucket size used.
export const REDUCE_ABOVE = 1500;
export const bucketFor = (n) => (n > REDUCE_ABOVE ? Math.ceil(n / 1000) : 1);

function linePathAt(values, indices, x, y) {
  let d = "";
  let pen = false;
  for (const i of indices) {
    const v = values[i];
    if (!finite(v)) {
      pen = false;
      continue;
    }
    d += `${pen ? "L" : "M"}${x(i).toFixed(1)} ${y(v).toFixed(1)}`;
    pen = true;
  }
  return d;
}

// Several series on one session axis (e.g. cumulative return, drawdown). Lines are drawn from
// the reduced indices; the readout always shows the exact value of every series at the cursor.
export function drawLineChart(container, { sessions, series, label, yFormat, refs = [], height = 260 }) {
  if (tooFew(container, sessions, label)) return null;
  const visible = series.filter((s) => s.values.some(finite));
  const ex = extent([...visible.map((s) => s.values), refs]);
  if (!visible.length || !ex) {
    const p = document.createElement("p");
    p.className = "chart-empty";
    p.textContent = `${label}: no series selected or no defined values in this period.`;
    container.replaceChildren(p);
    return null;
  }
  const pad = (ex[1] - ex[0]) * 0.05 || Math.abs(ex[1]) * 0.05 || 0.01;
  const [lo, hi] = [ex[0] - pad, ex[1] + pad];
  const y = (v) => PAD.top + ((hi - v) / (hi - lo)) * (height - PAD.top - PAD.bottom);
  const bucket = bucketFor(sessions.length);
  let drawn = 0;
  frame(container, {
    height,
    sessions,
    ariaLabel: `${label}: ${visible.length} series, ${sessions.length} sessions from ${formatDate(sessions[0])} to ${formatDate(sessions.at(-1))}. Use the arrow keys to inspect sessions.`,
    draw(svg, { width, x }) {
      svg.append(yAxis(niceTicks(lo, hi, 4), y, width, yFormat));
      for (const r of refs) {
        svg.append(s("line", { x1: PAD.left, x2: width - PAD.right, y1: y(r), y2: y(r), class: "ref" }));
      }
      for (const sr of visible) {
        const idx = reduceMinMax(sr.values, bucket);
        drawn = Math.max(drawn, idx.length);
        svg.append(s("path", { d: linePathAt(sr.values, idx, x, y), class: `line ${sr.cls}` }));
      }
      svg.append(xLabels(sessions, x, height - 6));
    },
    describe: (i) => `${formatDate(sessions[i])} · ` + visible.map((sr) => `${sr.label} ${yFormat(sr.values[i])}`).join(" · "),
  });
  return { sessions: sessions.length, bucket, maxPointsDrawn: drawn, reduced: bucket > 1 };
}

// Oscillator pane: lines, optional histogram and fixed reference levels.
export function drawOscillator(container, { sessions, lines, histogram, refs = [], domain, label, decimals = 2 }) {
  if (tooFew(container, sessions, label)) return;
  const height = 170;
  const ex = domain || extent([...lines.map((l) => l.values), ...(histogram ? [histogram] : [])]);
  if (!ex) {
    const p = document.createElement("p");
    p.className = "chart-empty";
    p.textContent = `${label}: no defined values in this range (indicator warm-up).`;
    container.replaceChildren(p);
    return;
  }
  let [lo, hi] = ex;
  if (!domain) {
    const m = Math.max(Math.abs(lo), Math.abs(hi)) * 1.1 || 1;
    [lo, hi] = histogram ? [-m, m] : [lo - (hi - lo) * 0.05, hi + (hi - lo) * 0.05];
  }
  const y = (v) => PAD.top + ((hi - v) / (hi - lo || 1)) * (height - PAD.top - PAD.bottom);

  frame(container, {
    height,
    sessions,
    ariaLabel: `${label} for ${sessions.length} sessions. Use the arrow keys to inspect sessions.`,
    draw(svg, { width, x, step }) {
      // fixed scales (RSI) label their reference levels as whole numbers
      svg.append(yAxis(domain ? refs : niceTicks(lo, hi, 4), y, width, (t) => formatNumber(t, domain ? 0 : 2)));
      for (const r of refs) {
        svg.append(s("line", { x1: PAD.left, x2: width - PAD.right, y1: y(r), y2: y(r), class: "ref" }));
      }
      if (histogram) {
        const bw = Math.max(1, step * 0.7);
        let pos = "";
        let neg = "";
        histogram.forEach((v, i) => {
          if (!finite(v)) return;
          const top = y(Math.max(v, 0));
          const h = Math.max(0.5, Math.abs(y(v) - y(0)));
          const r = `M${(x(i) - bw / 2).toFixed(1)} ${top.toFixed(1)}h${bw.toFixed(1)}v${h.toFixed(1)}h${(-bw).toFixed(1)}Z`;
          if (v >= 0) pos += r;
          else neg += r;
        });
        svg.append(s("path", { d: pos, class: "hist-pos" }), s("path", { d: neg, class: "hist-neg" }));
      }
      for (const l of lines) svg.append(s("path", { d: linePath(l.values, x, y), class: `line ${l.cls}` }));
      svg.append(xLabels(sessions, x, height - 6));
    },
    describe: (i) =>
      `${formatDate(sessions[i])} · ` +
      [...lines.map((l) => `${l.label} ${formatNumber(l.values[i], decimals)}`), ...(histogram ? [`Histogram ${formatNumber(histogram[i], decimals)}`] : [])].join(" · "),
  });
}
