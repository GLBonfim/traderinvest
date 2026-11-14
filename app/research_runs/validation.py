"""Records an explicit Phase 9 statistical-validation run in the research run registry.

The existing `Validator` is run unchanged (same configuration, bootstrap, multiple-testing
corrections, cost scenarios, redundancy and concentration); this module only adds input checks,
fingerprints, persistence and verification around it:

1. inputs are checked BEFORE anything is registered (invalid input never creates a run);
2. a `validation` run is started; the `Validator` runs; every table of
   `ValidationReport.frames()` is written to staging as CSV, plus `summary.json`;
3. the registry publishes the run as complete only when every required artifact is present
   and its SHA-256 is verified; any error before that records the run as failed (staging is
   kept for diagnosis) and earlier runs are never touched.

Exploratory research output: statistical significance is not economic significance, and
nothing here is evidence of future profitability. This module is imported only by the CLI and
by tests; nothing runs it automatically.
"""

import dataclasses
import hashlib
import json
from datetime import date
from typing import Any, Literal

import numpy as np
import pandas as pd

from app.research_runs.registry import (
    UNKNOWN,
    Fingerprint,
    RegistryError,
    RunRecord,
    RunRegistry,
    RunStatus,
)
from app.validation.config import VALIDATION_VERSION, ValidationConfig
from app.validation.engine import Validator, report_summary

KIND: Literal["validation"] = "validation"
FRAMES = ("intervals", "comparisons", "cost_sensitivity", "redundancy", "concentration")
SUMMARY = "summary.json"
ARTIFACTS = (*(f"{n}.csv" for n in FRAMES), SUMMARY)
INPUT_COLUMNS = ("open", "high", "low", "close", "volume", "adj_close")
DATA_FINGERPRINT_METHOD = (
    "sha256 of pandas.util.hash_pandas_object(bars[open,high,low,close,volume,adj_close], "
    "index=True) as little-endian uint64, preceded by the column names"
)
NOTE = (
    "Exploratory. Statistical significance is not economic significance; "
    "not evidence of future profitability."
)


class ValidationRunError(RuntimeError):
    """The validation run failed after it was registered; it is recorded as failed (or, if the
    registry itself refused the write, left incomplete) and never shown as complete."""

    def __init__(self, run_id: str, record: RunRecord | None, message: str) -> None:
        super().__init__(message)
        self.run_id = run_id
        self.record = record


# ── inputs and fingerprints ──


def check_bars(bars: Any) -> None:
    """Structural input checks only; the statistics are left entirely to the Validator."""
    if not isinstance(bars, pd.DataFrame):
        raise ValueError("bars must be a pandas DataFrame")
    if bars.empty:
        raise ValueError("bars are empty")
    missing = [c for c in INPUT_COLUMNS if c not in bars.columns]
    if missing:
        raise ValueError(f"bars are missing columns: {missing}")
    if not isinstance(bars.index, pd.DatetimeIndex) or bars.index.tz is None:
        raise ValueError("bars must have a timezone-aware DatetimeIndex")
    if not bars.index.is_unique or not bars.index.is_monotonic_increasing:
        raise ValueError("bars must be in strictly increasing time order")


def data_fingerprint(bars: pd.DataFrame) -> Fingerprint:
    """Content fingerprint of exactly the data handed to the Validator (see the method text)."""
    h = hashlib.sha256("|".join(INPUT_COLUMNS).encode())
    hashed = pd.util.hash_pandas_object(bars[list(INPUT_COLUMNS)], index=True)
    h.update(np.asarray(hashed, dtype="<u8").tobytes())
    return Fingerprint.known(h.hexdigest())


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, date):  # also datetime / pd.Timestamp
        return value.isoformat()
    if value is None or isinstance(value, str | bool | int | float):
        return value
    raise TypeError(f"cannot record parameter value of type {type(value).__name__}")


