"""Read-only UI at /ui (static files): serving, write rejection, route inventory, DEMO marking
and the absence of external, network or operational code in the shipped assets."""

import ast
import hashlib
import json
import re
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from starlette.routing import BaseRoute, Mount, Route

from app.api import main as api_main
from app.api.main import app
from app.core.config import Settings, get_settings
from app.database.session import get_engine
from app.web import STATIC_DIR, ReadOnlyStaticFiles

client = TestClient(app)

BANNER = "DEMONSTRATION DATA — NOT LIVE MARKET DATA"
NAV = ("Overview", "Market Analysis", "Research", "Data &amp; Operations", "About")
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
ASSETS = sorted(p for p in STATIC_DIR.rglob("*") if p.is_file())
CODE = [p for p in ASSETS if p.suffix in {".html", ".js", ".css"}]


def _inventory(routes: list[BaseRoute], prefix: str = "") -> set[tuple[str, ...]]:
    """Every route of the app, flattened. Unknown route kinds fail instead of being skipped."""
    out: set[tuple[str, ...]] = set()
    for r in routes:
        if isinstance(r, APIRoute):
            out.add(("api", prefix + r.path, ",".join(sorted(r.methods))))
        elif isinstance(r, Mount):
            out.add(("mount", prefix + r.path, type(r.app).__name__))
        elif isinstance(r, Route):
            out.add(("route", prefix + r.path, ",".join(sorted(r.methods or ()))))
        elif type(r).__name__ == "_IncludedRouter":  # FastAPI include_router wrapper
            ctx = r.include_context  # type: ignore[attr-defined]
            out |= _inventory(r.original_router.routes, prefix + ctx.prefix)  # type: ignore[attr-defined]
        else:
            raise AssertionError(f"unexpected route type {type(r).__name__}")
    return out


def test_route_inventory_is_explicit() -> None:
    assert _inventory(app.routes) == {
        ("api", "/health", "GET"),
        # read-only market routes (ADR-0028); no other API endpoint is allowed
        ("api", "/api/v1/market/summary", "GET"),
        ("api", "/api/v1/market/series", "GET"),
        # read-only research routes (ADR-0029)
        ("api", "/api/v1/research/strategies", "GET"),
        ("api", "/api/v1/research/backtests", "GET"),
        ("api", "/api/v1/research/curves", "GET"),
        ("mount", "/ui", ReadOnlyStaticFiles.__name__),  # static files, GET/HEAD only
        # FastAPI's own documentation routes (unchanged)
        ("route", "/openapi.json", "GET,HEAD"),
        ("route", "/docs", "GET,HEAD"),
        ("route", "/docs/oauth2-redirect", "GET,HEAD"),
        ("route", "/redoc", "GET,HEAD"),
    }


def test_ui_mount_is_not_an_api_endpoint() -> None:
    assert set(app.openapi()["paths"]) == {
        "/health",
        "/api/v1/market/summary",
        "/api/v1/market/series",
        "/api/v1/research/strategies",
        "/api/v1/research/backtests",
        "/api/v1/research/curves",
    }


def test_ui_redirects_to_trailing_slash() -> None:
    resp = client.get("/ui", follow_redirects=False)
    assert resp.status_code in (307, 308)
    assert resp.headers["location"].endswith("/ui/")


