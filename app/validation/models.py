"""Typed validation outputs. Every result carries its full provenance."""

from dataclasses import asdict, dataclass, field
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class Provenance:
    validation_version: str
    config_fingerprint: str
    method: str
    block_length: int
    n_resamples: int
    confidence: float
    seed: int
    slice_name: str
    slice_first_session: pd.Timestamp
    slice_last_session: pd.Timestamp
    sessions: int
    cost_scenario: str
    backtest_fingerprint: str


@dataclass(frozen=True)
class MetricInterval:
    """Point estimate on the observed sample + bootstrap percentile interval."""

    subject: str  # strategy / benchmark id (suffix "__gross" for the zero-cost run)
    metric: str
    point: float
    lower: float
    upper: float
    valid_resamples: int
    note: str
    provenance: Provenance


@dataclass(frozen=True)
class Comparison:
    """Paired (time-aligned) difference `a - b` of a metric.

    family = "formal": member of the pre-declared test family (p-values, Holm and BH adjusted).
    family = "descriptive": interval only; no p-value, no significance claim.
    """

    comparison_id: str
    family: str
    a: str
    b: str
    metric: str
    null_hypothesis: str
    estimate: float
    lower: float
    upper: float
    p_value: float
    p_holm: float
    p_bh: float
    family_size: int
    note: str
    provenance: Provenance


@dataclass(frozen=True)
class CostSensitivityRow:
    slice_name: str
    scenario: str
    subject: str
    cumulative_return: float
    cagr: float
    sharpe: float
    max_drawdown: float
    completed_trades: float
    orders: float
    annualized_turnover: float
    total_costs: float
    backtest_fingerprint: str


@dataclass(frozen=True)
class RedundancyRow:
    slice_name: str
    a: str
    b: str
    position_agreement: float
    daily_return_correlation: float
    sessions_with_different_returns: int
    sessions: int


@dataclass(frozen=True)
class ConcentrationRow:
    slice_name: str
    subject: str
    cumulative_return: float
    cumulative_return_without_best_10_days: float
    log_return_share_of_top_1pct_days: float
    top5_winners_share_of_gross_profit: float


@dataclass
class ValidationReport:
    validation_version: str
    config_fingerprint: str
    config: dict[str, Any]
    intervals: list[MetricInterval] = field(default_factory=list)
    comparisons: list[Comparison] = field(default_factory=list)
    cost_sensitivity: list[CostSensitivityRow] = field(default_factory=list)
    redundancy: list[RedundancyRow] = field(default_factory=list)
    concentration: list[ConcentrationRow] = field(default_factory=list)
    timings_ms: dict[str, float] = field(default_factory=dict)

    def frames(self) -> dict[str, pd.DataFrame]:
        def flat(rows: list[Any]) -> pd.DataFrame:
            out = []
            for r in rows:
                d = asdict(r)
                prov = d.pop("provenance", None)
                out.append({**d, **({f"prov_{k}": v for k, v in prov.items()} if prov else {})})
            return pd.DataFrame(out)

        return {
            "intervals": flat(self.intervals),
            "comparisons": flat(self.comparisons),
            "cost_sensitivity": flat(self.cost_sensitivity),
            "redundancy": flat(self.redundancy),
            "concentration": flat(self.concentration),
        }
