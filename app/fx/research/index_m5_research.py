"""Read-only native M5 IND collection and directional study; no profit approval."""

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
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

VERSION = "index-m5-study/1.0.1"
BAR = pd.Timedelta(minutes=5)
CATALOG = (
    ("SPX", "CBOE", "USD"),
    ("NDX", "NASDAQ", "USD"),
    ("INDU", "NYSE", "USD"),
    ("RUT", "RUSSELL", "USD"),
    ("DAX", "EUREX", "EUR"),
    ("ESTX50", "EUREX", "EUR"),
    ("FTSE", "LSE", "GBP"),
    ("CAC40", "MONEP", "EUR"),
    ("N225", "OSE.JPN", "JPY"),
    ("HSI", "HKFE", "HKD"),
    ("ASX200", "ASX", "AUD"),
    ("SMI", "SOFFEX", "CHF"),
)
SEARCH = (
    "SPX",
    "NASDAQ",
    "DOW",
    "RUSSELL",
    "DAX",
    "STOXX",
    "FTSE",
    "CAC",
    "NIKKEI",
    "HANG SENG",
    "ASX",
    "SMI",
)


def save(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, default=str), encoding="utf-8")


def status(root: Path, state: str, current: str | None, count: int) -> None:
    save(
        root / "status.json",
        {
            "pid": os.getpid(),
            "observed_at": datetime.now(UTC).isoformat(),
            "state": state,
            "index_atual": current,
            "collected_count": count,
            "execution_enabled": False,
            "approved": False,
        },
    )


def validate(frame: pd.DataFrame, now: datetime) -> pd.DataFrame:
    if not isinstance(frame.index, pd.DatetimeIndex) or frame.index.tz is None:
        raise ValueError("timezone-aware M5 required")
    if not frame.index.is_unique or not frame.index.is_monotonic_increasing:
        raise ValueError("ordered unique rows required")
    idx = frame.index.tz_convert("UTC")
    if not ((idx.minute % 5 == 0) & (idx.second == 0) & (idx.microsecond == 0)).all():
        raise ValueError("native M5 grid required")
    out = frame.loc[idx + BAR <= pd.Timestamp(now), ["open", "high", "low", "close"]].copy()
    out.index = pd.DatetimeIndex(out.index).tz_convert("UTC")
    values = out.to_numpy(float)
    if len(out) < 2 or not np.isfinite(values).all() or (values <= 0).any():
        raise ValueError("insufficient or invalid OHLC")
    if (out.high < out.max(axis=1)).any() or (out.low > out.min(axis=1)).any():
        raise ValueError("inconsistent OHLC")
    if out.index.to_series().diff().eq(BAR).mean() < 0.8:
        raise ValueError("insufficient native five-minute adjacency")
    return out


