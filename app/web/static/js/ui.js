// Small DOM helpers and reusable components. Elements are built with createElement /
// textContent only, so no data value can ever be interpreted as markup.

import { formatValue, MISSING } from "./format.js";

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") el.className = v;
    else el.setAttribute(k, v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

const BADGES = {
  demo: ["badge-demo", "DEMO"],
  local: ["badge-local", "LOCAL"],
  stale: ["badge-warn", "Stale"],
  current: ["badge-ok", "Up to date"],
  unavailable: ["badge-off", "Unavailable"],
  available: ["badge-ok", "Available"],
  planned: ["badge-neutral", "Planned"],
  missing: ["badge-off", "Not provided"],
  never: ["badge-off", "Never"],
  "not-connected": ["badge-neutral", "Not connected"],
  "in-use": ["badge-demo", "In use"],
  "in-use-local": ["badge-local", "In use"],
};

export function badge(kind, text) {
  const [cls, label] = BADGES[kind] || ["badge-neutral", kind];
  return h("span", { class: `badge ${cls}` }, text || label);
}

export function sectionHead(title, ...extra) {
  return h("div", { class: "section-head" }, h("h2", { class: "section-title" }, title), extra);
}

export function section(title, extra, ...body) {
  return h("section", { class: "section" }, sectionHead(title, extra), body);
}

export function emptyState(title, text, planned = []) {
  return h(
    "div",
    { class: "state" },
    h("p", { class: "state-title" }, title),
    h("div", {}, text),
    planned.length ? h("ul", { class: "planned" }, planned.map((p) => h("li", {}, p))) : null,
  );
}

export function errorState(message) {
  return h(
    "div",
    { class: "state state-error", role: "alert" },
    h("p", { class: "state-title" }, "Demonstration content could not be shown"),
    h("div", {}, message),
  );
}

export function loadingBlock(n = 4) {
  return h(
    "div",
    { class: "grid", "aria-busy": "true", "aria-label": "Loading" },
    Array.from({ length: n }, () => h("div", { class: "skeleton" })),
  );
}

// A value card tagged with its source (DEMO or LOCAL). A null/undefined value renders as an
// explicit "Not provided" state, never as zero. `item.text` overrides the formatted value.
export function kpiCard(item, source = "demo") {
  const value = item.text !== undefined ? item.text : formatValue(item.value, item.kind);
  const missing = value === MISSING;
  return h(
    "div",
    { class: missing ? "card kpi kpi-missing" : "card kpi" },
    h(
      "div",
      { class: "card-head" },
      h("span", { class: "card-label" }, item.label),
      missing ? badge("missing") : badge(source),
    ),
    h("div", { class: "kpi-value" }, value),
    item.note ? h("div", { class: "kpi-note" }, item.note) : null,
  );
}

// Definition list: [label, value] pairs shown as a compact two-column block.
export function facts(pairs) {
  return h(
    "dl",
    { class: "facts" },
    pairs.map(([k, v]) => [h("dt", {}, k), h("dd", {}, v)]),
  );
}
