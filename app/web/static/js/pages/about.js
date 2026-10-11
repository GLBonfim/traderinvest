// About: purpose, data sources, what the interface does not do, limitations and status.
// Static explanatory text only; no data is loaded.

import { badge, h, section } from "../ui.js";

const SOURCES = [
  ["DEMO", "in-use", "Hand-written illustrative values shipped with the interface. The only data shown in this release."],
  ["LOCAL", "in-use-local", "Stored end-of-day research data from the local platform database, read through read-only endpoints of this server. Used by Market Analysis and Research; not live market data."],
  ["EXTERNAL", "never", "Market-data providers. This interface never contacts them; its content policy blocks any request outside its own origin."],
  ["BROKER", "never", "Broker or account state. Never shown or contacted by this interface."],
];

const DOES_NOT = [
  "Trade, or create, change or cancel orders of any kind",
  "Connect to a broker or show broker or account state",
  "Fetch market data or contact any external service",
  "Run, schedule or change platform operations, data loading or simulations",
  "Change any configuration or stored data",
];

const LIMITATIONS = [
  "Overview values are illustrative (DEMO)",
  "Market Analysis covers SPY daily bars stored locally (yfinance, prototype-grade, raw dividend-unadjusted prices); it is never live",
  "Research shows backtests of fixed baseline rules only: no statistical validation, machine learning or walk-forward in this interface",
  "Data & Operations is not implemented yet",
  "English only; dark theme only",
  "Intended for local, single-user use",
];

const STATUS = [
  ["Overview", "available", "Dataset context and demonstration snapshot"],
  ["Market Analysis", "available", "Stored end-of-day SPY data, read-only (LOCAL)"],
  ["Research", "available", "Stored backtests of the baseline strategies, read-only (LOCAL)"],
  ["Data & Operations", "planned", "Not implemented yet"],
  ["About", "available", "This page"],
];

const bullets = (items) => h("ul", { class: "bullets" }, items.map((t) => h("li", {}, t)));

export function aboutPage(main) {
  main.append(
    h(
      "div",
      { class: "prose section" },
      h(
        "p",
        {},
        h("strong", {}, "TraderInvest"),
        " is a quantitative research platform for SPY / S&P 500. It studies technical " +
          "indicators, candlestick and price-action observations, market regimes and fixed " +
          "baseline strategies, and evaluates them with backtests and statistical validation " +
          "that report uncertainty and costs. It is a research tool: it makes no claim of " +
          "profitability and is not a trading terminal.",
      ),
      h(
        "p",
        {},
        "This interface is ",
        h("strong", {}, "read-only"),
        ". Overview shows ",
        h("strong", {}, "demonstration data (DEMO)"),
        "; Market Analysis and Research show ",
        h("strong", {}, "stored end-of-day research data (LOCAL)"),
        ". It is not connected to live market data.",
      ),
    ),
    section(
      "Data sources",
      null,
      h(
        "div",
        { class: "card table-card" },
        h(
          "table",
          { class: "table" },
          h(
            "thead",
            {},
            h("tr", {}, h("th", { scope: "col" }, "Source"), h("th", { scope: "col" }, "Meaning"), h("th", { scope: "col" }, "This release")),
          ),
          h(
            "tbody",
            {},
            SOURCES.map(([tag, status, text]) =>
              h(
                "tr",
                {},
                h("th", { scope: "row" }, h("span", { class: `badge ${tag === "DEMO" ? "badge-demo" : "badge-neutral"}` }, tag)),
                h("td", {}, text),
                h("td", {}, badge(status)),
              ),
            ),
          ),
        ),
      ),
    ),
    h(
      "div",
      { class: "two-col" },
      section("What this interface does not do", null, h("div", { class: "card" }, bullets(DOES_NOT))),
      section("Current limitations", null, h("div", { class: "card" }, bullets(LIMITATIONS))),
    ),
    section(
      "Implementation status",
      null,
      h(
        "div",
        { class: "card table-card" },
        h(
          "table",
          { class: "table" },
          h("thead", {}, h("tr", {}, h("th", { scope: "col" }, "Area"), h("th", { scope: "col" }, "Notes"), h("th", { scope: "col" }, "Status"))),
          h(
            "tbody",
            {},
            STATUS.map(([area, status, note]) =>
              h("tr", {}, h("th", { scope: "row" }, area), h("td", {}, note), h("td", {}, badge(status))),
            ),
          ),
        ),
      ),
    ),
  );
}
