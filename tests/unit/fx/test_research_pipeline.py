"""Research pipeline: spread floor, H1 resampling, bootstrap test, neighbour smoothing and the
full-system replay on a small synthetic bid/ask history (no data files, no network)."""

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from app.fx.agents.signals import features, mid_frame, regime_frame, trend_signals
from app.fx.calendar import FxCalendar
from app.fx.models import DataOrigin
from app.fx.proposals import AgentRole
from app.fx.research.history import resample, with_spread_floor
from app.fx.research.replay import HistoricalMarket, run_replay
from app.fx.research.study import neighbour_smoothed, one_sided_bootstrap_p

START = datetime(2026, 9, 7, 0, 0, tzinfo=UTC)  # a Monday


def history(
    symbol: str = "EURUSD", days: int = 12, seed: int = 1, spread: float = 0.0
) -> pd.DataFrame:
    """Bid/ask M15 bars on weekdays only (weekends are absent, like real FX data)."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(START, periods=days * 96, freq="15min", tz="UTC")
    idx = idx[idx.dayofweek < 5]
    mid = (1.10 if symbol != "USDJPY" else 150.0) * np.exp(
        np.cumsum(0.0002 * np.sin(np.arange(len(idx)) / 30) + rng.normal(0, 0.0005, len(idx)))
    )
    o = np.concatenate([[mid[0]], mid[:-1]])
    hi, lo = np.maximum(o, mid) * 1.0003, np.minimum(o, mid) * 0.9997
    out = {}
    for side, sign in (("bid", -1), ("ask", 1)):
        for k, v in (("open", o), ("high", hi), ("low", lo), ("close", mid)):
            out[f"{side}_{k}"] = v + sign * spread / 2
    return pd.DataFrame(out, index=idx)


def test_spread_floor_widens_around_the_mid() -> None:
    df = history(spread=0.0)
    out = with_spread_floor(df, "EURUSD")
    spread = out["ask_close"] - out["bid_close"]
    assert spread.min() == pytest.approx(0.00003)  # 0.3 pip floor
    mid_before = (df["ask_close"] + df["bid_close"]) / 2
    mid_after = (out["ask_close"] + out["bid_close"]) / 2
    np.testing.assert_allclose(mid_before, mid_after)
    wide = with_spread_floor(history(spread=0.0005), "EURUSD")  # already wider: unchanged
    assert (wide["ask_close"] - wide["bid_close"]).min() == pytest.approx(0.0005)


def test_resample_to_h1() -> None:
    df = history(days=2)
    h1 = resample(df, "1h")
    first = df.iloc[:4]
    row = h1.iloc[0]
    assert row["bid_open"] == first["bid_open"].iloc[0]
    assert row["bid_high"] == first["bid_high"].max() and row["ask_low"] == first["ask_low"].min()
    assert row["ask_close"] == first["ask_close"].iloc[-1]
    assert len(h1) == len(df) // 4


def test_one_sided_bootstrap_p() -> None:
    rng = np.random.default_rng(0)
    assert one_sided_bootstrap_p(rng.normal(0.5, 1, 400)) < 0.01
    assert one_sided_bootstrap_p(rng.normal(-0.2, 1, 400)) == 1.0
    assert np.isnan(one_sided_bootstrap_p(np.array([1.0, 2.0])))


def test_neighbour_smoothing_prefers_plateaus() -> None:
    scores = {(1.0,): 1.0, (2.0,): 5.0, (3.0,): -5.0, (4.0,): 3.0, (5.0,): 3.5}
    smooth = neighbour_smoothed(scores, ["x"])
    assert smooth[(2.0,)] < 5.0  # an isolated spike is discounted
    assert max(smooth, key=lambda c: smooth[c]) == (5.0,)


def test_historical_market_ohlc_path_and_bars() -> None:
    df = history(days=3)
    m = HistoricalMarket({"EURUSD": df}, START, START + timedelta(days=1), FxCalendar())
    assert m.origin is DataOrigin.HISTORICAL
    seen = []
    for _ in range(4):
        quotes, closed = m.step()
        seen.append(quotes["EURUSD"].bid)
    first = df.iloc[0]
    rising = (first["bid_close"] + first["ask_close"]) >= (first["bid_open"] + first["ask_open"])
    expected = (
        [first["bid_open"], first["bid_low"], first["bid_high"], first["bid_close"]]
        if rising
        else [first["bid_open"], first["bid_high"], first["bid_low"], first["bid_close"]]
    )
    assert seen == pytest.approx(expected)
    assert len(closed) == 1 and closed[0][2].close == pytest.approx(
        (first["bid_close"] + first["ask_close"]) / 2
    )
    assert len(m.bars("EURUSD", "M15", 600)) == 1 and m.now == START + timedelta(minutes=15)


def test_full_system_replay_runs_the_real_product() -> None:
    data = {
        s: with_spread_floor(history(s, days=26, seed=i), s)
        for i, s in enumerate(("EURUSD", "GBPUSD", "USDJPY"))
    }
    feats = {
        s: features(mid_frame(df), df["ask_close"] - df["bid_close"]) for s, df in data.items()
    }
    regimes = {s: regime_frame(f) for s, f in feats.items()}
    sig = {AgentRole.TREND: {s: trend_signals(f) for s, f in feats.items()}}
    start = START + timedelta(days=12)
    res = run_replay(
        data,
        start,
        START + timedelta(days=26),
        sig,
        {AgentRole.TREND: 48},
        feats,
        regimes,
        frozenset({"trend"}),
    )
    assert res["journal_chain_ok"] is True
    assert res["max_entries_one_day"] <= 5
    assert res["trading_days"] > 5 and res["entries_filled"] == res["positions_closed"]
    none = run_replay(
        data,
        start,
        START + timedelta(days=26),
        sig,
        {AgentRole.TREND: 48},
        feats,
        regimes,
        frozenset(),
    )
    assert none["entries_filled"] == 0 and none["return_pct"] == pytest.approx(0.0)
