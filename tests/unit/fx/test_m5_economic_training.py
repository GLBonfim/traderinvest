import numpy as np
import pandas as pd

from app.fx.research.m5_boosted_training import portable_predict
from app.fx.research.m5_economic_training import NAMES, economic_features, fit


def test_economic_inputs_causal_and_sensitive_to_spread():
    idx = pd.date_range("2024-01-01", periods=10, freq="5min", tz="UTC")
    df = pd.DataFrame({"bid_close": 1.1, "ask_close": 1.1001}, index=idx)
    f = pd.DataFrame({"atr": 0.001}, index=idx)
    before = economic_features(df, f, "EURUSD")
    changed = df.copy()
    changed.loc[idx[5] :, "ask_close"] = 1.101
    after = economic_features(changed, f, "EURUSD")
    np.testing.assert_array_equal(before[:5], after[:5])
    assert (after[5:, 1] > before[5:, 1]).all()
    assert (before[:, 0] > 0).all()
    assert ((before[:, 3] >= 0) & (before[:, 3] <= 1)).all()


def test_new_dimensions_export_parity_and_offline_flags():
    rng = np.random.default_rng(7)
    x = rng.normal(size=(400, len(NAMES)))
    y = x[:, -1] * 0.3 + rng.normal(size=400)
    model, artifact = fit("reversao", 3, x, y)
    assert len(artifact["feature_names"]) == 51
    assert artifact["export_max_abs_error"] < 1e-10
    assert not artifact["execution_enabled"]
    assert np.isfinite(portable_predict(model, x)).all()
