"""Synthetic regression tests: executable stops, realized drawdown and cutoff reads."""

import pandas as pd
import pytest

from app.fx.research import m5_validation as m5
from app.fx.research.backtest import run_backtest, summarize
from tests.unit.fx.test_m5_validation import m5_frame
from tests.unit.fx.test_research_backtest import FLAT, FREE, bars, signals


@pytest.mark.parametrize("direction", [1, -1])
def test_legacy_entry_spread_beyond_stop(direction):
    df = bars([FLAT] * 3)
    trade = run_backtest(df, signals(df, 0, direction, 0.0001, 0.003), "EURUSD", costs=FREE).iloc[0]
    assert trade["exit"] == pytest.approx(df["bid_open" if direction > 0 else "ask_open"].iloc[1])
    assert trade["r_gross"] == pytest.approx(-2.0)


@pytest.mark.parametrize("direction", [1, -1])
def test_m5_entry_spread_beyond_stop(direction):
    df = m5_frame(days=1, spread=0.0002)
    a = m5._arrays(df, m5.PROTOCOL)
    entry = df["ask_open" if direction > 0 else "bid_open"].iloc[1]
    _, price, reason = m5._exit(
        a, direction, 1, entry - direction * 0.0001, entry + direction * 0.003, 2, 0.0
    )
    assert price == pytest.approx(df["bid_open" if direction > 0 else "ask_open"].iloc[1])
    assert reason == "stop_gap"


def test_realized_drawdown_orders_closes_and_groups_ties():
    trades = pd.DataFrame(
        {
            "r_net": [5.0, -5.0, -5.0],
            "r_cost": 0.0,
            "bars": 1,
            "exit_ts": pd.to_datetime(["2024-01-02", "2024-01-01", "2024-01-03"], utc=True),
        }
    )
    assert summarize(trades)["max_dd_r"] == pytest.approx(5.0)
    assert summarize(trades)["realized_max_dd_r"] == pytest.approx(5.0)
    trades["exit_ts"] = pd.Timestamp("2024-01-01", tz="UTC")
    assert summarize(trades)["max_dd_r"] == pytest.approx(5.0)
    assert summarize(trades.iloc[::-1])["max_dd_r"] == pytest.approx(5.0)


def test_cutoff_loader_never_materializes_later_prices(tmp_path, monkeypatch):
    df = m5_frame(days=2)
    directory = tmp_path / "EURUSD"
    directory.mkdir()
    for side in ("BID", "ASK"):
        part = pd.DataFrame(
            {k: df[f"{side.lower()}_{k}"] for k in ("open", "high", "low", "close")}
        ).reset_index(names="ts")
        part.to_parquet(directory / f"{side}_M5_20240306.parquet")
        later = part.assign(ts=part.ts + pd.Timedelta(days=365))
        later.to_parquet(directory / f"{side}_M5_20250306.parquet")
    cutoff = df.index[200]
    original = pd.read_parquet

    def guarded(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        assert (result.ts < cutoff).all(), "holdout prices materialized"
        return result

    monkeypatch.setattr(pd, "read_parquet", guarded)
    assert m5.load_m5(tmp_path, "EURUSD", cutoff).index.max() < cutoff


def test_manifest_counts_rows_without_decoding_prices(tmp_path, monkeypatch):
    import hashlib
    import json

    directory = tmp_path / "EURUSD"
    directory.mkdir()
    chunks = []
    for side in ("BID", "ASK"):
        file = directory / f"{side}_M5_20240306.parquet"
        pd.DataFrame(
            {"ts": pd.date_range("2024-03-04", periods=10, freq="5min", tz="UTC"), "close": 1.0}
        ).to_parquet(file)
        chunks.append(
            {
                "file": str(file),
                "pair": "EURUSD",
                "sha256": hashlib.sha256(file.read_bytes()).hexdigest(),
                "rows": 10,
            }
        )
    (tmp_path / "manifest.json").write_text(
        json.dumps({"complete": True, "upsampled": False, "bar_size": "5 mins", "chunks": chunks})
    )

    def forbid(*args, **kwargs):
        pytest.fail("manifest decoded price rows")

    monkeypatch.setattr(pd, "read_parquet", forbid)
    m5.check_manifest(tmp_path, pairs=("EURUSD",))
