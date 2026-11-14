"""EUR cost model per scenario (pure; design v2 section 8).

Commission, half-spread and slippage are charged on each fill; TER is inside the price series
(see `ter_adjustment_pct`). Scenario `zero` has no trading cost, `double` doubles the
commission (rate, minimum and maximum), spread and slippage.
"""

from dataclasses import dataclass
from typing import Any

SCENARIOS = ("zero", "realistic", "double")


@dataclass(frozen=True)
class CostModel:
    commission_rate: float  # fraction of traded value
    commission_min: float  # EUR per order
    commission_max: float | None  # EUR per order
    half_spread: dict[str, float]  # fraction per side, per asset
    slippage: dict[str, float]  # fraction per side, per asset

    def commission(self, value: float) -> float:
        if value <= 0 or (self.commission_rate == 0 and self.commission_min == 0):
            return 0.0
        fee = max(self.commission_rate * value, self.commission_min)
        if self.commission_max is not None:
            fee = min(fee, self.commission_max)
        return fee

    def side_cost(self, asset: str) -> float:
        return self.half_spread[asset] + self.slippage[asset]


def cost_model(universe_raw: dict[str, Any], tiers: dict[str, str], scenario: str) -> CostModel:
    """Build the model from the registry: `tiers` maps asset id -> cost class."""
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario {scenario!r}")
    factor = {"zero": 0.0, "realistic": 1.0, "double": 2.0}[scenario]
    classes = universe_raw["cost_classes"]
    commission = universe_raw["commission"]
    maximum = commission.get("maximum_eur")
    return CostModel(
        commission_rate=factor * commission["rate_pct"] / 100.0,
        commission_min=factor * commission["minimum_eur"],
        commission_max=None if maximum is None else factor * maximum,
        half_spread={a: factor * classes[c]["half_spread_pct"] / 100.0 for a, c in tiers.items()},
        slippage={a: factor * classes[c]["slippage_pct"] / 100.0 for a, c in tiers.items()},
    )


def ter_adjustment_pct(kind: str, ter_eu: float, ter_proxy: float | None, scenario: str) -> float:
    """Annual adjustment (in % per year, added to the series' return) per section 8.2.

    Own series contain TER_eu; a US proxy contains its own TER_us; an index contains none.
    """
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario {scenario!r}")
    if kind == "product_own":
        return {"zero": ter_eu, "realistic": 0.0, "double": -ter_eu}[scenario]
    if kind == "proxy":
        if ter_proxy is None:
            raise ValueError("a proxy needs its own TER")
        return {
            "zero": ter_proxy,
            "realistic": ter_proxy - ter_eu,
            "double": ter_proxy - 2 * ter_eu,
        }[scenario]
    if kind == "index":
        return {"zero": 0.0, "realistic": -ter_eu, "double": -2 * ter_eu}[scenario]
    raise ValueError(f"unknown series kind {kind!r}")
