"""Read-only, resumable genuine M5 BID/ASK history; never upsample M15."""

import argparse
import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from app.fx.ibkr.paper import assert_paper_accounts, mask
from app.fx.research.history import bars_to_frame

SYMBOLS = ("EURUSD", "EURGBP", "EURJPY")


def ends() -> list[datetime]:
    start = datetime(2023, 1, 1, tzinfo=UTC)
    end = datetime(2026, 10, 17, tzinfo=UTC)
    out = []
    stamp = start + timedelta(days=7)
    while stamp < end:
        out.append(stamp)
        stamp += timedelta(days=7)
    return [*out, end]


def validate(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        raise ValueError("empty M5 response")
    df = df.copy()
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.sort_values("ts").drop_duplicates("ts")
    if not ((df.ts.dt.minute % 5 == 0) & (df.ts.dt.second == 0)).all():
        raise ValueError("response timestamps are not M5 aligned")
    if df.ts.diff().eq(pd.Timedelta(minutes=5)).sum() < len(df) * 0.8:
        raise ValueError("response does not contain genuine consecutive M5 bars")
    values = df[["open", "high", "low", "close"]]
    if not np.isfinite(values.to_numpy(float)).all() or not (values > 0).all().all():
        raise ValueError("invalid price")
    if not ((df.high >= values.max(axis=1)) & (df.low <= values.min(axis=1))).all():
        raise ValueError("inconsistent OHLC")
    return df


async def download(root: Path, client_id: int = 64, parallel: int = 6) -> dict[str, Any]:
    import msvcrt

    root.mkdir(parents=True, exist_ok=True)
    lock = (root / "download.lock").open("a+b")
    lock.seek(0)
    if not lock.read(1):
        lock.write(b"0")
        lock.flush()
    lock.seek(0)
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        return await _download(root, client_id, parallel)
    finally:
        lock.close()


async def _download(root: Path, client_id: int, parallel: int) -> dict[str, Any]:
    from ib_async import IB, Forex

    root.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "source": "IBKR historical BID and ASK, read-only paper API",
        "bar_size": "5 mins",
        "timezone": "UTC bar-open",
        "chunks": [],
        "requested_start": "2023-01-01",
        "requested_end": "2026-10-17",
        "request_duration": "1 W",
        "upsampled": False,
    }
    ib = IB()
    await ib.connectAsync("127.0.0.1", 4002, clientId=client_id, readonly=True, timeout=20)
    semaphore = asyncio.Semaphore(parallel)

    def save() -> None:
        manifest["updated_at"] = datetime.now(UTC).isoformat()
        tmp = root / "manifest.tmp"
        tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        tmp.replace(root / "manifest.json")

    def record(path: Path, sym: str, side: str, df: pd.DataFrame) -> None:
        manifest["chunks"].append(
            {
                "pair": sym,
                "side": side,
                "file": str(path),
                "rows": len(df),
                "first": df.ts.min().isoformat(),
                "last": df.ts.max().isoformat(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
        save()

    try:
        manifest["account"] = [mask(a) for a in assert_paper_accounts(ib.managedAccounts())]
        contracts = {}
        for sym in SYMBOLS:
            contracts[sym] = Forex(sym)
            await ib.qualifyContractsAsync(contracts[sym])
        expected = {
            root / sym / f"{side}_M5_{e:%Y%m%d}.parquet"
            for e in ends()
            for sym in SYMBOLS
            for side in ("BID", "ASK")
        }
        for path in sorted(root.glob("*/*_M5_*.parquet")):
            if path not in expected:  # preserve already downloaded overlapping quarterly files
                record(
                    path, path.parent.name, path.name.split("_")[0], validate(pd.read_parquet(path))
                )

        async def one(sym: str, side: str, end: datetime) -> None:
            path = root / sym / f"{side}_M5_{end:%Y%m%d}.parquet"
            async with semaphore:
                if path.exists():
                    df = validate(pd.read_parquet(path))
                else:
                    # Deliberate overlap at the final partial quarter; loader deduplicates.
                    bars: list[Any] = []
                    for attempt in range(3):
                        bars = await ib.reqHistoricalDataAsync(
                            contracts[sym],
                            end,
                            "1 W",
                            "5 mins",
                            side,
                            False,
                            formatDate=2,
                            timeout=120,
                        )
                        if bars:
                            break
                        print(
                            f"Retry historical data {sym} {side} {end}: {attempt + 1}", flush=True
                        )
                        await asyncio.sleep(20)
                    df = validate(bars_to_frame(bars))
                    path.parent.mkdir(parents=True, exist_ok=True)
                    tmp = path.with_suffix(".tmp.parquet")
                    df.to_parquet(tmp, index=False)
                    tmp.replace(path)
                    await asyncio.sleep(1)
                record(path, sym, side, df)
                print(f"{sym} {side} {end:%Y%m%d}: {len(df)} M5 bars", flush=True)

        await asyncio.gather(
            *(one(s, side, e) for e in sorted(ends()) for s in SYMBOLS for side in ("BID", "ASK"))
        )
        manifest["complete"] = True
        save()
        return manifest
    finally:
        ib.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("data/fx/ibkr-m5"))
    parser.add_argument("--parallel", type=int, choices=range(1, 7), default=6)
    args = parser.parse_args()
    asyncio.run(download(args.data, parallel=args.parallel))


if __name__ == "__main__":
    main()
