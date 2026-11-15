"""Pre-registered universe (``registry/universe.json``): loading and structural validation.

The file is the single source of truth for exposures, research series, executable products,
cost classes and commission inputs. This module only parses and checks it; it never edits it.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.integrity import canonical_json

REGISTRY_DIR = Path(__file__).resolve().parent / "registry"
UNIVERSE_PATH = REGISTRY_DIR / "universe.json"
SCHEMA = "trend-universe/1"
CALENDARS = frozenset({"XNYS", "XETR"})
CURRENCIES = frozenset({"EUR", "USD"})
KINDS = frozenset({"proxy", "product_own"})
MIN_ASSETS, MAX_ASSETS = 10, 15


class UniverseError(ValueError):
    """The universe registry is structurally invalid."""


@dataclass(frozen=True)
class SeriesSpec:
    source: str
    symbol: str
    currency: str
    calendar: str
    kind: str
    tracks: str
    ter_pct: float | None = None  # a proxy fund's own TER (condition c); None for own series


@dataclass(frozen=True)
class ProductSpec:
    isin: str
    name: str
    ticker: str
    ter_pct: float
    distribution: str
    inception: str
    structure: str
    tracks: str


@dataclass(frozen=True)
class AssetSpec:
    id: str
    research: SeriesSpec
    product: ProductSpec
    cost_class: str


@dataclass(frozen=True)
class Universe:
    assets: tuple[AssetSpec, ...]
    benchmark_only: tuple[AssetSpec, ...]
    raw: dict[str, Any]
    sha256: str

    @property
    def n(self) -> int:
        return len(self.assets)

    def all_series(self) -> tuple[AssetSpec, ...]:
        """Universe assets followed by benchmark-only legs (no duplicates)."""
        return self.assets + self.benchmark_only


def _series(d: dict[str, Any], where: str) -> SeriesSpec:
    try:
        s = SeriesSpec(
            source=d["source"],
            symbol=d["symbol"],
            currency=d["currency"],
            calendar=d["calendar"],
            kind=d["kind"],
            tracks=d["tracks"],
            ter_pct=None if d.get("ter_pct") is None else float(d["ter_pct"]),
        )
    except KeyError as exc:
        raise UniverseError(f"{where}: research field missing: {exc}") from exc
    if s.source != "yahoo":
        raise UniverseError(f"{where}: unsupported source {s.source!r}")
    if s.calendar not in CALENDARS:
        raise UniverseError(f"{where}: unsupported calendar {s.calendar!r}")
    if s.currency not in CURRENCIES:
        raise UniverseError(f"{where}: unsupported currency {s.currency!r}")
    if s.kind not in KINDS:
        raise UniverseError(f"{where}: unsupported kind {s.kind!r}")
    if s.kind == "product_own" and s.currency != "EUR":
        raise UniverseError(f"{where}: a product's own series must be the EUR line")
    if s.kind == "proxy" and (s.ter_pct is None or not 0 <= s.ter_pct < 2):
        raise UniverseError(f"{where}: a proxy needs its own TER (ter_pct)")
    if s.kind == "product_own" and s.ter_pct is not None:
        raise UniverseError(f"{where}: an own series carries the product TER, not ter_pct")
    return s


def _product(d: dict[str, Any], where: str) -> ProductSpec:
    try:
        p = ProductSpec(
            isin=d["isin"],
            name=d["name"],
            ticker=d["ticker"],
            ter_pct=float(d["ter_pct"]),
            distribution=d["distribution"],
            inception=d["inception"],
            structure=d["structure"],
            tracks=d["tracks"],
        )
    except KeyError as exc:
        raise UniverseError(f"{where}: product field missing: {exc}") from exc
    if len(p.isin) != 12 or not p.isin[:2].isalpha():
        raise UniverseError(f"{where}: malformed ISIN {p.isin!r}")
    if not 0 <= p.ter_pct < 2:
        raise UniverseError(f"{where}: implausible TER {p.ter_pct}")
    if p.distribution not in ("accumulating", "distributing"):
        raise UniverseError(f"{where}: unknown distribution policy {p.distribution!r}")
    return p


def _asset(d: dict[str, Any], classes: dict[str, Any]) -> AssetSpec:
    where = str(d.get("id", "?"))
    cost_class = d.get("cost_class")
    if cost_class not in classes:
        raise UniverseError(f"{where}: unknown cost class {cost_class!r}")
    return AssetSpec(
        id=where,
        research=_series(d.get("research", {}), where),
        product=_product(d.get("product", {}), where),
        cost_class=str(cost_class),
    )


def parse_universe(raw: dict[str, Any]) -> Universe:
    """Validate a registry dict; raises `UniverseError` on any structural problem."""
    if raw.get("schema") != SCHEMA:
        raise UniverseError(f"unexpected schema {raw.get('schema')!r}")
    classes = {k: v for k, v in raw.get("cost_classes", {}).items() if len(k) == 1}
    assets = tuple(_asset(a, classes) for a in raw.get("assets", []))
    if not MIN_ASSETS <= len(assets) <= MAX_ASSETS:
        raise UniverseError(
            f"universe must hold {MIN_ASSETS}-{MAX_ASSETS} assets, got {len(assets)}"
        )
    ids = [a.id for a in assets]
    if len(set(ids)) != len(ids):
        raise UniverseError("duplicate asset id")
    legs = raw.get("benchmarks", {}).get("sixty_forty", {}).get("legs", [])
    if abs(sum(float(leg["weight"]) for leg in legs) - 1.0) > 1e-12:
        raise UniverseError("60/40 leg weights must sum to 1")
    extra: list[AssetSpec] = []
    for leg in legs:
        ref = leg.get("same_as_asset")
        if ref is not None:
            if ref not in ids:
                raise UniverseError(f"60/40 leg refers to unknown asset {ref!r}")
            continue
        if leg.get("id") in ids:
            raise UniverseError(f"benchmark leg id {leg.get('id')!r} clashes with an asset")
        extra.append(_asset(leg, classes))
    symbols = [a.research.symbol for a in assets + tuple(extra)]
    if len(set(symbols)) != len(symbols):
        raise UniverseError("duplicate research symbol")
    commission = raw.get("commission", {})
    for key in ("rate_pct", "minimum_eur", "surveyed_on"):
        if key not in commission:
            raise UniverseError(f"commission field missing: {key}")
    digest = hashlib.sha256(canonical_json(raw).encode()).hexdigest()
    return Universe(assets=assets, benchmark_only=tuple(extra), raw=raw, sha256=digest)


def series_rule_violations(universe: Universe) -> list[str]:
    """Owner condition (a): a proxy must track exactly the executable product's index.

    An asset listed here must be excluded or given a product that matches; it is never kept with
    a "similar" index. The replay (T2) refuses to run while this list is not empty.
    """
    return [
        f"{a.id}: proxy {a.research.symbol} tracks {a.research.tracks!r}, "
        f"product {a.product.ticker} tracks {a.product.tracks!r}"
        for a in universe.all_series()
        if a.research.kind == "proxy" and a.research.tracks != a.product.tracks
    ]


def load_universe(path: Path = UNIVERSE_PATH) -> Universe:
    return parse_universe(json.loads(path.read_text(encoding="utf-8")))
