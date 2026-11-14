// Research: stored backtests of the fixed baseline strategies and both buy & hold benchmarks
// (LOCAL, read-only, ADR-0029). Series are shown in the API's fixed order — never ranked or
// sorted by performance. Historical simulations; not evidence of future profitability.

import { bucketFor, drawLineChart } from "../charts.js";
import { api, ApiError, apiUrl, CURVE_KINDS, SLICES } from "../data.js";
import {
  formatDate,
  formatDateTime,
  formatInteger,
  formatNumber,
  formatPercent,
  MISSING,
} from "../format.js";
import { badge, emptyState, facts, h, loadingBlock, section } from "../ui.js";

const SLICE_LABELS = { full: "Full history", early: "Early (≤ 2009)", late: "Late (≥ 2010)" };
const KIND_LABELS = { net: "Net of costs", gross: "Gross (no costs)" };
const SERIES_KIND = {
  strategy: "Strategy",
  benchmark_price: "Benchmark · price return",
  benchmark_total: "Benchmark · total return",
};
const NULL_NOTE = "Not defined for this series and period (e.g. no completed trade or no variance).";

// Metric columns, in this order; only those actually returned by the API are shown.
export const METRIC_COLUMNS = [
  ["cagr", "CAGR", (v) => formatPercent(v, 1)],
  ["cumulative_return", "Cumulative return", (v) => formatPercent(v, 1)],
  ["annualized_volatility", "Volatility (ann.)", (v) => formatPercent(v, 1)],
  ["sharpe", "Sharpe", (v) => formatNumber(v, 2)],
  ["sortino", "Sortino", (v) => formatNumber(v, 2)],
  ["max_drawdown", "Max drawdown", (v) => formatPercent(v, 1)],
  ["exposure", "Exposure", (v) => formatPercent(v, 1)],
  ["annualized_turnover", "Turnover (ann.)", (v) => (v === null || v === undefined ? MISSING : `${formatNumber(v, 2)}×`)],
  ["orders", "Orders", (v) => formatInteger(v)],
  ["total_costs", "Total costs", (v) => formatInteger(v)],
];

// One cell: a null metric is an explicit "not defined" mark, never a zero.
export function metricCell(value, fmt) {
  if (value === null || value === undefined) {
    return h("td", { class: "num null-value", title: NULL_NOTE, "aria-label": "not defined" }, MISSING);
  }
  return h("td", { class: "num" }, fmt(value));
}

function failure(err, what) {
  if (err instanceof ApiError && err.status === 503) {
    return h(
      "div",
      { class: "state state-error", role: "alert" },
      h("p", { class: "state-title" }, "Local research database unavailable"),
      h("div", {}, `The ${what} could not be loaded because the local database is not reachable. Nothing was changed.`),
    );
  }
  if (err instanceof ApiError && err.status === 404) {
    return emptyState("No stored data for this period", `There are not enough stored sessions to compute the ${what}.`);
  }
  return h(
    "div",
    { class: "state state-error", role: "alert" },
    h("p", { class: "state-title" }, `The ${what} could not be shown`),
    h("div", {}, err instanceof Error ? err.message : String(err)),
  );
}

function chip(label, pressed, onClick, extra = {}) {
  const b = h("button", { type: "button", class: "chip", "aria-pressed": String(pressed), ...extra }, label);
  b.addEventListener("click", () => onClick(b));
  return b;
}

const fullDataLink = (name, params, text) =>
  h("a", { href: apiUrl(name, params).pathname + apiUrl(name, params).search, target: "_blank", rel: "noopener noreferrer" }, text);

// ── blocks ──

function sliceInfo(bt) {
  const s = bt.slice;
  const w = bt.window;
  return h(
    "div",
    { class: "card" },
    facts([
      ["Period", SLICE_LABELS[s.name]],
      ["Declared range", `${s.start ? formatDate(s.start) : "first stored session"} – ${s.end ? formatDate(s.end) : "latest stored session"}`],
      ["Sessions in window", `${formatDate(w.first_session)} – ${formatDate(w.last_session)} · ${formatInteger(w.sessions)} sessions`],
      ["Out-of-sample", "No — descriptive sub-period"],
    ]),
    h("p", { class: "context-note" }, s.note),
  );
}

