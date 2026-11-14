// Market Analysis: stored end-of-day research data (LOCAL) from the two read-only market
// endpoints. Describes the latest CLOSED session only; candlestick patterns, price-action
// structure and regimes are observations, not signals.

import { drawOscillator, drawPriceChart, OVERLAY_STYLES } from "../charts.js";
import { api, ApiError, RANGES } from "../data.js";
import {
  formatCompact,
  formatDate,
  formatDateTime,
  formatInteger,
  formatNumber,
  formatPercent,
  humanize,
  MISSING,
} from "../format.js";
import { badge, emptyState, facts, h, kpiCard, loadingBlock, section } from "../ui.js";

const DEFAULT_RANGE = "1y";
const RANGE_LABELS = { "3m": "3M", "6m": "6M", "1y": "1Y", "2y": "2Y", "5y": "5Y" };
const OVERLAY_GROUPS = [
  { id: "sma_20", label: "SMA 20", keys: ["sma_20"] },
  { id: "sma_50", label: "SMA 50", keys: ["sma_50"] },
  { id: "sma_200", label: "SMA 200", keys: ["sma_200"] },
  { id: "bb", label: "Bollinger", keys: ["bb_upper", "bb_middle", "bb_lower"] },
];
const REGIME_ORDER = ["composite", "trend", "volatility", "momentum", "participation"];

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
    return emptyState("No stored market data", "There are no stored closed daily bars for SPY yet.");
  }
  return h(
    "div",
    { class: "state state-error", role: "alert" },
    h("p", { class: "state-title" }, `The ${what} could not be shown`),
    h("div", {}, err instanceof Error ? err.message : String(err)),
  );
}

function toggle(label, pressed, onClick) {
  const b = h("button", { type: "button", class: "chip", "aria-pressed": String(pressed) }, label);
  b.addEventListener("click", () => onClick(b));
  return b;
}

// ── blocks ──

function sourcePanel(sm) {
  const { meta, dataset } = sm;
  const fr = dataset.freshness;
  const run = dataset.last_ingestion;
  return h(
    "div",
    { class: "card context context-local" },
    h(
      "div",
      { class: "context-head" },
      h(
        "div",
        {},
        h("div", { class: "context-eyebrow" }, "Stored research data"),
        h("div", { class: "context-title" }, `${meta.symbol} · Daily, end of day`),
        h("div", { class: "context-sub" }, `Provider ${meta.provider} (prototype-grade) · prices ${meta.prices}`),
      ),
      h("div", { class: "badge-row" }, badge("local"), badge(fr.stale ? "stale" : "current")),
    ),
    facts([
      ["Stored period", `${formatDate(dataset.first_session)} – ${formatDate(dataset.last_session)}`],
      ["Closed sessions", formatInteger(dataset.bars)],
      ["Latest closed session", formatDate(dataset.last_session)],
      [
        "Freshness",
        fr.stale
          ? `${formatInteger(fr.missing_sessions)} completed session(s) not stored yet (expected ${formatDate(fr.expected_session)})`
          : "Up to date with the exchange calendar",
      ],
      ["Market status", `${fr.market_status} — checked ${formatDateTime(fr.as_of)}`],
      ["Last data load", run ? `#${run.run_id} ${run.status} · ${formatDateTime(run.finished_at)}` : MISSING],
    ]),
    h("p", { class: "context-note" }, `${meta.label}. Values describe stored closed sessions only; nothing here is live or a recommendation.`),
  );
}

function latestCards(bar) {
  const signed = bar.change === null ? MISSING : `${bar.change > 0 ? "+" : ""}${formatNumber(bar.change)}`;
  const items = [
    { label: "Close", text: formatNumber(bar.close), note: formatDate(bar.session) },
    { label: "Change vs previous close", text: signed, note: formatPercent(bar.change_pct, 2, true) },
    { label: "Open", text: formatNumber(bar.open) },
    { label: "High", text: formatNumber(bar.high) },
    { label: "Low", text: formatNumber(bar.low) },
    { label: "Volume", text: formatCompact(bar.volume), note: "shares" },
  ];
  return h("div", { class: "grid grid-compact" }, items.map((i) => kpiCard(i, "local")));
}

function regimeBlock(rg) {
  const cards = REGIME_ORDER.filter((k) => rg.dimensions[k]).map((k) => {
    const d = rg.dimensions[k];
    return h(
      "div",
      { class: d.available ? "card regime" : "card regime kpi-missing" },
      h("div", { class: "card-head" }, h("span", { class: "card-label" }, humanize(k)), d.available ? badge("local") : badge("unavailable")),
      h("div", { class: "kpi-value regime-label" }, d.available ? humanize(d.label) : "Not enough history"),
      h(
        "div",
        { class: "kpi-note" },
        d.available
          ? `${formatInteger(d.age_sessions)} session(s)` + (d.changed_on_session ? " · changed on this session" : "") + (d.previous_label ? ` · previously ${humanize(d.previous_label)}` : "")
          : "Label withheld: insufficient data",
      ),
    );
  });
  const m = rg.measures;
  return [
    h("div", { class: "grid grid-compact" }, cards),
    h(
      "div",
      { class: "card measures" },
      facts([
        ["Trend quality", formatNumber(m.trend_quality)],
        ["Realized volatility (20, annualised)", formatPercent(m.volatility_measure)],
        ["Volatility percentile (causal)", formatPercent(m.volatility_percentile, 0)],
        ["ATR / close", formatPercent(m.atr_pct)],
        ["RSI 14", formatNumber(m.momentum_rsi, 1)],
        ["ROC 12", formatPercent(m.momentum_roc == null ? null : m.momentum_roc / 100)],
        ["MACD", formatNumber(m.momentum_macd)],
        ["Relative volume (20)", formatNumber(m.relative_volume)],
      ]),
    ),
  ];
}

