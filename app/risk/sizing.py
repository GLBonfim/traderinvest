"""Deterministic position sizing: requested LONG -> target exposure (fraction of equity)."""

import math

from app.risk.config import SizingSpec


def size_exposure(
    spec: SizingSpec, *, close: float, realized_vol: float, atr: float
) -> tuple[float, str]:
    """Exposure for a LONG request, and a reason code. Uses only values known at the close of T.

    Undefined or non-positive risk inputs never become a default size: the exposure is 0 with
    reason `risk_input_undefined`.
    """
    if spec.method == "full":
        return spec.max_exposure, "full"
    if spec.method == "fixed_fraction":
        return min(spec.fraction, spec.max_exposure), "fixed_fraction"
    if spec.method == "volatility_target":
        if not (math.isfinite(realized_vol) and realized_vol > 0):
            return 0.0, "risk_input_undefined"
        return min(spec.target_volatility / realized_vol, spec.max_exposure), "volatility_target"
    if spec.method == "atr_risk":
        if not (math.isfinite(atr) and atr > 0 and math.isfinite(close) and close > 0):
            return 0.0, "risk_input_undefined"
        exposure = spec.atr_risk_budget * close / (spec.atr_stop_multiple * atr)
        return min(exposure, spec.max_exposure), "atr_risk"
    raise ValueError(f"unknown sizing method {spec.method!r}")