function comparisonTable(bt) {
  const present = METRIC_COLUMNS.filter(([k]) => bt.results.some((r) => k in r.metrics));
  return h(
    "div",
    { class: "card table-card" },
    h(
      "table",
      { class: "table table-wide", id: "comparison" },
      h(
        "caption",
        { class: "table-caption" },
        "Fixed order (strategies, then benchmarks) — not ranked; sorting is intentionally not offered.",
      ),
      h(
        "thead",
        {},
        h("tr", {}, h("th", { scope: "col" }, "Series"), present.map(([, label]) => h("th", { scope: "col", class: "num" }, label))),
      ),
      h(
        "tbody",
        {},
        bt.results.map((r, i) =>
          h(
            "tr",
            { "data-series": r.series_id },
            h(
              "th",
              { scope: "row" },
              h("span", { class: `swatch s${i}`, "aria-hidden": "true" }),
              r.series_id,
              h("span", { class: "cell-sub" }, SERIES_KIND[r.kind] || r.kind),
            ),
            present.map(([k, , fmt]) => metricCell(r.metrics[k], fmt)),
          ),
        ),
      ),
    ),
    h("p", { class: "table-foot" }, `${MISSING} = ${NULL_NOTE} Costs are in research-capital units (initial ${formatInteger(bt.assumptions.initial_capital)}).`),
  );
}

function positionsTable(bt) {
  return h(
    "div",
    { class: "card table-card" },
    h(
      "table",
      { class: "table table-wide" },
      h("caption", { class: "table-caption" }, `State at the end of the window (${formatDate(bt.window.last_session)})`),
      h("thead", {}, h("tr", {}, ["Series", "Position", "Decision not executed (no next session in data)"].map((c) => h("th", { scope: "col" }, c)))),
      h(
        "tbody",
        {},
        bt.results.map((r) =>
          h(
            "tr",
            {},
            h("th", { scope: "row" }, r.series_id),
            h("td", {}, r.open_position ? `Open, marked to market (since ${formatDate(r.open_position.entry_time)})` : "Flat"),
            h("td", {}, r.pending_state ? Object.entries(r.pending_state).map(([k, v]) => `${k}: ${v ?? MISSING}`).join(" · ") : "None"),
          ),
        ),
      ),
    ),
  );
}

function strategiesBlock(st, bt) {
  const a = bt.assumptions;
  const c = a.costs;
  return [
    h(
      "div",
      { class: "grid grid-compact" },
      st.strategies.map((s) =>
        h(
          "div",
          { class: "card" },
          h("div", { class: "card-head" }, h("span", { class: "card-label" }, s.strategy_id), badge("local", `v${s.version}`)),
          h("p", { class: "strategy-rule" }, s.description),
          h("div", { class: "kpi-note" }, `Inputs: ${s.features.join(", ")}`),
        ),
      ),
    ),
    h(
      "div",
      { class: "two-col" },
      h(
        "div",
        { class: "card" },
        h("h3", { class: "card-title" }, "Rules and fixed parameters"),
        h("ul", { class: "bullets" }, st.rules.map((r) => h("li", {}, r))),
        facts(Object.entries(st.parameters).map(([k, v]) => [k, String(v)])),
      ),
      h(
        "div",
        { class: "card" },
        h("h3", { class: "card-title" }, "Execution and cost assumptions"),
        facts([
          ["Execution", `${a.execution} (raw open of the next session)`],
          ["Initial capital", `${formatInteger(a.initial_capital)} (normalised research capital)`],
          ["Commission", `${formatNumber(c.commission_per_trade)} per order + ${formatNumber(c.commission_bps, 1)} bps (min ${formatNumber(c.minimum_commission)})`],
          ["Spread", `${formatNumber(c.spread_bps, 1)} bps quoted (half paid per order)`],
          ["Slippage", `${formatNumber(c.slippage_bps, 1)} bps per order`],
          ["Risk-free rate", formatPercent(a.risk_free_rate, 2)],
        ]),
        h("p", { class: "context-note" }, c.note),
      ),
    ),
    h(
      "div",
      { class: "card" },
      h("h3", { class: "card-title" }, "Benchmarks"),
      h("ul", { class: "bullets" }, st.benchmarks.map((b) => h("li", {}, h("strong", {}, `${SERIES_KIND[b.kind]}: `), b.description))),
      bt.data.total_return_benchmark_available ? null : h("p", { class: "muted" }, "Total-return benchmark unavailable: adjusted prices are missing."),
    ),
    h(
      "div",
      { class: "card" },
      h("h3", { class: "card-title" }, "Reproducibility"),
      facts([
        ["Dataset key", bt.meta.dataset_key],
        ["Stored data", `${bt.meta.symbol} · ${bt.meta.timeframe} · ${bt.meta.provider} · ${formatDate(bt.data.first_session)} – ${formatDate(bt.data.last_session)} · ${formatInteger(bt.data.bars)} bars`],
        ["Backtest configuration fingerprint", a.config_fingerprint],
        ["Strategy engine fingerprint", a.strategy_engine_fingerprint],
        ["Engine versions", Object.entries(bt.meta.engine_versions).map(([k, v]) => `${k} ${v}`).join(" · ")],
        ["Computed", formatDateTime(bt.meta.computed_at)],
        ["Full data (JSON)", h("span", {}, fullDataLink("research-backtests", { slice: bt.slice.name }, "backtests"), " · ", fullDataLink("research-curves", { slice: bt.slice.name, kind: "net" }, "net curves"), " · ", fullDataLink("research-curves", { slice: bt.slice.name, kind: "gross" }, "gross curves"))],
      ]),
    ),
  ];
}

