import copy
import json
from typing import Any

import pytest

from app.trend.universe import UNIVERSE_PATH, UniverseError, load_universe, parse_universe


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