def examples(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    c = frame.close
    x = pd.DataFrame(index=frame.index)
    for lag in (1, 3, 12, 48):
        x[f"return_{lag}"] = np.log(c / c.shift(lag))
    x["range"] = (frame.high - frame.low) / c
    x["body"] = (c - frame.open) / c
    x["volatility"] = pd.Series(np.log(c / c.shift()), index=frame.index).rolling(48).std()
    starts = pd.Series(frame.index, index=frame.index)
    ends = starts.shift(-96) + BAR
    complete = ends.notna()
    y = (c.shift(-96) > c).astype(float).where(complete)
    return x, y, ends


def split_masks(
    index: pd.DatetimeIndex, x: pd.DataFrame, y: pd.Series, ends: pd.Series
) -> tuple[np.ndarray, np.ndarray]:
    cutoff = index[int(len(index) * 0.7)]
    valid = np.isfinite(x.to_numpy()).all(axis=1) & y.notna().to_numpy()
    return valid & (ends < cutoff - pd.Timedelta(hours=24)).to_numpy(), valid & (index >= cutoff)


def study(frame: pd.DataFrame) -> dict[str, Any]:
    x, y, ends = examples(frame)
    train, test = split_masks(pd.DatetimeIndex(frame.index), x, y, ends)
    result: dict[str, Any] = {
        "training_rows": int(train.sum()),
        "evaluation_rows": int(test.sum()),
        "approved": False,
        "execution_enabled": False,
        "models": [],
        "pnl": None,
    }
    if train.sum() < 500 or test.sum() < 100 or y[train].nunique() < 2:
        result["state"] = "insufficient_complete_labels"
        return result
    models = {
        "reversao": make_pipeline(
            StandardScaler(), LogisticRegression(C=0.1, max_iter=500, random_state=7)
        ),
        "continuacao": RandomForestClassifier(
            n_estimators=100, max_depth=3, min_samples_leaf=50, random_state=7, n_jobs=2
        ),
        "impulso": RandomForestClassifier(
            n_estimators=100, max_depth=5, min_samples_leaf=50, random_state=7, n_jobs=2
        ),
    }
    for role, model in models.items():
        model.fit(x[train], y[train])
        predictions = model.predict(x[test])
        result["models"].append(
            {
                "study_role": role,
                "accuracy": float((predictions == y[test]).mean()),
                "majority_baseline_accuracy": float(max(y[test].mean(), 1 - y[test].mean())),
                "predictions": len(predictions),
            }
        )
    result["state"] = "descriptive_directional_study_complete"
    result["warning"] = (
        "Overlapping labels are dependent; accuracy is not profitability or approval."
    )
    return result


async def collect(root: Path, prior: Path | None = None) -> dict[str, Any]:
    from ib_async import IB, Index, util

    ib: Any = IB()
    manifest: dict[str, Any] = {
        "schema": VERSION,
        "chunks": [],
        "unavailable": [],
        "execution_enabled": False,
        "approved": False,
        "orders_sent": 0,
    }
    if prior is not None:
        previous = json.loads((prior / "manifest.json").read_text(encoding="utf-8"))
        for chunk in previous["chunks"]:
            name = chunk["file"]
            if Path(name).name != name:
                raise ValueError("invalid prior snapshot path")
            content = (prior / name).read_bytes()
            if hashlib.sha256(content).hexdigest() != chunk["sha256"]:
                raise ValueError("prior snapshot hash mismatch")
            (root / name).write_bytes(content)
            manifest["chunks"].append({**chunk, "reused_from": str(prior)})
    try:
        await ib.connectAsync("127.0.0.1", 4002, clientId=89, readonly=True, timeout=15)
        accounts = ib.managedAccounts()
        if len(accounts) != 1 or not accounts[0].startswith("DU"):
            raise ValueError("single DU demo account required")
        candidates: list[Any] = [Index(s, e, c) for s, e, c in CATALOG]
        for term in SEARCH:
            status(root, "discovering", term, len(manifest["chunks"]))
            try:
                descriptions = await asyncio.wait_for(ib.reqMatchingSymbolsAsync(term), timeout=20)
                candidates.extend(
                    d.contract
                    for d in (descriptions or [])
                    if d.contract is not None and d.contract.secType == "IND"
                )
            except (TimeoutError, RuntimeError, ValueError) as error:
                manifest["unavailable"].append({"search": term, "error": str(error)})
            save(root / "manifest.json", manifest)
            await asyncio.sleep(1)
        seen: set[int] = {chunk["conId"] for chunk in manifest["chunks"]}
        for contract in candidates:
            status(root, "collecting", contract.symbol, len(manifest["chunks"]))
            label = {
                "symbol": contract.symbol,
                "exchange": contract.exchange,
                "secType": contract.secType,
            }
            try:
                qualified = await asyncio.wait_for(ib.qualifyContractsAsync(contract), timeout=30)
                if (
                    len(qualified) != 1
                    or qualified[0] is None
                    or qualified[0].secType != "IND"
                    or not qualified[0].conId
                ):
                    raise ValueError("unique qualified IND required")
                c = qualified[0]
                if c.conId in seen:
                    continue
                seen.add(c.conId)
                bars = await ib.reqHistoricalDataAsync(
                    c,
                    endDateTime="",
                    durationStr="1 M",
                    barSizeSetting="5 mins",
                    whatToShow="TRADES",
                    useRTH=False,
                    formatDate=2,
                    timeout=60,
                )
                raw = util.df(bars)
                if raw is None or raw.empty:
                    raise ValueError("no historical bars returned")
                frame = validate(raw.set_index("date"), datetime.now(UTC))
                content = frame.to_parquet(index=True)
                sha = hashlib.sha256(content).hexdigest()
                name = f"IND_{c.conId}_{sha[:16]}.parquet"
                (root / name).write_bytes(content)
                manifest["chunks"].append(
                    {
                        **label,
                        "conId": c.conId,
                        "currency": c.currency,
                        "localSymbol": c.localSymbol,
                        "file": name,
                        "sha256": sha,
                        "rows": len(frame),
                        "first": str(frame.index[0]),
                        "last": str(frame.index[-1]),
                        "barSize": "5 mins",
                        "whatToShow": "TRADES",
                        "tradable": False,
                        "study": study(frame),
                    }
                )
            except (TimeoutError, RuntimeError, ValueError, OSError) as error:
                manifest["unavailable"].append({**label, "error": str(error)})
            save(root / "manifest.json", manifest)
            await asyncio.sleep(2)
        status(root, "complete", None, len(manifest["chunks"]))
        return manifest
    finally:
        ib.disconnect()


async def run(out: Path, prior: Path | None = None) -> None:
    if out.exists():
        raise ValueError("output exists; preserve run before retry")
    out.mkdir(parents=True)
    lock = (out / "collector.lock").open("w+b")
    lock.write(b"0")
    lock.flush()
    lock.seek(0)
    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    try:
        source = Path(__file__).read_bytes()
        (out / "frozen_source.py").write_bytes(source)
        save(
            out / "protocol.json",
            {
                "schema": VERSION,
                "prior_completed_snapshots": str(prior) if prior else None,
                "frozen_at": datetime.now(UTC).isoformat(),
                "source_sha256": hashlib.sha256(source).hexdigest(),
                "catalog": CATALOG,
                "search_terms": SEARCH,
                "universe_limit": (
                    "Explicit candidates plus IND results from listed symbol searches; "
                    "not exhaustive worldwide."
                ),
                "connection": "loopback4002 client89 readonly singleDU",
                "data_scope": "INDEX study may include 2026; never reads FX holdout.",
                "horizon_bars": 96,
                "horizon_definition": (
                    "96 observed trading bars, may cross session gaps; "
                    "not eight elapsed hours. Actual label end is purged."
                ),
                "split": "chronological70/30; training label ends before cutoff minus24h",
                "minimum_rows": [500, 100],
                "models": "logisticC0.1; RF100 depth3/5 leaf50 seed7",
                "execution_enabled": False,
                "approved": False,
                "policy": "IND reference, nontradable; no profit approval/cost/PnL claims.",
            },
        )
        status(out, "connecting", None, 0)
        result = await collect(out, prior)
        save(out / "manifest.json", result)
        save(
            out / "summary.json",
            {
                "completed_at": datetime.now(UTC).isoformat(),
                "collected_indices": len(result["chunks"]),
                "unavailable": len(result["unavailable"]),
                "execution_enabled": False,
                "approved": False,
                "orders_sent": 0,
            },
        )
    finally:
        lock.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--prior", type=Path)
    args = parser.parse_args()
    asyncio.run(run(args.out, args.prior))