function indicatorTable(rows) {
  // a new badge per row (a DOM node can only be in one place)
  const status = (st) =>
    st === "ok" ? badge("available", "Defined") : st === "warm-up" ? badge("planned", "Warm-up") : badge("missing", "Undefined");
  return h(
    "div",
    { class: "card table-card" },
    h(
      "table",
      { class: "table" },
      h("thead", {}, h("tr", {}, ["Indicator", "Definition", "Value", "Status"].map((c) => h("th", { scope: "col" }, c)))),
      h(
        "tbody",
        {},
        rows.map((r) =>
          h(
            "tr",
            {},
            h("th", { scope: "row" }, `${r.indicator}`, h("span", { class: "cell-sub" }, r.group)),
            h("td", {}, r.definition),
            h("td", { class: "num" }, r.value === null ? MISSING : formatNumber(r.value, Math.abs(r.value) >= 1e6 ? 0 : 2)),
            h("td", {}, status(r.status)),
          ),
        ),
      ),
    ),
  );
}

function observationBlock(sm) {
  const pats = sm.candles.patterns;
  const st = sm.structure;
  const s = st.state;
  // distances are fractions (close vs zone edge), e.g. 0.012 = 1.2%
  const zone = (lo, hi, dist) => (lo === null || hi === null ? MISSING : `${formatNumber(lo)} – ${formatNumber(hi)} (${formatPercent(dist, 1, true)} away)`);
  const patternList = pats.length
    ? h(
        "ul",
        { class: "bullets" },
        pats.map((p) => h("li", {}, `${humanize(p.pattern)} · ${humanize(p.orientation)} · ${formatInteger(p.bars_in_pattern)} bar(s) · strength ${formatNumber(p.strength)}`)),
      )
    : h("p", { class: "muted" }, "No candlestick pattern was observed on the latest session.");
  const events = st.recent_events.slice(-8).reverse();
  return [
    h(
      "div",
      { class: "two-col" },
      h(
        "div",
        { class: "card" },
        h("h3", { class: "card-title" }, `Candlestick patterns · ${formatDate(sm.latest_bar.session)}`),
        patternList,
      ),
      h(
        "div",
        { class: "card" },
        h("h3", { class: "card-title" }, "Price-action structure"),
        st.available
          ? facts([
              ["Structure", humanize(s.structure)],
              ["Trend", `${humanize(s.trend)} · quality ${formatNumber(s.trend_quality)}`],
              ["Last swing labels", `${humanize(s.last_high_label)} / ${humanize(s.last_low_label)}`],
              ["Support zone", zone(s.support_low, s.support_high, s.support_distance_pct)],
              ["Resistance zone", zone(s.resistance_low, s.resistance_high, s.resistance_distance_pct)],
              ["Consolidation", s.in_consolidation ? `Yes · ${formatInteger(s.range_bar_count)} sessions` : "No"],
            ])
          : h("p", { class: "muted" }, "Structure unavailable: not enough confirmed swings."),
      ),
    ),
    h(
      "div",
      { class: "card table-card" },
      h(
        "table",
        { class: "table" },
        h("caption", { class: "table-caption" }, `Price-action events available in the last ${st.recent_sessions} sessions (most recent first)`),
        h("thead", {}, h("tr", {}, ["Event", "Session", "Reference level", "Direction"].map((c) => h("th", { scope: "col" }, c)))),
        h(
          "tbody",
          {},
          events.length
            ? events.map((e) =>
                h(
                  "tr",
                  {},
                  h("th", { scope: "row" }, humanize(e.event_type)),
                  h("td", {}, formatDate(e.session)),
                  h("td", { class: "num" }, formatNumber(e.reference_level)),
                  h("td", {}, humanize(e.direction)),
                ),
              )
            : h("tr", {}, h("td", { colspan: "4" }, "No events in this window.")),
        ),
      ),
    ),
  ];
}

// ── charts ──

