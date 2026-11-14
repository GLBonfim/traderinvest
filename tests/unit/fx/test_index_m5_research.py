from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from app.fx.research.index_m5_research import examples, split_masks, study, validate


def frame(n=1800, freq="5min"):
    idx = pd.date_range("2025-01-01", periods=n, freq=freq, tz="UTC")
    c = 100 + np.sin(np.arange(n) / 20)
    return pd.DataFrame({"open": c, "high": c + 1, "low": c - 1, "close": c}, index=idx)


def test_native_and_closed_only():
    raw = frame(20)
    out = validate(raw, raw.index[-1].to_pydatetime())
    assert len(out) == 19
    with pytest.raises(ValueError, match="adjacency"):
        validate(frame(20, "15min"), datetime(2026, 1, 1, tzinfo=UTC))


def test_feature_causality_and_purge():
    raw = frame()
    x, y, ends = examples(raw)
    changed = raw.copy()
    changed.iloc[1000:, :] *= 2
    np.testing.assert_array_equal(x.iloc[:1000], examples(changed)[0].iloc[:1000])
    train, test = split_masks(raw.index, x, y, ends)
    assert train.any() and test.any()
    assert ends[train].max() < raw.index[test].min() - pd.Timedelta(hours=24)


def test_no_approval_even_if_directional_models_complete():
    result = study(frame())
    assert len(result["models"]) == 3
    assert not result["approved"] and not result["execution_enabled"]
    assert result["pnl"] is None


def test_missing_contract_is_recorded_and_does_not_abort(tmp_path, monkeypatch):
    import asyncio
    import sys
    from types import SimpleNamespace

    from app.fx.research import index_m5_research as module

    class FakeIB:
        async def connectAsync(self, *args, **kwargs):  # noqa: N802
            assert kwargs["readonly"] is True

        def managedAccounts(self):  # noqa: N802
            return ["DU-TEST"]

        async def qualifyContractsAsync(self, contract):  # noqa: N802
            return [None]

        def disconnect(self):
            pass

    async def no_sleep(*args):
        pass

    monkeypatch.setitem(
        sys.modules,
        "ib_async",
        SimpleNamespace(
            IB=FakeIB,
            Index=lambda s, e, c: SimpleNamespace(symbol=s, exchange=e, secType="IND"),
            util=SimpleNamespace(),
        ),
    )
    monkeypatch.setattr(module, "SEARCH", ())
    monkeypatch.setattr(module, "CATALOG", (("MISSING", "TEST", "USD"),))
    monkeypatch.setattr(module.asyncio, "sleep", no_sleep)
    result = asyncio.run(module.collect(tmp_path))
    assert result["chunks"] == []
    assert len(result["unavailable"]) == 1
    assert "unique qualified" in result["unavailable"][0]["error"]