def run_parameters(
    validator: Validator, bars: pd.DataFrame, source: dict[str, Any]
) -> dict[str, Any]:
    """The parameters effectively used, as recorded in the manifest."""
    idx = pd.DatetimeIndex(bars.index)
    params: dict[str, Any] = _jsonable(
        {
            "validation_version": VALIDATION_VERSION,
            "validation_config": dataclasses.asdict(validator.config),
            "backtest_config_fingerprint": validator.backtest_config.fingerprint(),
            "strategy_engine_fingerprint": validator.strategy_engine.fingerprint(),
            "input": {
                "rows": len(bars),
                "first_ts": idx[0],
                "last_ts": idx[-1],
                "columns": list(INPUT_COLUMNS),
                "data_fingerprint_method": DATA_FINGERPRINT_METHOD,
            },
            "source": source,
        }
    )
    return params


def _csv(frame: pd.DataFrame) -> str:
    return frame.to_csv(index=False, lineterminator="\n")


# ── run ──


def record_validation(
    bars: pd.DataFrame,
    *,
    registry: RunRegistry,
    validator: Validator | None = None,
    dataset_key: Fingerprint = UNKNOWN,
    source: dict[str, Any] | None = None,
) -> RunRecord:
    """Runs the Validator once on `bars` and records the run. Returns the verified COMPLETE
    record, or raises `ValidationRunError` (the run is then never complete)."""
    check_bars(bars)
    validator = validator or Validator(ValidationConfig())
    if not isinstance(validator, Validator):
        raise ValueError("validator must be an app.validation.engine.Validator")
    if not isinstance(dataset_key, Fingerprint):
        raise ValueError("dataset_key must be a Fingerprint (known / unknown / not applicable)")
    params = run_parameters(validator, bars, source or {"kind": "in-memory bars"})
    handle = registry.start(
        KIND,
        config_fingerprint=Fingerprint.known(validator.config.fingerprint()),
        data_fingerprint=data_fingerprint(bars),
        dataset_key=dataset_key,
        parameters=params,
        required_artifacts=ARTIFACTS,
    )
    try:
        report = validator.run(bars)
        frames = report.frames()
        if tuple(frames) != FRAMES:
            raise RuntimeError(f"unexpected report tables: {sorted(frames)}")
        for name in FRAMES:
            handle.write_artifact(f"{name}.csv", _csv(frames[name]))
        summary = {**report_summary(report), "config": report.config, "note": NOTE}
        handle.write_artifact(
            SUMMARY, json.dumps(_jsonable(summary), indent=2, sort_keys=True, allow_nan=False)
        )
        record = handle.publish()
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        failed: RunRecord | None = None
        if handle.ended is None:
            try:
                failed = handle.fail(reason)
            except RegistryError as reg_exc:  # e.g. lock busy: the run stays INCOMPLETE
                reason += f" (could not record the failure: {type(reg_exc).__name__}: {reg_exc})"
        raise ValidationRunError(handle.run_id, failed, reason) from exc
    if record.status is not RunStatus.COMPLETE:  # defensive: publish verifies the run
        raise ValidationRunError(record.run_id, record, f"run not complete: {record.problems}")
    return record


# ── retrieval ──


def load_validation_run(
    registry: RunRegistry, run_id: str | None = None
) -> tuple[RunRecord, dict[str, pd.DataFrame], dict[str, Any]]:
    """Tables and summary of a COMPLETE, verified validation run (default: the latest one).
    Runs that are incomplete, failed or fail verification are refused."""
    if run_id is None:
        latest = registry.latest_complete(KIND)
        if latest is None:
            raise LookupError("no complete validation run in the registry")
        record = latest
    else:
        record = registry.get(run_id)
    if record.kind != KIND:
        raise LookupError(f"run {record.run_id} is not a validation run")
    if record.status is not RunStatus.COMPLETE:
        raise LookupError(f"run {record.run_id} is {record.status.value}: {list(record.problems)}")
    base = registry.published / record.run_id
    frames = {n: pd.read_csv(base / f"{n}.csv") for n in FRAMES}
    summary = json.loads((base / SUMMARY).read_text(encoding="utf-8"))
    after = registry.get(record.run_id)  # files may not change while being read
    if after.status is not RunStatus.COMPLETE:
        raise LookupError(f"run {record.run_id} changed while reading: {list(after.problems)}")
    return record, frames, summary
