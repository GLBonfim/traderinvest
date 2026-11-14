import numpy as np
import pandas as pd
import pytest

from app.fx.research.m5_crosspair_training import sample_excluding
from app.fx.research.m5_lesson_training import NAMES


def data():
    idx = pd.date_range("2023-12-28", periods=1200, freq="5min", tz="UTC")
    frames = {s: pd.DataFrame(index=idx) for s in ("EURUSD", "EURGBP", "EURJPY")}
    xs = {s: np.full((len(idx), len(NAMES)), i + 1.0) for i, s in enumerate(frames)}
    ys = {
        s: pd.DataFrame({"net_r": i + 1.0, "label_end": idx + pd.Timedelta(minutes=485)}, index=idx)
        for i, s in enumerate(frames)
    }
    context = {s: pd.DataFrame({"h4_available": 1.0, "h4_bias": -1.0}, index=idx) for s in frames}
    masks = {s: np.ones(len(idx), bool) for s in frames}
    return frames, xs, ys, context, masks


def test_cross_market_training_never_uses_its_target_pair():
    frames, xs, ys, context, masks = data()
    before = pd.Timestamp("2024-01-01", tz="UTC")
    x, y, w, training = sample_excluding("EURJPY", before, frames, xs, ys, context, masks)
    assert training == ["EURGBP", "EURUSD"] and set(np.unique(x)) == {1.0, 2.0}
    xs["EURJPY"][:] = 999
    ys["EURJPY"]["net_r"] = 999
    xx, yy, ww, _ = sample_excluding("EURJPY", before, frames, xs, ys, context, masks)
    np.testing.assert_array_equal(x, xx)
    np.testing.assert_array_equal(y, yy)
    np.testing.assert_array_equal(w, ww)


def test_cross_market_purges_future_horizon_and24h_embargo():
    frames, xs, ys, context, masks = data()
    before = pd.Timestamp("2024-01-01", tz="UTC")
    for target in ys.values():
        target.loc[target.label_end >= before - pd.Timedelta(hours=24), "net_r"] = 9999.0
    _, y, _, _ = sample_excluding("EURUSD", before, frames, xs, ys, context, masks)
    assert len(y) >= 300 and 9999.0 not in y


def test_cross_market_rejects_unknown_target_and_insufficient_markets():
    frames, xs, ys, context, masks = data()
    before = pd.Timestamp("2024-01-01", tz="UTC")
    with pytest.raises(ValueError):
        sample_excluding("USDJPY", before, frames, xs, ys, context, masks)
    frames.pop("EURJPY")
    with pytest.raises(ValueError):
        sample_excluding("EURUSD", before, frames, xs, ys, context, masks)
