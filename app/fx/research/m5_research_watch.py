"""Continuous native M5 research collection. Read-only client; never a trading lab."""

import argparse
import asyncio
import hashlib
import json
import msvcrt
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from app.fx.calendar import FxCalendar
from app.fx.research.m5_validation import BAR, write_json

SYMBOLS = ("EURUSD", "EURGBP", "EURJPY")


def validate_closed(frame: pd.DataFrame, now: datetime) -> pd.DataFrame:
    if not isinstance(frame.index, pd.DatetimeIndex) or frame.index.tz is None:
        raise ValueError("timezone-aware native M5 required")
    if not frame.index.is_unique or not frame.index.is_monotonic_increasing:
        raise ValueError("unique ordered historical rows required")
    idx = frame.index.tz_convert("UTC")
    if not ((idx.minute % 5 == 0) & (idx.second == 0)).all():
        raise ValueError("M5 grid required")
    out = frame.loc[idx + BAR <= pd.Timestamp(now)].copy()
    cols = [s + "_" + f for s in ("bid", "ask") for f in ("open", "high", "low", "close")]
    values = out[cols].to_numpy(float)
    if out.empty or not np.isfinite(values).all() or (values <= 0).any():
        raise ValueError("empty/invalid raw price data")
    delta = out.index.to_series().diff().dropna()
    if delta.eq(BAR).mean() < 0.8:
        raise ValueError("cannot reinterpret M15 as native M5")
    for side in ("bid", "ask"):
        v = out[[side + "_" + f for f in ("open", "high", "low", "close")]]
        if (out[side + "_high"] < v.max(axis=1)).any() or (
            out[side + "_low"] > v.min(axis=1)
        ).any():
            raise ValueError("inconsistent OHLC")
    if (out.bid_close > out.ask_close).any():
        raise ValueError("crossed historical bid/ask")
    return out[cols]


async def collect(root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    from ib_async import IB

    from app.fx.ibkr.m5_pipeline import native_fetcher

    ib = IB()
    result = {}
    try:
        await ib.connectAsync("127.0.0.1", 4002, clientId=86, readonly=True, timeout=15)
        accounts = ib.managedAccounts()
        if len(accounts) != 1 or not accounts[0].startswith("DU"):
            raise ValueError("single demo DU required for research connection")
        fetch = native_fetcher(ib)
        for symbol in SYMBOLS:
            frame = validate_closed(await fetch(symbol, "M5"), datetime.now(UTC))
            # Atomic immutable content-addressed snapshots. Corrections keep both versions.
            raw = frame.to_parquet(index=True)
            sha = hashlib.sha256(raw).hexdigest()
            name = f"{symbol}_M5_{frame.index[-1]:%Y%m%dT%H%M}_{sha[:12]}.parquet"
            target = root / name
            if not target.exists():
                temp = target.with_suffix(".tmp")
                temp.write_bytes(raw)
                temp.replace(target)
                manifest["chunks"].append(
                    {
                        "file": name,
                        "sha256": sha,
                        "symbol": symbol,
                        "rows": len(frame),
                        "first": frame.index[0].isoformat(),
                        "last": frame.index[-1].isoformat(),
                        "collected_at": datetime.now(UTC).isoformat(),
                    }
                )
            result[symbol] = {
                "last_closed_bar": frame.index[-1].isoformat(),
                "rows": len(frame),
                "sha256": sha,
            }
        return result
    finally:
        ib.disconnect()


async def run(root: Path, once: bool = False) -> None:
    root.mkdir(parents=True, exist_ok=True)
    lock = (root / "collector.lock").open("a+b")
    lock.seek(0)
    if not lock.read(1):
        lock.write(b"0")
        lock.flush()
    lock.seek(0)
    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    path = root / "manifest.json"
    manifest = (
        json.loads(path.read_text(encoding="utf-8"))
        if path.exists()
        else {
            "schema": "m5-research-collection/1",
            "frozen_at": datetime.now(UTC).isoformat(),
            "source": "IBKR native M5 BID/ASK readonly demo client86",
            "chunks": [],
            "execution_enabled": False,
            "orders_sent": 0,
            "prospective_note": (
                "Collection is not a timestamped forecast or independent model evaluation. "
                "New immutable snapshots are quarantined from frozen training/holdout runs."
            ),
            "training_policy": (
                "Never refit identical data repeatedly. A new experiment requires a frozen "
                "protocol and sufficient completed future labels with purge/embargo. No "
                "automatic approval."
            ),
        }
    )
    write_json(path, manifest)
    try:
        while True:
            opened = FxCalendar().is_open(datetime.now(UTC))
            state = {
                "observed_at": datetime.now(UTC).isoformat(),
                "pid": os.getpid(),
                "execution_enabled": False,
                "orders_sent": 0,
                "timeframe": "M5",
                "state": "collecting_readonly" if opened else "waiting_market_open",
                "training_running": False,
            }
            if opened:
                try:
                    state["symbols"] = await collect(root, manifest)
                    write_json(path, manifest)
                    state["state"] = "native_data_collected"
                except (OSError, RuntimeError, ValueError, TimeoutError) as error:
                    state.update(state="waiting_data_connection", error=str(error))
            temp = root / "status.tmp"
            write_json(temp, state)
            temp.replace(root / "status.json")
            print(json.dumps(state), flush=True)
            if once:
                return
            await asyncio.sleep(300 if opened else 60)
    finally:
        lock.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dir", type=Path, default=Path("data/fx/training/m5-research-incoming-20261024")
    )
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.dir, args.once))
