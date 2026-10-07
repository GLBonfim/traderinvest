"""Execution timing, rejected look-ahead execution models, temporal slicing, point-in-time
guarantees (prefix, future mutation incl. adj_close, leaky controls) and determinism."""

import dataclasses
from datetime import date

import numpy as np
import pandas as pd
import pytest

from app.backtest.config import BacktestConfig
from app.backtest.engine import BENCHMARK_PRICE, BENCHMARK_TOTAL, Backtester, run_suite
from app.backtest.execution import ExecutionModel, FillQuote
from app.backtest.models import BacktestResult
from tests.unit.backtest.helpers import FREE, bars_from, states
from tests.unit.strategies.helpers import session_walk, small_engine

L, F = "LONG", "FLAT"


def walk(n: int, seed: int, start: date = date(2015, 1, 2)) -> pd.DataFrame:
    b = session_walk(n, seed, start)
    return b.assign(adj_close=b["close"] * np.linspace(0.9, 1.0, n))


# ── execution timing ──


def test_fills_at_next_session_open_across_holiday_weekend_early_close() -> None:
    bars = bars_from(opens=[100, 101, 102, 103, 104, 105], closes=[100, 101, 102, 103, 104, 105])
    # sessions: 07-01, 07-02, 07-03 (early close), 07-05 (after July 4th), 07-08 (Monday), 07-09
    st = states(bars, [F, F, L, L, F, F])  # LONG decided 07-03, FLAT decided 07-08
    r = Backtester(FREE).run_states(bars, st, strategy_id="x")
    buy, sell = r.fills.iloc[0], r.fills.iloc[1]
    assert buy["signal_observed_at"] == pd.Timestamp("2024-07-03 17:00", tz="UTC")  # 13:00 ET
    assert buy["ts"] == pd.Timestamp("2024-07-05 13:30", tz="UTC") and buy["price"] == 103
    assert sell["ts"] == pd.Timestamp("2024-07-09 13:30", tz="UTC") and sell["price"] == 105
    assert (r.fills["ts"] > r.fills["signal_observed_at"]).all()


def test_weekend_gap_uses_monday_open() -> None:
    bars = bars_from(opens=[10, 11, 12, 13], closes=[10, 11, 12, 13], start=date(2024, 7, 11))
    r = Backtester(FREE).run_states(bars, states(bars, [F, L, L, L]), strategy_id="x")
    assert r.fills.iloc[0]["ts"] == pd.Timestamp("2024-07-15 13:30", tz="UTC")  # Fri decision


class _AtClose(ExecutionModel):
    name = "bad_close"

    def __init__(self, closes: np.ndarray) -> None:
        self.closes = closes

    def quote(self, decision_index: int, opens: np.ndarray) -> FillQuote | None:
        return FillQuote(decision_index, float(self.closes[decision_index]))


class _AtNextHigh(ExecutionModel):
    name = "bad_high"

    def __init__(self, highs: np.ndarray) -> None:
        self.highs = highs

    def quote(self, decision_index: int, opens: np.ndarray) -> FillQuote | None:
        return FillQuote(decision_index + 1, float(self.highs[decision_index + 1]))


class _SkipOne(ExecutionModel):
    name = "bad_skip"

    def quote(self, decision_index: int, opens: np.ndarray) -> FillQuote | None:
        return FillQuote(decision_index + 2, float(opens[decision_index + 2]))


@pytest.mark.parametrize("model_factory", [
    lambda b: _AtClose(b["close"].to_numpy()),
    lambda b: _AtNextHigh(b["high"].to_numpy()),
    lambda b: _SkipOne(),
])  # fmt: skip
def test_lookahead_execution_models_are_rejected(model_factory) -> None:  # type: ignore[no-untyped-def]
    bars = bars_from(opens=[100, 101, 102, 103, 104, 105], closes=[100, 102, 103, 104, 105, 106])
    with pytest.raises(ValueError):
        Backtester(FREE, model=model_factory(bars)).run_states(
            bars, states(bars, [L] * 6), strategy_id="x"
        )


def test_missing_session_in_data_is_rejected() -> None:
    full = bars_from(opens=[1, 2, 3, 4, 5], closes=[1, 2, 3, 4, 5])
    bars = full.drop(full.index[2])  # session 3 missing from the data
    with pytest.raises(ValueError, match="next exchange session"):
        Backtester(FREE).run_states(bars, states(bars, [F, L, L, L]), strategy_id="x")


# ── temporal slicing ──


def test_window_start_and_end() -> None:
    bars = walk(200, seed=1)
    cfg = dataclasses.replace(
        FREE, start=bars.index[100].date(), end=bars.index[150].date(), period_label="test"
    )
    res = run_suite(bars, config=cfg, strategy_engine=small_engine())
    for r in res.values():
        assert r.equity.index[0] == bars.index[100] and r.equity.index[-1] == bars.index[150]
        assert r.meta["period_label"] == "test"
        if len(r.fills):
            assert r.fills["ts"].min() >= bars.index[100]
    # Decision observed before the window executes at the window's first open (when it differs).
    bh = res["buy_and_hold"]
    assert bh.fills.iloc[0]["ts"] == bars.index[100]


def test_data_after_end_is_never_used() -> None:
    bars = walk(200, seed=2)
    cfg = dataclasses.replace(FREE, end=bars.index[150].date())
    base = run_suite(bars, config=cfg, strategy_engine=small_engine())
    altered = bars.copy()
    altered.iloc[151:, :4] *= 3.0
    altered.iloc[151:, altered.columns.get_loc("adj_close")] *= 0.1
    again = run_suite(altered, config=cfg, strategy_engine=small_engine())
    for sid in base:
        pd.testing.assert_frame_equal(base[sid].equity, again[sid].equity)


