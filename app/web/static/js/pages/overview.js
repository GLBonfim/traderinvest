// Overview: dataset context, DEMO snapshot and interface coverage. Every value comes from
// demo/overview.json through the DEMO-only data source.

import { source } from "../data.js";
import { formatDate, formatInteger } from "../format.js";
import {
  badge,
  emptyState,
  errorState,
  facts,
  h,
  kpiCard,
  loadingBlock,
  section,
} from "../ui.js";

function contextPanel(ds, meta) {
  return h(
    "div",
    { class: "card context" },
    h(
      "div",
      { class: "context-head" },
      h(
        "div",
        {},
        h("div", { class: "context-eyebrow" }, "Demonstration dataset"),
        h("div", { class: "context-title" }, `${ds.instrument} · ${ds.timeframe}`),
        h("div", { class: "context-sub" }, ds.instrument_note),
      ),
      badge("demo"),
    ),
    facts([
      ["Illustrative period", `${formatDate(ds.period_start)} – ${formatDate(ds.period_end)}`],
      ["Source", "DEMO · static file shipped with the interface"],
      ["Nature", ds.nature],
    ]),
    h("p", { class: "context-note" }, meta.note),
  );
}

function coverageTable(rows) {
  return h(
    "div",
    { class: "card table-card" },
    h(
      "table",
      { class: "table" },
      h(
        "thead",
        {},
        h("tr", {}, h("th", { scope: "col" }, "Area"), h("th", { scope: "col" }, "Content"), h("th", { scope: "col" }, "Interface")),
      ),
      h(
        "tbody",
        {},
        rows.map((r) =>
          h(
            "tr",
            {},
            h("th", { scope: "row" }, r.area),
            h("td", {}, r.detail),
            h("td", {}, badge(r.status === "available" ? "available" : "planned")),
          ),
        ),
      ),
    ),
  );
}

export async function overviewPage(main) {
  const slot = h("div", {}, loadingBlock(4));
  main.append(
    h(
      "p",
      { class: "page-intro" },
      "A first look at the platform. On this page every value is a hand-written " +
        "illustration: nothing reflects current markets, research results or any account.",
    ),
    slot,
    section(
      "Market summary",
      null,
      emptyState(
        "Shown on Market Analysis",
        "Stored end-of-day research data (LOCAL, not live) is shown on the Market Analysis " +
          "page. This page stays demonstrative.",
      ),
    ),
  );

  let data;
  try {
    data = await source.load("overview");
  } catch (err) {
    slot.replaceChildren(errorState(err instanceof Error ? err.message : String(err)));
    return;
  }

  const snapshot = Array.isArray(data.snapshot) ? data.snapshot : [];
  const coverage = Array.isArray(data.coverage) ? data.coverage : [];
  const available = coverage.filter((r) => r.status === "available").length;
  slot.replaceChildren(
    data.dataset ? section("Dataset context", null, contextPanel(data.dataset, data.meta)) : null,
    section(
      "Demonstration snapshot",
      badge("demo"),
      snapshot.length
        ? h("div", { class: "grid" }, snapshot.map((item) => kpiCard(item, "demo")))
        : emptyState("No demonstration values", "This dataset is empty."),
    ),
    section(
      "Interface coverage",
      h("span", { class: "section-meta" }, `${formatInteger(available)} of ${formatInteger(coverage.length)} areas available`),
      coverage.length ? coverageTable(coverage) : emptyState("No coverage information", "This dataset is empty."),
    ),
  );
}
