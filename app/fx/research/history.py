"""Download M15 BID and ASK bars from the IBKR paper session (read-only) into parquet chunks.

`python -m app.fx.research.history --years 6` — resumable: each (pair, side, chunk) is a file;
existing chunks are skipped. A manifest records source, request, row counts and SHA-256 of
every chunk. The connection is read-only and refused unless the session is a paper account.

Why BID and ASK instead of MIDPOINT: fills are simulated at the real side of the book (buy at
the ask, sell at the bid) and the spread of every bar is known, so costs are not guessed.
"""

import argparse
import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from app.fx.ibkr.paper import assert_paper_accounts, mask
from app.fx.models import PAIRS

DATA_DIR = Path("data/fx/ibkr")
SIDES = ("BID", "ASK")
CHUNK_MONTHS = 6


def chunk_ends(end: datetime, years: int) -> list[datetime]:
    """End datetimes of consecutive 6-month chunks going back `years` years."""
    out = []
    cur = end
    for _ in range(years * 12 // CHUNK_MONTHS):
        out.append(cur)
        cur = cur - timedelta(days=182)
    return out


def chunk_path(root: Path, symbol: str, side: str, end: datetime) -> Path:
    return root / symbol / f"{side}_M15_{end:%Y%m%d}.parquet"


def bars_to_frame(bars: list[Any]) -> pd.DataFrame:
    df = pd.DataFrame(
        {
            "ts": [b.date for b in bars],
            "open": [float(b.open) for b in bars],
            "high": [float(b.high) for b in bars],
            "low": [float(b.low) for b in bars],
            "close": [float(b.close) for b in bars],
        }
    )
    if not df.empty:
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def download(
    root: Path, symbols: list[str], years: int, port: int, client_id: int, parallel: int
) -> dict[str, Any]:
    from ib_async import IB, Forex

    ib = IB()
    await ib.connectAsync("127.0.0.1", port, clientId=client_id, timeout=15, readonly=True)
    manifest: dict[str, Any] = {"chunks": []}
    try:
        accounts = assert_paper_accounts(ib.managedAccounts())
        manifest["account"] = [mask(a) for a in accounts]
        end = datetime(2026, 10, 17, tzinfo=UTC)
        ends = chunk_ends(end, years)
        sem = asyncio.Semaphore(parallel)

        async def one(symbol: str, side: str, chunk_end: datetime) -> None:
            path = chunk_path(root, symbol, side, chunk_end)
            if path.exists():
                manifest["chunks"].append(
                    {
                        "pair": symbol,
                        "side": side,
                        "end": chunk_end.isoformat(),
                        "file": str(path),
                        "rows": len(pd.read_parquet(path)),
                        "sha256": sha256(path),
                        "cached": True,
                    }
                )
                return
            async with sem:
                contract = Forex(symbol)
                await ib.qualifyContractsAsync(contract)
                for attempt in range(3):
                    bars = await ib.reqHistoricalDataAsync(
                        contract,
                        chunk_end,
                        f"{CHUNK_MONTHS} M",
                        "15 mins",
                        side,
                        False,
                        formatDate=2,
                        timeout=300,
                    )
                    if bars:
                        break
                    await asyncio.sleep(10 * (attempt + 1))
                df = bars_to_frame(bars)
                path.parent.mkdir(parents=True, exist_ok=True)
                df.to_parquet(path, index=False)
                manifest["chunks"].append(
                    {
                        "pair": symbol,
                        "side": side,
                        "end": chunk_end.isoformat(),
                        "file": str(path),
                        "rows": len(df),
                        "sha256": sha256(path),
                        "first": df["ts"].min().isoformat() if len(df) else None,
                        "last": df["ts"].max().isoformat() if len(df) else None,
                        "downloaded_at": datetime.now(UTC).isoformat(),
                    }
                )
                print(f"{symbol} {side} ..{chunk_end:%Y-%m-%d}: {len(df)} bars", flush=True)
                await asyncio.sleep(1)

        await asyncio.gather(*(one(s, side, e) for s in symbols for side in SIDES for e in ends))
    finally:
        ib.disconnect()
    manifest.update(
        {
            "source": "IBKR TWS API (IB Gateway, paper session, read-only)",
            "contract": "Forex IDEALPRO",
            "bar_size": "15 mins",
            "what_to_show": list(SIDES),
            "use_rth": False,
            "timezone": "UTC (formatDate=2, bar open time)",
            "years": years,
            "written_at": datetime.now(UTC).isoformat(),
        }
    )
    (root / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return manifest


# Conservative minimum spread per pair (pips). IBKR's BID and ASK bars often close at the
# same price (median close spread 0.0 pips on EURUSD), which is not an executable spread; every
# bar is widened symmetrically around its mid to at least these values.
SPREAD_FLOOR_PIPS: dict[str, float] = {
    "EURUSD": 0.3,
    "GBPUSD": 0.6,
    "USDJPY": 0.4,
    "AUDUSD": 0.5,
    "USDCHF": 0.6,
    "USDCAD": 0.6,
    "NZDUSD": 0.8,
    "EURGBP": 0.6,
    "EURJPY": 0.8,
}


def with_spread_floor(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Widens bid/ask of every OHLC field to at least the pair's floor (mid unchanged)."""
    from app.fx.models import pair

    floor = SPREAD_FLOOR_PIPS.get(symbol, 1.0) * pair(symbol).pip_size
    out = df.copy()
    for k in ("open", "high", "low", "close"):
        bid, ask = out[f"bid_{k}"], out[f"ask_{k}"]
        mid = (bid + ask) / 2
        half = ((ask - bid).clip(lower=floor)) / 2
        out[f"bid_{k}"], out[f"ask_{k}"] = mid - half, mid + half
    return out


def resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Bid/ask OHLC to a longer bar (e.g. "1h"), bar-open labelled, empty bars dropped."""
    agg: dict[str, str] = {}
    for side in ("bid", "ask"):
        agg |= {
            f"{side}_open": "first",
            f"{side}_high": "max",
            f"{side}_low": "min",
            f"{side}_close": "last",
        }
    return df.resample(rule, label="left", closed="left").agg(agg).dropna()


def load_pair(root: Path, symbol: str) -> pd.DataFrame:
    """BID/ASK OHLC joined on the bar open time, deduplicated, sorted (UTC index)."""
    sides = {}
    for side in SIDES:
        files = sorted((root / symbol).glob(f"{side}_M15_*.parquet"))
        if not files:
            raise FileNotFoundError(f"no {side} data for {symbol} under {root}")
        df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
        df = df.drop_duplicates("ts").set_index("ts").sort_index()
        sides[side] = df.add_prefix(f"{side.lower()}_")
    joined = sides["BID"].join(sides["ASK"], how="inner")
    return joined


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="traderinvest-fx-history")
    ap.add_argument("--years", type=int, default=6)
    ap.add_argument("--port", type=int, default=4002)
    ap.add_argument("--client-id", type=int, default=31)
    ap.add_argument("--parallel", type=int, default=3)
    ap.add_argument("--pairs", nargs="*", default=list(PAIRS))
    ap.add_argument("--out", type=Path, default=DATA_DIR)
    a = ap.parse_args(argv)
    m = asyncio.run(download(a.out, a.pairs, a.years, a.port, a.client_id, a.parallel))
    print(f"chunks: {len(m['chunks'])} · rows: {sum(c['rows'] for c in m['chunks'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