def test_same_strategy_states_regardless_of_window() -> None:
    bars = walk(220, seed=3)
    a = run_suite(
        bars,
        config=dataclasses.replace(FREE, start=bars.index[120].date()),
        strategy_engine=small_engine(),
    )
    b = run_suite(bars, config=FREE, strategy_engine=small_engine())
    for sid in ("sma_trend", "rsi_momentum"):
        pd.testing.assert_series_equal(a[sid].equity["state"], b[sid].equity["state"].iloc[120:])


# ── point-in-time ──


def _known_by(r: BacktestResult, ts: pd.Timestamp) -> dict[str, pd.DataFrame]:
    trades = r.trades[r.trades["exit_time"] <= ts] if len(r.trades) else r.trades
    return {
        "equity": r.equity.loc[:ts],
        "fills": r.fills[r.fills["ts"] <= ts].reset_index(drop=True),
        "trades": trades.reset_index(drop=True),
    }


def _assert_same_until(
    x: dict[str, BacktestResult], y: dict[str, BacktestResult], ts: pd.Timestamp
) -> None:
    for sid in y:
        kx, ky = _known_by(x[sid], ts), _known_by(y[sid], ts)
        for key in kx:
            pd.testing.assert_frame_equal(kx[key], ky[key], check_dtype=False, obj=f"{sid}.{key}")


def test_prefix_equals_full_history() -> None:
    bars = walk(150, seed=4)
    full = run_suite(bars, config=FREE, strategy_engine=small_engine())
    for i in range(30, len(bars), 6):
        prefix = run_suite(bars.iloc[: i + 1], config=FREE, strategy_engine=small_engine())
        _assert_same_until(prefix, full, bars.index[i])


def test_future_mutation_never_changes_the_past() -> None:
    base = walk(200, seed=5)
    cut = 140
    reference = run_suite(base, config=FREE, strategy_engine=small_engine())
    rng = np.random.default_rng(6)
    for _ in range(3):
        altered = base.copy()
        fut = altered.iloc[cut + 1 :]
        altered.iloc[cut + 1 :, :4] = fut.iloc[:, :4].to_numpy() * rng.uniform(
            0.5, 1.5, (len(fut), 1)
        )
        altered["high"] = altered[["open", "high", "low", "close"]].max(axis=1)
        altered["low"] = altered[["open", "high", "low", "close"]].min(axis=1)
        altered.iloc[cut + 1 :, altered.columns.get_loc("volume")] = rng.integers(
            0, 10**7, len(fut)
        )
        altered.iloc[cut + 1 :, altered.columns.get_loc("adj_close")] = rng.uniform(
            1, 500, len(fut)
        )
        result = run_suite(altered, config=FREE, strategy_engine=small_engine())
        _assert_same_until(result, reference, base.index[cut])


def test_metrics_through_t_match_prefix_metrics() -> None:
    bars = walk(160, seed=7)
    full = run_suite(bars, config=FREE, strategy_engine=small_engine())
    prefix = run_suite(bars.iloc[:121], config=FREE, strategy_engine=small_engine())
    for sid in ("sma_trend", BENCHMARK_PRICE, BENCHMARK_TOTAL):
        pd.testing.assert_series_equal(
            prefix[sid].equity["drawdown"], full[sid].equity["drawdown"].iloc[:121]
        )
        assert prefix[sid].metrics["max_drawdown"] == full[sid].equity["drawdown"].iloc[:121].min()


def test_future_dependent_states_are_detected() -> None:
    """Control: a rule that is LONG when TOMORROW closes higher must break prefix equality."""
    bars = walk(120, seed=8)

    def peeking(b: pd.DataFrame) -> pd.Series:
        nxt = b["close"].shift(-1)
        return pd.Series(np.where(nxt > b["close"], L, F), index=b.index)

    bt = Backtester(FREE)
    full = bt.run_states(bars, peeking(bars), strategy_id="peek")
    mismatches = 0
    for i in range(20, 120, 5):
        part = bt.run_states(bars.iloc[: i + 1], peeking(bars.iloc[: i + 1]), strategy_id="peek")
        if not part.equity.equals(full.equity.loc[: bars.index[i]]):
            mismatches += 1
    assert mismatches > 0


# ── determinism / config ──


def test_deterministic() -> None:
    bars = walk(150, seed=9)
    x = run_suite(bars, config=BacktestConfig(), strategy_engine=small_engine())
    y = run_suite(bars.copy(), config=BacktestConfig(), strategy_engine=small_engine())
    for sid in x:
        pd.testing.assert_frame_equal(x[sid].equity, y[sid].equity)
        pd.testing.assert_frame_equal(x[sid].trades, y[sid].trades)
        assert x[sid].metrics.keys() == y[sid].metrics.keys()
        for k, v in x[sid].metrics.items():
            assert (v == y[sid].metrics[k]) or (v != v and y[sid].metrics[k] != y[sid].metrics[k])


@pytest.mark.parametrize(
    "kw", [{"initial_capital": 0}, {"spread_bps": -1}, {"execution": "close"},
           {"start": date(2020, 1, 2), "end": date(2019, 1, 2)}],
)  # fmt: skip
def test_invalid_config(kw: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        BacktestConfig(**kw)  # type: ignore[arg-type]


def test_fingerprint_changes_with_assumptions() -> None:
    assert BacktestConfig().fingerprint() == BacktestConfig().fingerprint()
    assert BacktestConfig(slippage_bps=3).fingerprint() != BacktestConfig().fingerprint()
