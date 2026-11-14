"""Frozen offline economic-feature hypothesis. No broker or activation API."""

import argparse
import hashlib
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from app.fx.models import pair
from app.fx.research import m5_boosted_training as boosted
from app.fx.research import m5_lesson_training as lesson
from app.fx.research.backtest import EUR_TO, USD_TO
from app.fx.research.m5_validation import PROTOCOL, fee_floor

VERSION = "m5-economic-features/1.0.0"
NAMES = (
    *lesson.NAMES,
    "estimated_roundtrip_fee_r",
    "spread_r",
    "atr_stop_to_fee_floor",
    "authorized_units_fraction",
)


def economic_features(df: pd.DataFrame, f: pd.DataFrame, symbol: str) -> np.ndarray:
    """Only closed signal prices/ATR; next entry and future exits are never read."""
    stop = np.maximum(PROTOCOL.stop_atr * f.atr.to_numpy(float), fee_floor(symbol))
    quote = pair(symbol).quote
    costs = PROTOCOL.base_costs
    budget = costs.capital_eur * costs.risk_pct / 100 * EUR_TO.get(quote, 1)
    units = np.floor(np.minimum(budget / stop, costs.max_units) / 1000) * 1000
    # Conservative known-price proxy; actual future fills keep original label costs.
    price = df.ask_close.to_numpy(float)
    commission = 2 * np.maximum(
        costs.commission_min_usd * USD_TO.get(quote, 1),
        costs.commission_bps * 1e-4 * units * price,
    )
    fee_r = commission / np.maximum(units * stop, 1e-12)
    spread_r = (price - df.bid_close.to_numpy(float)) / stop
    return np.column_stack(
        (
            fee_r,
            spread_r,
            PROTOCOL.stop_atr * f.atr.to_numpy(float) / fee_floor(symbol),
            units / costs.max_units,
        )
    )


def augment(df: pd.DataFrame, f: pd.DataFrame, symbol: str, x: np.ndarray) -> np.ndarray:
    return np.column_stack((x, economic_features(df, f, symbol)))


def fit(
    role: str, variant: float, x: np.ndarray, y: np.ndarray, sample_weight: np.ndarray | None = None
) -> tuple[Any, dict[str, Any]]:
    model, artifact = boosted.fitted_model(
        role,
        variant,
        x,
        y,
        sample_weight,
        feature_names=NAMES,
    )
    artifact["schema"] = VERSION
    return model, artifact


def protocol(source: Path) -> dict[str, Any]:
    p = boosted.protocol(source)
    p.update(
        schema=VERSION,
        features=NAMES,
        model_trainer_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        boosted_library_sha256=hashlib.sha256(Path(boosted.__file__).read_bytes()).hexdigest(),
        hypothesis=(
            "Expose ex-ante economic viability to the learner: roundtrip commission/R, "
            "spread/R, ATR-stop/fee-floor, authorized rounded units/max-units. "
            "Current closed ASK price is a conservative commission proxy, not a fill. "
            "Labels, costs, thresholds, six fixed depth3/5 candidates and gates unchanged."
        ),
        selection="Six candidates; all pairs pooled with original global simulation limits.",
        evaluation_limit=(
            "2024/2025 reused exploratory development, not independent confirmation. "
            "No 2026 holdout access. Further holdout/joint integration required."
        ),
        predeclared_feature_rules="Four features only; no future-price or target-derived inputs.",
    )
    return p


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data", type=Path, default=Path("data/fx/ibkr-m5"))
    p.add_argument("--out", type=Path, required=True)
    p.add_argument(
        "--source", type=Path, default=Path("C:/Users/guilo/Downloads/Aula 02 - semana vip.pdf")
    )
    a = p.parse_args()
    # Freeze all dependency sources before reading market data or fitting weights.
    declared = protocol(a.source)
    if a.out.exists():
        raise ValueError("frozen output exists; do not overwrite")
    lesson.run(
        a.data,
        a.out,
        a.source,
        fit_function=fit,
        score_function=boosted.score,
        protocol_function=lambda _: declared,
        feature_function=augment,
        source_files={
            "model_trainer.py": Path(__file__),
            "boosted_library.py": Path(boosted.__file__),
        },
    )