def test_index_is_served_with_banner_and_navigation() -> None:
    resp = client.get("/ui/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    html = resp.text
    assert BANNER in html
    assert all(f"<span>{label}</span>" in html for label in NAV)
    for ref in ('href="css/tokens.css"', 'href="css/components.css"', 'src="js/app.js"'):
        assert ref in html  # relative: served from /ui itself
    assert 'id="nav-backdrop"' in html and 'aria-controls="sidebar"' in html
    assert '<link rel="icon" href="data:,">' in html  # no implicit /favicon.ico request


def test_full_application_lifespan_serves_ui_and_keeps_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real app, lifespan included (settings, logging, safety check), with settings that
    ignore the local .env and an unreachable database: /ui is served and /health unchanged."""
    isolated = Settings(_env_file=None, postgres_password="x")  # type: ignore[call-arg]
    dead = create_engine(
        "postgresql+psycopg://nobody:nopass@127.0.0.1:1/none", connect_args={"connect_timeout": 1}
    )
    monkeypatch.setattr(api_main, "get_settings", lambda: isolated)
    app.dependency_overrides[get_settings] = lambda: isolated
    app.dependency_overrides[get_engine] = lambda: dead
    try:
        with TestClient(app) as c:  # runs the lifespan startup and shutdown
            page = c.get("/ui/")
            assets = [c.get(p) for p in ("/ui/css/components.css", "/ui/js/app.js")]
            demo = c.get("/ui/demo/overview.json")
            health = c.get("/health")
            market_api = c.get("/api/v1/market/summary")
    finally:
        app.dependency_overrides.clear()
    assert page.status_code == 200 and BANNER in page.text
    assert all(r.status_code == 200 for r in assets)
    assert demo.json()["meta"]["source"] == "DEMO"
    assert health.status_code == 503  # unchanged contract: unreachable database -> degraded
    body = health.json()
    assert body["status"] == "degraded" and body["trading_mode"] == "disabled"
    assert body["database"] == {
        "status": "unavailable",
        "schema_revision": None,
        "error": "OperationalError",
    }
    assert "nopass" not in health.text
    assert (market_api.status_code, market_api.json()) == (503, {"detail": "database unavailable"})


@pytest.mark.parametrize(
    ("path", "content_type"),
    [
        ("/ui/css/tokens.css", "text/css"),
        ("/ui/css/components.css", "text/css"),
        ("/ui/js/app.js", "text/javascript"),
        ("/ui/js/data.js", "text/javascript"),
        ("/ui/js/format.js", "text/javascript"),
        ("/ui/js/ui.js", "text/javascript"),
        ("/ui/js/pages/overview.js", "text/javascript"),
        ("/ui/js/pages/about.js", "text/javascript"),
        ("/ui/js/pages/market.js", "text/javascript"),
        ("/ui/js/pages/research.js", "text/javascript"),
        ("/ui/js/charts.js", "text/javascript"),
        ("/ui/demo/overview.json", "application/json"),
    ],
)
def test_assets_are_served_locally_with_correct_types(path: str, content_type: str) -> None:
    resp = client.get(path)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith(content_type)
    csp = resp.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "form-action 'none'" in csp
    assert "connect-src 'self'" in csp and "http" not in csp  # nothing external allowed
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_unknown_ui_path_is_404() -> None:
    assert client.get("/ui/does-not-exist.js").status_code == 404


def _digest() -> str:
    h = hashlib.sha256()
    for p in ASSETS:
        h.update(p.relative_to(STATIC_DIR).as_posix().encode())
        h.update(p.read_bytes())
    return h.hexdigest()


@pytest.mark.parametrize("method", WRITE_METHODS)
@pytest.mark.parametrize(
    "path", ["/ui/", "/ui/index.html", "/ui/js/app.js", "/ui/demo/overview.json", "/ui/new.json"]
)
def test_write_methods_are_rejected_and_change_nothing(method: str, path: str) -> None:
    before = _digest()
    resp = client.request(method, path, content=b'{"source": "LIVE"}')
    assert resp.status_code == 405
    assert _digest() == before
    assert not (STATIC_DIR / "new.json").exists()


def test_demo_datasets_are_marked_demo() -> None:
    files = sorted((STATIC_DIR / "demo").glob("*.json"))
    assert files
    for f in files:
        meta = json.loads(f.read_text(encoding="utf-8"))["meta"]
        assert meta["source"] == "DEMO", f
        assert meta["label"] == BANNER, f


def test_demo_dates_are_historical_placeholders() -> None:
    for f in (STATIC_DIR / "demo").glob("*.json"):
        text = f.read_text(encoding="utf-8")
        years = {int(y) for y in re.findall(r"\b(19\d{2}|20\d{2})-\d{2}-\d{2}\b", text)}
        assert years and max(years) <= 2019, f  # never presented as current data


def test_overview_demo_dataset_shape() -> None:
    data = json.loads((STATIC_DIR / "demo" / "overview.json").read_text(encoding="utf-8"))
    ds = data["dataset"]
    assert ds["instrument"] == "SPY"
    assert ds["period_start"] < ds["period_end"] <= data["meta"]["illustrative_as_of"]
    kinds = {"integer", "number", "percent", "date", "datetime", "text"}
    assert data["snapshot"] and all(i["kind"] in kinds and i["label"] for i in data["snapshot"])
    assert any(i["value"] is None for i in data["snapshot"])  # the "not provided" state
    coverage = {r["area"]: r["status"] for r in data["coverage"]}
    assert coverage == {
        "Overview": "available",
        "Market Analysis": "available",
        "Research": "available",
        "Data & Operations": "planned",
        "About": "available",
    }
    assert coverage["Market Analysis"] == "available"


def test_about_page_states_sources_scope_and_limits() -> None:
    about = (STATIC_DIR / "js" / "pages" / "about.js").read_text(encoding="utf-8")
    for tag, status in (
        ("DEMO", "in-use"),
        ("LOCAL", "in-use-local"),
        ("EXTERNAL", "never"),
        ("BROKER", "never"),
    ):
        assert re.search(rf'\["{tag}", "{status}",', about), tag
    for area in ("Data & Operations",):
        assert f'["{area}", "planned", "Not implemented yet"]' in about
    assert '["Market Analysis", "available", ' in about
    assert '["Research", "available", ' in about
    assert "no statistical validation, machine learning or walk-forward" in about
    for phrase in (
        "read-only",
        "demonstration data (DEMO)",
        "stored end-of-day research data (LOCAL)",
        "It is not connected to live market data.",
        "Trade, or create, change or cancel orders of any kind",
        "Overview values are illustrative (DEMO)",
        "it is never live",
    ):
        assert phrase in about, phrase


def test_only_expected_asset_types_are_shipped() -> None:
    assert {p.suffix for p in ASSETS} <= {".html", ".css", ".js", ".json"}


FORBIDDEN_CODE = (
    r"https?://",  # no external resource or request
    r"""(?:src|href)\s*=\s*["']//""",  # protocol-relative URLs
    r"@import",
    r"XMLHttpRequest",
    r"WebSocket",
    r"EventSource",
    r"sendBeacon",
    r"<form",
    r"""method:\s*["'](?!GET)""",  # any non-GET request
    r"/api/",
    r"innerHTML|outerHTML|insertAdjacentHTML|document\.write",
    r"\balpaca\b",
    r"\bkill",
    r"\bsync\b",
    r"\bsubmit\b",
    r"\bingest\(",
    r"localStorage|sessionStorage|indexedDB",
)


# Exact literals allowed in exactly one file each: the SVG XML namespace (an identifier, never
# fetched) and the two read-only API paths of ADR-0028 (same origin).
ALLOWED_LITERALS = {
    "charts.js": ('"http://www.w3.org/2000/svg"',),
    "data.js": (
        '"/api/v1/market/summary"',
        '"/api/v1/market/series"',
        '"/api/v1/research/strategies"',
        '"/api/v1/research/backtests"',
        '"/api/v1/research/curves"',
    ),
}


@pytest.mark.parametrize("path", CODE, ids=lambda p: p.relative_to(STATIC_DIR).as_posix())
def test_assets_contain_no_external_or_operational_code(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for literal in ALLOWED_LITERALS.get(path.name, ()):
        assert text.count(literal) == 1, (path.name, literal)
        text = text.replace(literal, '""')
    for pattern in FORBIDDEN_CODE:
        assert not re.search(pattern, text, flags=re.IGNORECASE), (path.name, pattern)


def test_only_the_demo_source_fetches_and_only_demo_files() -> None:
    js = {p.name: p.read_text(encoding="utf-8") for p in STATIC_DIR.rglob("*.js")}
    assert [n for n, t in js.items() if "fetch(" in t] == ["data.js"]
    calls = re.findall(r"fetch\((.*?),", js["data.js"])
    assert calls == ["new URL(`${name}.json`", "url"]
    assert 'new URL("../demo/", import.meta.url)' in js["data.js"]
    assert 'body.meta.source !== "DEMO"' in js["data.js"]  # non-DEMO demo data is refused
    assert "new URL(path, window.location.origin)" in js["data.js"]  # API: same origin only
    assert 'body.meta.source !== "LOCAL"' in js["data.js"]  # non-LOCAL API data is refused
    assert js["data.js"].count('method: "GET"') == 2


def test_banner_policy_per_source() -> None:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    app_js = (STATIC_DIR / "js" / "app.js").read_text(encoding="utf-8")
    assert BANNER in html  # static default before any script runs: DEMO
    assert f'text: "{BANNER}"' in app_js
    assert 'text: "NOT LIVE · END-OF-DAY RESEARCH DATA"' in app_js
    assert 'market: { title: "Market Analysis", render: marketPage, source: "local" }' in app_js
    assert 'research: { title: "Research", render: researchPage, source: "local" }' in app_js
    assert 'overview: { title: "Overview", render: overviewPage }' in app_js  # stays DEMO


def test_research_page_never_ranks_and_calls_only_allowed_data() -> None:
    js = (STATIC_DIR / "js" / "pages" / "research.js").read_text(encoding="utf-8")
    assert ".sort(" not in js and "localeCompare" not in js  # API order is kept, never ranked
    assert "fetch(" not in js  # data only through data.js (ApiSource)
    loads = set(re.findall(r'(?:get|api\.load|apiUrl)\("([a-z-]+)"', js))
    assert loads == {"research-strategies", "research-backtests", "research-curves"}
    assert "Historical simulation, not evidence of future profitability." in js
    assert "Not defined for this series and period" in js  # null explained, never 0


def test_web_module_imports_only_serving_code() -> None:
    tree = ast.parse((STATIC_DIR.parent / "__init__.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert imported == {
        "mimetypes",
        "pathlib",
        "fastapi",
        "starlette.responses",
        "starlette.staticfiles",
        "starlette.types",
    }
