"""Data manifest: the frozen description of every input of the experiment.

The manifest pins the universe (by hash), every raw file (by SHA-256), the data end, the
evaluable window, the 60/20/20 partition boundaries, the data rules and the audit verdict.
Its own hash is the identity every later run records.
"""

import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd

from app.core.integrity import atomic_write, canonical_json
from app.trend import audit as qa
from app.trend.loaders import read_ecb_csv, read_price_csv
from app.trend.sources import FileRecord, sha256_file
from app.trend.universe import UNIVERSE_PATH, Universe

SCHEMA = "trend-manifest/1"
MANIFEST_PATH = UNIVERSE_PATH.parent / "manifest.json"
AUDIT_PATH = UNIVERSE_PATH.parent / "audit.json"

DATA_RULES = {
    "sessions": (
        "An asset's sessions are the provider's rows. Exchange-calendar differences are listed by "
        "the audit; a calendar session without a provider row is no session for that asset (its "
        "move is captured by the next row)."
    ),
    "total_return": (
        "Daily factor (close_t + dividend_t) / close_{t-1}; split-adjusted close; dividends gross "
        "at the ex-date, reinvested at that close. The provider's Adj Close is used only to verify "
        "the data."
    ),
    "eur_conversion": (
        "USD series are divided by the ECB USD-per-EUR reference rate of the same date, or the "
        "latest earlier one; more than 4 weekdays of staleness fails closed."
    ),
    "cash": (
        "EONIA - 0.085 p.p. until 2019-09-30, EUR STR from 2019-10-01, minus 0.25 p.p., ACT/360, "
        "negative rates kept; interest from T-1 to T uses the rate for T-1."
    ),
    "master_calendar": "Union of the universe assets' provider rows.",
    "data_end": "Last complete session before the snapshot; later rows are ignored.",
}


def manifest_sha256(manifest: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(manifest).encode()).hexdigest()


def run_audit(
    universe: Universe, records: list[FileRecord], data_root: Path, data_end: str
) -> dict[str, Any]:
    """Run every T1 quality check and derive the window and partitions (no strategy maths)."""
    by_role = {r.role: data_root / r.path for r in records}
    end = pd.Timestamp(data_end)
    prices: dict[str, Any] = {}
    sessions: dict[str, pd.DatetimeIndex] = {}
    usd_dates: list[pd.DatetimeIndex] = []
    for asset in universe.all_series():
        frame = read_price_csv(by_role[f"price:{asset.research.symbol}"], data_end)
        prices[asset.id] = {"symbol": asset.research.symbol} | qa.audit_prices(
            frame, asset.research.calendar
        )
        idx = pd.DatetimeIndex(frame.index)
        sessions[asset.id] = idx
        if asset.research.currency == "USD":
            usd_dates.append(idx)
    universe_ids = [a.id for a in universe.assets]
    master = pd.DatetimeIndex(sorted(set().union(*(set(sessions[i]) for i in universe_ids))))
    extra_starts = {a.id: sessions[a.id][0] for a in universe.benchmark_only}
    window = qa.evaluable_window({i: sessions[i] for i in universe_ids}, extra_starts, master, end)
    start = pd.Timestamp(window.evaluable_start)
    partitions = qa.partition_bounds(master, start, end)

    fx = read_ecb_csv(by_role["fx:USD"], data_end)
    # Earliest date any input is needed: the first warm-up close of the evaluable start (one
    # extra row for the first return); older provider rows are never read by a replay.
    first_needed = min(
        sessions[i][max(0, int(sessions[i].searchsorted(start)) - qa.WARMUP_SESSIONS - 1)]
        for i in universe_ids
    )
    for b in universe.benchmark_only:
        first_needed = min(first_needed, max(sessions[b.id][0], start))
    usd_all = pd.DatetimeIndex(sorted(set().union(*(set(d) for d in usd_dates))))
    usd_all = usd_all[usd_all >= first_needed]
    fx_report = qa.audit_fx(fx, usd_all)
    eonia = read_ecb_csv(by_role["rate:eonia"], data_end)
    estr = read_ecb_csv(by_role["rate:estr"], data_end)
    rates = qa.audit_rates(eonia, estr, pd.Timestamp("2019-10-01"), first_needed)

    failures: list[str] = []
    for aid, rep in prices.items():
        if rep["duplicates"] or not rep["monotonic"]:
            failures.append(f"{aid}: duplicate or unordered dates")
        if rep["nan_close"] or rep["nan_adj_close"] or rep["non_positive_close"]:
            failures.append(f"{aid}: missing or non-positive prices")
        if rep["adjustment_sessions_above_tolerance"]:
            failures.append(f"{aid}: provider adjustment inconsistent with dividends")
        if pd.Timestamp(rep["last"]) != end:
            failures.append(f"{aid}: series does not reach the data end")
    if fx_report["sessions_without_prior_rate"] or fx_report["sessions_over_limit"]:
        failures.append("fx: stale or missing ECB rate for a USD session")
    if rates["nan"] or rates["max_gap_weekdays"] > qa.MAX_FX_STALENESS:
        failures.append("cash rate: gap in the EONIA/EUR STR chain")
    if window.years < qa.MIN_EVALUABLE_YEARS:
        failures.append(f"evaluable history {window.years} years < {qa.MIN_EVALUABLE_YEARS}")
    return {
        "data_end": data_end,
        "prices": prices,
        "fx": fx_report,
        "cash_rate": rates,
        "master_sessions": len(master),
        "window": {
            "evaluable_start": window.evaluable_start,
            "data_end": window.data_end,
            "years": window.years,
            "binding_asset": window.binding_asset,
            "warmup_end": window.warmup_end,
        },
        "partitions": partitions,
        "failures": failures,
        "verdict": "PASS" if not failures else "FAIL",
    }


def build_manifest(
    universe: Universe, records: list[FileRecord], audit_report: dict[str, Any], snapshot: str
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "design": "docs/superpowers/specs/2026-11-14-multi-asset-trend-v2-design.md",
        "adr": "ADR-0040",
        "universe_sha256": universe.sha256,
        "snapshot": snapshot,
        "files": sorted((r.to_dict() for r in records), key=lambda d: str(d["role"])),
        "data_end": audit_report["data_end"],
        "data_rules": DATA_RULES,
        "window": audit_report["window"],
        "partitions": audit_report["partitions"],
        "audit_verdict": audit_report["verdict"],
        "audit_sha256": hashlib.sha256(canonical_json(audit_report).encode()).hexdigest(),
        "parameters": {
            "sma_sessions": 200,
            "vol_sessions": 60,
            "vol_target": 0.10,
            "drift_threshold": 0.25,
            "annualisation_vol": 252,
        },
    }


def write_json(path: Path, obj: dict[str, Any]) -> None:
    atomic_write(path, json.dumps(obj, indent=2, sort_keys=True, allow_nan=False) + "\n")


def verify_manifest(manifest: dict[str, Any], data_root: Path, universe: Universe) -> list[str]:
    """Re-hash every raw file and the universe; returns a list of problems (empty = OK)."""
    problems: list[str] = []
    if manifest.get("schema") != SCHEMA:
        problems.append("unexpected manifest schema")
    if manifest.get("universe_sha256") != universe.sha256:
        problems.append("universe registry changed since the manifest was frozen")
    for f in manifest.get("files", []):
        path = data_root / f["path"]
        if not path.is_file():
            problems.append(f"missing raw file {f['path']}")
        elif sha256_file(path) != f["sha256"]:
            problems.append(f"hash mismatch {f['path']}")
    return problems
