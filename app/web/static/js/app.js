// Shell: hash router, navigation state and the mobile menu. Read-only by construction: pages
// build the DOM with createElement/textContent and the only data access is data.js, which
// serves DEMO files only.

import { aboutPage } from "./pages/about.js";
import { marketPage } from "./pages/market.js";
import { overviewPage } from "./pages/overview.js";
import { researchPage } from "./pages/research.js";
import { emptyState, h } from "./ui.js";

// Banner per data source (ADR-0028). DEMO is also the static default in index.html.
const BANNERS = {
  demo: {
    cls: "badge badge-demo",
    tag: "DEMO",
    text: "DEMONSTRATION DATA — NOT LIVE MARKET DATA",
    note: "Illustrative values only · no market, account or broker data",
  },
  local: {
    cls: "badge badge-local",
    tag: "LOCAL",
    text: "NOT LIVE · END-OF-DAY RESEARCH DATA",
    note: "Stored closed daily bars · raw prices · read-only",
  },
};

function setBanner(kind) {
  const b = BANNERS[kind];
  document.getElementById("demo-banner").classList.toggle("banner-local", kind === "local");
  const tag = document.getElementById("banner-tag");
  tag.className = b.cls;
  tag.textContent = b.tag;
  document.getElementById("banner-text").textContent = b.text;
  document.getElementById("banner-note").textContent = b.note;
  const top = document.getElementById("topbar-badge");
  top.className = `${b.cls} topbar-badge`;
  top.textContent = b.tag;
}

function placeholderPage(intro, planned) {
  return (main) => {
    main.append(
      h("p", { class: "page-intro" }, intro),
      emptyState("Not implemented yet", "This area is planned for a later phase.", planned),
    );
  };
}

const ROUTES = {
  overview: { title: "Overview", render: overviewPage },
  market: { title: "Market Analysis", render: marketPage, source: "local" },
  research: { title: "Research", render: researchPage, source: "local" },
  "data-ops": {
    title: "Data & Operations",
    render: placeholderPage("Data quality and the history of local processing, read-only.", [
      "Data-quality events",
      "Ingestion and processing history",
      "Alerts",
    ]),
  },
  about: { title: "About", render: aboutPage },
};

const DEFAULT_ROUTE = "overview";

const shell = document.getElementById("shell");
const toggle = document.getElementById("nav-toggle");
const backdrop = document.getElementById("nav-backdrop");

function currentRoute() {
  const key = location.hash.replace(/^#\/?/, "");
  return key === "" ? DEFAULT_ROUTE : key;
}

function isNavOpen() {
  return shell.classList.contains("nav-open");
}

function setNavOpen(open, { restoreFocus = false } = {}) {
  shell.classList.toggle("nav-open", open);
  toggle.setAttribute("aria-expanded", String(open));
  if (open) {
    const active = document.querySelector('.nav-link[aria-current="page"]') || document.querySelector(".nav-link");
    if (active) active.focus();
  } else if (restoreFocus) {
    toggle.focus();
  }
}

let renderSeq = 0;

async function render() {
  const seq = ++renderSeq;
  const key = currentRoute();
  const route = ROUTES[key];
  const main = document.getElementById("main");
  main.replaceChildren();

  for (const link of document.querySelectorAll(".nav-link")) {
    if (link.dataset.route === key) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  if (isNavOpen()) setNavOpen(false);

  const title = route ? route.title : "Not found";
  const kind = route && route.source === "local" ? "local" : "demo";
  setBanner(kind);
  document.getElementById("page-title").textContent = title;
  document.title = `${title} · TraderInvest · ${BANNERS[kind].tag}`;

  if (!route) {
    main.append(emptyState("Page not found", "This address does not match any area of the interface."));
  } else {
    await route.render(main);
  }
  if (seq === renderSeq) main.focus({ preventScroll: true });
}

// The DEMO banner wraps onto several lines on narrow screens: keep the sticky top bar and the
// mobile menu exactly below it (CSSOM custom property; no inline style attribute).
const banner = document.getElementById("demo-banner");
const updateBannerHeight = () =>
  document.documentElement.style.setProperty("--banner-h", `${banner.offsetHeight}px`);
new ResizeObserver(updateBannerHeight).observe(banner);
updateBannerHeight();

toggle.addEventListener("click", () => setNavOpen(!isNavOpen()));
backdrop.addEventListener("click", () => setNavOpen(false, { restoreFocus: true }));
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && isNavOpen()) setNavOpen(false, { restoreFocus: true });
});
window.addEventListener("hashchange", render);
render();