function chartsBlock() {
  const state = { range: DEFAULT_RANGE, overlays: new Set(["sma_50", "sma_200"]), cache: new Map(), data: null, seq: 0 };
  const price = h("div", { class: "chart" }, loadingBlock(1));
  const rsi = h("div", { class: "chart" });
  const macd = h("div", { class: "chart" });

  const rangeBtns = RANGES.map((r) =>
    toggle(RANGE_LABELS[r], r === state.range, () => {
      state.range = r;
      for (const b of rangeBtns) b.setAttribute("aria-pressed", String(b.textContent === RANGE_LABELS[r]));
      load();
    }),
  );
  const overlayBtns = OVERLAY_GROUPS.map((g) =>
    toggle(g.label, g.keys.every((k) => state.overlays.has(k)) || state.overlays.has(g.id), (b) => {
      const on = b.getAttribute("aria-pressed") !== "true";
      b.setAttribute("aria-pressed", String(on));
      for (const k of g.keys) on ? state.overlays.add(k) : state.overlays.delete(k);
      draw();
    }),
  );

  function draw() {
    const d = state.data;
    if (!d) return;
    drawPriceChart(price, d, [...state.overlays]);
    drawOscillator(rsi, {
      sessions: d.sessions,
      lines: [{ label: "RSI 14", values: d.oscillators.rsi_14, cls: "ln-rsi" }],
      refs: [30, 50, 70],
      domain: [0, 100],
      label: "RSI 14",
      decimals: 1,
    });
    drawOscillator(macd, {
      sessions: d.sessions,
      lines: [
        { label: "MACD", values: d.oscillators.macd, cls: "ln-macd" },
        { label: "Signal", values: d.oscillators.macd_signal, cls: "ln-signal" },
      ],
      histogram: d.oscillators.macd_hist,
      refs: [0],
      label: "MACD (12, 26, 9)",
    });
  }

  async function load() {
    const seq = ++state.seq;
    if (!state.cache.has(state.range)) {
      price.replaceChildren(loadingBlock(1));
      try {
        state.cache.set(state.range, await api.load("market-series", { range: state.range }));
      } catch (err) {
        if (seq === state.seq) price.replaceChildren(failure(err, "price series"));
        rsi.replaceChildren();
        macd.replaceChildren();
        return;
      }
    }
    if (seq !== state.seq) return;
    state.data = state.cache.get(state.range);
    draw();
  }

  let width = 0;
  const ro = new ResizeObserver((entries) => {
    const w = Math.round(entries[0].contentRect.width);
    if (w !== width) {
      width = w;
      draw();
    }
  });
  ro.observe(price);

  load();
  return [
    section(
      "Price and volume",
      badge("local"),
      h(
        "div",
        { class: "card chart-card" },
        h(
          "div",
          { class: "chart-toolbar" },
          h("div", { class: "chip-group", role: "group", "aria-label": "Period" }, rangeBtns),
          h("div", { class: "chip-group", role: "group", "aria-label": "Overlays" }, overlayBtns),
        ),
        price,
        h("p", { class: "chart-legend" }, "Candles: raw daily OHLC; colour shows close above or below open only. Overlays are computed on the full stored history (no future data)."),
      ),
    ),
    h(
      "div",
      { class: "two-col" },
      section("RSI 14", badge("local"), h("div", { class: "card chart-card" }, rsi)),
      section("MACD (12, 26, 9)", badge("local"), h("div", { class: "card chart-card" }, macd)),
    ),
  ];
}

export async function marketPage(main) {
  const head = h("div", {}, loadingBlock(3));
  main.append(
    h("p", { class: "page-intro" }, "SPY end-of-day analysis from stored closed daily bars. Read-only; not live market data."),
    head,
  );

  let sm;
  try {
    sm = await api.load("market-summary");
  } catch (err) {
    head.replaceChildren(failure(err, "market summary"));
    return;
  }

  const incomplete = [];
  if (sm.dataset.freshness.stale) incomplete.push(`${formatInteger(sm.dataset.freshness.missing_sessions)} completed session(s) are not stored yet`);
  const unavailable = REGIME_ORDER.filter((k) => sm.regimes.dimensions[k] && !sm.regimes.dimensions[k].available);
  if (unavailable.length) incomplete.push(`regime label(s) unavailable: ${unavailable.map(humanize).join(", ")}`);
  if (!sm.structure.available) incomplete.push("price-action structure unavailable");
  const undef = sm.indicators.filter((r) => r.status !== "ok").length;
  if (undef) incomplete.push(`${formatInteger(undef)} indicator(s) without a defined value`);

  const blocks = [
    section("Data source", null, sourcePanel(sm)),
    incomplete.length
      ? h("div", { class: "callout callout-warn", role: "status" }, h("strong", {}, "Incomplete data: "), incomplete.join("; ") + ".")
      : null,
    section(`Latest closed session · ${formatDate(sm.latest_bar.session)}`, badge("local"), latestCards(sm.latest_bar)),
    ...chartsBlock(),
    section("Market regimes", h("span", { class: "section-meta" }, "Observations, not signals"), ...regimeBlock(sm.regimes)),
    section("Technical indicators", h("span", { class: "section-meta" }, `As of ${formatDate(sm.latest_bar.session)}`), indicatorTable(sm.indicators)),
    section("Patterns and structure", h("span", { class: "section-meta" }, "Observations, not signals"), ...observationBlock(sm)),
    h("p", { class: "disclaimer" }, "Observations, not signals. Candlestick patterns, price-action structure and regimes describe stored history; they are not predictions or recommendations."),
  ];
  head.replaceChildren(...blocks.filter(Boolean)); // replaceChildren would print null as text
}
