import copy
import json
from typing import Any

import pytest

from app.trend.universe import (
    UNIVERSE_PATH,
    UniverseError,
    load_universe,
    parse_universe,
    series_rule_violations,
)


@pytest.fixture
def raw() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(UNIVERSE_PATH.read_text(encoding="utf-8"))
    return data


def test_registered_universe_is_valid() -> None:
    u = load_universe()
    assert 10 <= u.n <= 15
    assert [a.id for a in u.benchmark_only] == ["GLOBAL_EQ"]
    assert all(a.product.isin and a.product.ticker for a in u.all_series())


def test_hash_is_stable_and_content_sensitive(raw: dict[str, Any]) -> None:
    a = parse_universe(raw)
    b = parse_universe(copy.deepcopy(raw))
    assert a.sha256 == b.sha256
    changed = copy.deepcopy(raw)
    changed["assets"][0]["product"]["ter_pct"] = 0.08
    assert parse_universe(changed).sha256 != a.sha256


def test_rejects_too_few_assets(raw: dict[str, Any]) -> None:
    raw["assets"] = raw["assets"][:9]
    with pytest.raises(UniverseError, match="10-15"):
        parse_universe(raw)


def test_rejects_duplicate_symbol(raw: dict[str, Any]) -> None:
    raw["assets"][1]["research"] = copy.deepcopy(raw["assets"][0]["research"])
    with pytest.raises(UniverseError, match="duplicate research symbol"):
        parse_universe(raw)


def test_product_own_series_must_be_eur(raw: dict[str, Any]) -> None:
    raw["assets"][1]["research"]["currency"] = "USD"
    with pytest.raises(UniverseError, match="EUR line"):
        parse_universe(raw)


def test_rejects_unknown_cost_class(raw: dict[str, Any]) -> None:
    raw["assets"][0]["cost_class"] = "Z"
    with pytest.raises(UniverseError, match="cost class"):
        parse_universe(raw)


def test_sixty_forty_weights_must_sum_to_one(raw: dict[str, Any]) -> None:
    raw["benchmarks"]["sixty_forty"]["legs"][0]["weight"] = 0.7
    with pytest.raises(UniverseError, match="sum to 1"):
        parse_universe(raw)


def test_proxy_needs_its_own_ter(raw: dict[str, Any]) -> None:
    del raw["assets"][0]["research"]["ter_pct"]  # US_EQ, proxy SPY
    with pytest.raises(UniverseError, match="needs its own TER"):
        parse_universe(raw)


def test_own_series_carries_no_proxy_ter(raw: dict[str, Any]) -> None:
    raw["assets"][1]["research"]["ter_pct"] = 0.2  # EUROPE_EQ, own series
    with pytest.raises(UniverseError, match="not ter_pct"):
        parse_universe(raw)


def test_series_rule_flags_index_mismatch(raw: dict[str, Any]) -> None:
    u = parse_universe(raw)
    flagged = [v.split(":")[0] for v in series_rule_violations(u)]
    assert flagged == ["JAPAN_EQ", "EM_EQ"]  # IMI products against non-IMI US proxies
    for a in raw["assets"]:
        a["research"]["tracks"] = a["product"]["tracks"]
    assert series_rule_violations(parse_universe(raw)) == []
