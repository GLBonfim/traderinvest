"""Cross-validate the Technical Indicator Engine against TA-Lib on stored SPY bars.

TA-Lib is NOT a project dependency. Run in an ephemeral environment:

    uv run --with TA-Lib python scripts/crossvalidate_indicators.py

This is a numerical-correctness check only (formulas, seeding, warm-up), not a tuning step.
"""

import sys

import numpy as np
import pandas as pd
import talib

from app.candles.loader import load_closed_bars
from app.database.session import get_session_factory
from app.indicators import IndicatorConfig, IndicatorEngine

TOL = 1e-9  # values are equal if |diff| <= TOL * max(1, |reference|)
BURN_IN = 300  # bars after which recursive-initialisation differences must have vanished


def compare(name: str, ours: pd.Series, ref: np.ndarray, explained: str = "") -> dict[str, object]:
    ours_v = ours.to_numpy(dtype=np.float64)
    first_ours = int(np.flatnonzero(~np.isnan(ours_v))[0])
    first_ref = int(np.flatnonzero(~np.isnan(ref))[0])
    both = ~np.isnan(ours_v) & ~np.isnan(ref)
    diff = np.abs(ours_v - ref)
    within = diff <= TOL * np.maximum(1.0, np.abs(ref))
    late = both & (np.arange(len(ref)) >= BURN_IN)
    if explained == "offset":  # constant offset: the difference itself must not vary
        delta = (ours_v - ref)[both]
        ok = bool(np.all(np.abs(delta - delta[0]) <= TOL * np.maximum(1.0, np.abs(ref[both]))))
    elif explained == "init":  # recursive seed differs: must agree after the burn-in
        ok = bool(within[late].all())
    else:  # values must agree wherever both exist; start may differ only if `explained`
        ok = bool(within[both].all()) and (first_ours == first_ref or explained == "start")
    return {
        "indicator": name,
        "first_ours": first_ours,
        "first_talib": first_ref,
        "max_abs_diff": float(diff[both].max()),
        f"max_abs_diff_t>={BURN_IN}": float(diff[late].max()),
        "status": ("match" if not explained else f"explained:{explained}") if ok else "MISMATCH",
    }


def main() -> int:
    cfg = IndicatorConfig()
    with get_session_factory()() as s:
        _, bars = load_closed_bars(s, "SPY")
    v = IndicatorEngine(cfg).analyze(bars).values
    h, lo, c, vol = (bars[x].to_numpy(dtype=np.float64) for x in ("high", "low", "close", "volume"))

    macd, macd_sig, macd_hist = talib.MACD(c, cfg.macd_fast, cfg.macd_slow, cfg.macd_signal)
    k, d = talib.STOCH(h, lo, c, cfg.stoch_period, cfg.stoch_k_smoothing, 0, cfg.stoch_d_period, 0)
    up, mid, low_band = talib.BBANDS(c, cfg.bollinger_period, cfg.bollinger_k, cfg.bollinger_k, 0)
    n = cfg.realized_vol_period
    log_ret = np.r_[np.nan, np.log(c[1:] / c[:-1])]
    rv_ref = (
        talib.STDDEV(log_ret, n, 1.0) * np.sqrt(n / (n - 1)) * np.sqrt(cfg.trading_days_per_year)
    )

    rows = [
        *(compare(f"sma_{p}", v[f"sma_{p}"], talib.SMA(c, p)) for p in cfg.sma_periods),
        *(compare(f"ema_{p}", v[f"ema_{p}"], talib.EMA(c, p)) for p in cfg.ema_periods),
        compare("macd", v["macd"], macd, "init"),
        compare("macd_signal", v["macd_signal"], macd_sig, "init"),
        compare("macd_hist", v["macd_hist"], macd_hist, "init"),
        compare("rsi_14", v["rsi_14"], talib.RSI(c, cfg.rsi_period)),
        compare("stoch_k", v["stoch_k"], k, "start"),
        compare("stoch_d", v["stoch_d"], d),
        compare("roc_12", v["roc_12"], talib.ROC(c, cfg.roc_period)),
        compare("true_range", v["true_range"], talib.TRANGE(h, lo, c)),
        compare("atr_14", v["atr_14"], talib.ATR(h, lo, c, cfg.atr_period)),
        compare("bb_upper", v["bb_upper"], up),
        compare("bb_middle", v["bb_middle"], mid),
        compare("bb_lower", v["bb_lower"], low_band),
        compare("realized_vol_20", v["realized_vol_20"], rv_ref),
        compare("obv", v["obv"], talib.OBV(c, vol), "offset"),
    ]
    report = pd.DataFrame(rows)
    with pd.option_context("display.width", 200, "display.max_columns", 20):
        print(f"TA-Lib {talib.__version__}; {len(bars)} SPY daily bars")
        print(report.to_string(index=False))
    print(f"OBV offset (talib - ours) = volume[0] = {vol[0]:,.0f}")
    return 0 if (report["status"] != "MISMATCH").all() else 1


if __name__ == "__main__":
    sys.exit(main())
