import asyncio
import json

import pandas as pd
import pytest

from app.fx.ibkr import m5_pipeline
from app.fx.research.m5_research_watch import collect, validate_closed
from tests.unit.fx.ibkr_fakes import NOW
from tests.unit.fx.test_m5_runtime import native


def test_collector_rejects_m15_and_excludes_forming():
    frame = native()
    assert validate_closed(frame, NOW).index[-1] + pd.Timedelta(minutes=5) <= pd.Timestamp(NOW)
    with pytest.raises(ValueError):
        validate_closed(frame.iloc[::3], NOW)
    frame.iloc[-5, 0] = float("nan")
    with pytest.raises(ValueError):
        validate_closed(frame, NOW)


def test_collection_connects_readonly_and_saves_immutable_deduplicated_snapshots(
    tmp_path, monkeypatch
):
    import ib_async

    calls = []

    class ReadOnlyIB:
        async def connectAsync(self, *args, **kwargs):  # noqa: N802
            calls.append(kwargs)

        def managedAccounts(self):  # noqa: N802
            return ["DU00000014"]

        def disconnect(self):
            pass

    async def fetch(symbol, timeframe):
        assert timeframe == "M5"
        return native()

    monkeypatch.setattr(ib_async, "IB", ReadOnlyIB)
    monkeypatch.setattr(m5_pipeline, "native_fetcher", lambda ib: fetch)
    manifest = {"chunks": []}
    result = asyncio.run(collect(tmp_path, manifest))
    assert len(result) == 3 and len(manifest["chunks"]) == 3
    asyncio.run(collect(tmp_path, manifest))
    assert len(manifest["chunks"]) == 3
    assert all(k["readonly"] is True and k["clientId"] == 86 for k in calls)
    assert len(list(tmp_path.glob("*.parquet"))) == 3
    assert "DU00000014" not in json.dumps(manifest)


def test_closed_market_does_not_connect_or_train(tmp_path, monkeypatch):
    from app.fx.research import m5_research_watch as watcher

    monkeypatch.setattr(watcher.FxCalendar, "is_open", lambda self, now: False)

    async def forbidden(*args):
        raise AssertionError("market closed: no network needed")

    monkeypatch.setattr(watcher, "collect", forbidden)
    asyncio.run(watcher.run(tmp_path, once=True))
    state = json.loads((tmp_path / "status.json").read_text())
    assert state["state"] == "waiting_market_open"
    assert not state["training_running"] and state["orders_sent"] == 0