// ── page ──

export async function researchPage(main) {
  const cache = new Map(); // one request per selection: promises are reused, failures are not
  const get = (name, params) => {
    const key = `${name}|${JSON.stringify(params)}`;
    if (!cache.has(key)) {
      const p = api.load(name, params);
      cache.set(key, p);
      p.catch(() => cache.delete(key));
    }
    return cache.get(key);
  };

  const state = { slice: "full", kind: "net", hidden: new Set(), seq: 0 };
  const periodBox = h("div", {}, loadingBlock(1));
  const tableBox = h("div", {}, loadingBlock(2));
  const curvesNote = h("p", { class: "chart-legend", id: "curves-note" });
  const cumChart = h("div", { class: "chart", id: "chart-cumulative" });
  const ddChart = h("div", { class: "chart", id: "chart-drawdown" });
  const seriesChips = h("div", { class: "chip-group", role: "group", "aria-label": "Series shown in the charts" });
  const positionsBox = h("div", {});
  const detailsBox = h("div", {}, loadingBlock(2));
  let order = [];

  const sliceChips = SLICES.map((sl) =>
    chip(SLICE_LABELS[sl], sl === state.slice, () => {
      if (state.slice === sl) return;
      state.slice = sl;
      for (const b of sliceChips) b.setAttribute("aria-pressed", String(b.dataset.slice === sl));
      refresh();
    }, { "data-slice": sl }),
  );
  const kindChips = CURVE_KINDS.map((k) =>
    chip(KIND_LABELS[k], k === state.kind, () => {
      if (state.kind === k) return;
      state.kind = k;
      for (const b of kindChips) b.setAttribute("aria-pressed", String(b.dataset.kind === k));
      refreshCurves();
    }, { "data-kind": k }),
  );

  function renderSeriesChips() {
    seriesChips.replaceChildren(
      ...order.map((id, i) =>
        chip(h("span", {}, h("span", { class: `swatch s${i}`, "aria-hidden": "true" }), id), !state.hidden.has(id), (b) => {
          const show = b.getAttribute("aria-pressed") !== "true";
          b.setAttribute("aria-pressed", String(show));
          show ? state.hidden.delete(id) : state.hidden.add(id);
          drawCurves();
        }, { "data-series": id }),
      ),
    );
  }

  let curves = null;
  function drawCurves() {
    if (!curves) return;
    const series = (key) =>
      order
        .map((id, i) => ({ id, label: id, values: curves.series[id][key], cls: `s${i}` }))
        .filter((sr) => !state.hidden.has(sr.id));
    const pct = (v) => formatPercent(v, Math.abs(v ?? 0) >= 10 ? 0 : 1);
    const info = drawLineChart(cumChart, { sessions: curves.sessions, series: series("cumulative_return"), label: `Cumulative return (${KIND_LABELS[curves.kind].toLowerCase()})`, yFormat: pct, refs: [0] });
    drawLineChart(ddChart, { sessions: curves.sessions, series: series("drawdown"), label: `Drawdown (${KIND_LABELS[curves.kind].toLowerCase()})`, yFormat: (v) => formatPercent(v, 1), refs: [0], height: 200 });
    const n = curves.sessions.length;
    const bucket = bucketFor(n);
    curvesNote.textContent =
      bucket > 1
        ? `Reduced for display: ${formatInteger(n)} sessions drawn as the minimum and maximum of each block of ${bucket} sessions (every peak and trough at that resolution, including the deepest drawdown, is kept; nothing is averaged). The readout above each chart shows the exact value of every session. Full-resolution data: the JSON links under Reproducibility.`
        : `All ${formatInteger(n)} sessions drawn (no reduction).`;
    curvesNote.dataset.reduced = String(bucket > 1 && Boolean(info));
  }

  async function refreshCurves() {
    const seq = ++state.seq;
    cumChart.replaceChildren(loadingBlock(1));
    ddChart.replaceChildren();
    try {
      const c = await get("research-curves", { slice: state.slice, kind: state.kind });
      if (seq !== state.seq) return;
      curves = c;
      drawCurves();
    } catch (err) {
      if (seq === state.seq) {
        curves = null;
        cumChart.replaceChildren(failure(err, "curves"));
        curvesNote.textContent = "";
      }
    }
  }

  async function refresh() {
    periodBox.replaceChildren(loadingBlock(1));
    tableBox.replaceChildren(loadingBlock(2));
    const curvesDone = refreshCurves();
    try {
      const bt = await get("research-backtests", { slice: state.slice });
      order = bt.results.map((r) => r.series_id); // exactly the API order
      if (seriesChips.childElementCount !== order.length) renderSeriesChips();
      periodBox.replaceChildren(sliceInfo(bt));
      tableBox.replaceChildren(comparisonTable(bt));
      positionsBox.replaceChildren(positionsTable(bt));
      if (strategies) detailsBox.replaceChildren(...strategiesBlock(strategies, bt));
      if (curves) drawCurves();
    } catch (err) {
      periodBox.replaceChildren();
      tableBox.replaceChildren(failure(err, "backtest results"));
      positionsBox.replaceChildren();
    }
    await curvesDone;
  }

  let width = 0;
  new ResizeObserver((entries) => {
    const w = Math.round(entries[0].contentRect.width);
    if (w !== width) {
      width = w;
      drawCurves();
    }
  }).observe(cumChart);

  main.append(
    h("p", { class: "page-intro" }, "Backtests of the fixed baseline strategies against SPY buy & hold, on stored closed daily bars. Read-only research results."),
    h(
      "div",
      { class: "callout callout-warn", role: "note" },
      h("strong", {}, "Historical simulation, not evidence of future profitability. "),
      "One instrument and one historical path; generic cost assumptions; nothing here is ranked or recommended.",
    ),
    section("Period", h("div", { class: "chip-group", role: "group", "aria-label": "Period" }, sliceChips), periodBox),
    section("Strategy comparison", badge("local"), tableBox),
    section(
      "Curves",
      badge("local"),
      h(
        "div",
        { class: "card chart-card" },
        h(
          "div",
          { class: "chart-toolbar" },
          h("div", { class: "chip-group", role: "group", "aria-label": "Costs" }, kindChips),
        ),
        seriesChips,
        cumChart,
        ddChart,
        curvesNote,
      ),
    ),
    section("Positions at the end of the window", null, positionsBox),
    section("Strategies, assumptions and reproducibility", null, detailsBox),
  );

  let strategies = null;
  try {
    strategies = await get("research-strategies", {});
  } catch (err) {
    detailsBox.replaceChildren(failure(err, "strategy definitions"));
  }
  await refresh();
}
