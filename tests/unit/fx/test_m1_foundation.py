"""Native M1 development data and isolated simulation; all clients are fakes."""
import asyncio
import importlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS

import pandas as pd
import pytest
from pydantic import ValidationError

from app.fx.config import FxConfig
from app.fx.environment import Environment

START = datetime(2025, 1, 2, tzinfo=UTC)
END = START + timedelta(days=1)


class Client:
    def __init__(self, accounts=None, empty=False):
        self.accounts = ["DU123456"] if accounts is None else accounts
        self.empty = empty
        self.connects, self.requests = [], []
        self.disconnected = False

    async def connectAsync(self, *args, **kwargs):
        self.connects.append((args, kwargs))

    def managedAccounts(self):
        return self.accounts

    async def reqHistoricalDataAsync(self, contract, end, duration, size, side, *args, **kwargs):
        self.requests.append((end, duration, size, side))
        if self.empty:
            return []
        base = end - timedelta(days=1) + timedelta(hours=12)
        offset = 0.0001 if side == "ASK" else 0
        return [NS(date=base + timedelta(minutes=i), open=1.1+offset, high=1.11+offset,
                   low=1.09+offset, close=1.1+offset) for i in range(10)]

    def disconnect(self):
        self.disconnected = True


def module():
    return importlib.import_module("app.fx.research.m1_history")


def test_m1_config_is_simulation_only():
    assert FxConfig(timeframe="M1").environment is Environment.SIMULATION
    with pytest.raises(ValidationError, match="SIMULATION"):
        FxConfig(timeframe="M1", environment=Environment.DEMO)


def test_native_m1_collection_resume_hashes_and_pacing(tmp_path):
    m = module()
    client, pauses = Client(), []
    async def pause(seconds):
        pauses.append(seconds)
    manifest = asyncio.run(m.collect(tmp_path, START, END, symbols=("EURUSD",),
                                     client=client, pause=pause))
    assert manifest["complete"] and len(manifest["chunks"]) == 2
    assert client.connects[0][1]["readonly"] and client.connects[0][1]["clientId"] == 90
    assert [r[1:] for r in client.requests] == [("1 D", "1 min", "BID"), ("1 D", "1 min", "ASK")]
    assert pauses == [11] and client.disconnected
    frame = m.load_pair(tmp_path, "EURUSD")
    assert len(frame) == 10 and (frame.ask_close > frame.bid_close).all()
    resumed = Client()
    asyncio.run(m.collect(tmp_path, START, END, symbols=("EURUSD",), client=resumed, pause=pause))
    assert resumed.requests == []
    target = tmp_path / manifest["chunks"][0]["file"]
    target.write_bytes(target.read_bytes() + b"tampered")
    rejected = Client()
    with pytest.raises(ValueError, match="hash"):
        asyncio.run(m.collect(tmp_path, START, END, symbols=("EURUSD",), client=rejected, pause=pause))
    assert rejected.connects == []


@pytest.mark.parametrize("accounts", [[], ["U123"], ["DU1", "DU2"]])
def test_m1_refuses_nonunique_paper_identity(tmp_path, accounts):
    client = Client(accounts)
    with pytest.raises(Exception):
        asyncio.run(module().collect(tmp_path, START, END, symbols=("EURUSD",), client=client))
    assert client.requests == [] and client.disconnected


def test_m1_refuses_reserved2026_before_connect_or_output(tmp_path):
    root, client = tmp_path / "not-created", Client()
    with pytest.raises(ValueError, match="2026"):
        asyncio.run(module().collect(root, START, datetime(2026, 1, 2, tzinfo=UTC), client=client))
    assert client.connects == [] and not root.exists()


def test_m1_never_reinterprets_m5_as_m1():
    data = pd.DataFrame({"ts": pd.date_range(START, periods=10, freq="5min"),
                         "open": 1.1, "high": 1.2, "low": 1.0, "close": 1.1})
    with pytest.raises(ValueError, match="native M1"):
        module().validate_frame(data, START, END)


def test_m1_empty_response_is_not_blindly_retried(tmp_path):
    client = Client(empty=True)
    with pytest.raises(ValueError, match="empty"):
        asyncio.run(module().collect(tmp_path, START, END, symbols=("EURUSD",), client=client))
    assert len(client.requests) == 1 and client.disconnected
