"""Selective, reproducible risk-research runs (used by `app.risk.cli run`).

A run executes exactly the selected (strategy x risk scenario x slice) cases under one cost
scenario and writes its outputs into a NEW, exclusive directory:

1. the selection is validated before any data is read (unknown, duplicate or empty values and
   invalid combinations are refused);
2. the output directory is created with a single `os.mkdir` — atomic, and refused if the path
   already exists (even empty); nothing is ever deleted, cleaned or overwritten;
3. `manifest.json` is written atomically at start (`running`) and rewritten at the end
   (`complete` or `failed`); result files are created with mode "x" (never overwrite);
4. the manifest records code version, selection, effective configuration, the input window and
   a content fingerprint of the bars actually used. Only `status == "complete"` is a result.

Measurement only: no scenario is selected or ranked, nothing touches the paper accounts, the
operations pipeline, the scheduler or any broker.
"""

import dataclasses
import hashlib
import json
import math
import os
import subprocess
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from app.backtest.config import BacktestConfig
from app.core.integrity import atomic_write
from app.research_runs.registry import detect_code_version
from app.risk.config import RISK_VERSION, SCENARIOS, RiskConfig
from app.risk.diagnostics import (
    COMPARED_METRICS,
    INTERVAL_KIND,
    ORDER_DEPENDENT_METRICS,
    ORDER_DEPENDENT_NOTE,
    RiskSuiteReport,
    run_risk_suite,
)
from app.strategies.baselines import baseline_strategies
from app.strategies.config import StrategyConfig
from app.strategies.engine import StrategyEngine
from app.validation.config import (
    COST_SCENARIOS,
    DEFAULT_SLICES,
    CostScenario,
    SliceSpec,
    ValidationConfig,
)

MANIFEST = "manifest.json"
MANIFEST_SCHEMA = "risk-research-run/2"  # /2: environment, statistics, events
TABLE = "risk_table.csv"
COMPARISONS = "risk_comparisons.csv"
CONTROL = "control_no_overlay"
DEFAULT_COST = "D_default"  # the cost model the suite has always used (BacktestConfig defaults)
RUNNING, COMPLETE, FAILED = "running", "complete", "failed"

FINGERPRINT_COLUMNS = ("open", "high", "low", "close", "volume", "adj_close")
FINGERPRINT_VERSION = "bars-canonical-v1"
FINGERPRINT_METHOD = (
    "sha256 over UTF-8 text: a header line 'bars-canonical-v1|ts|open|high|low|close|volume|"
    "adj_close', then one line per bar in index order: the bar timestamp converted to UTC in "
    "ISO-8601 (pandas Timestamp.isoformat), then each column as float64 written with Python "
    "repr (shortest round-trip form; NaN as 'null', infinities as 'inf'/'-inf'), fields joined "
    "by '|', every line terminated by '\\n'. Bars must be unique and strictly increasing."
)


class SelectionError(ValueError):
    """The requested selection is invalid; nothing was read or written."""


class OutputExistsError(FileExistsError):
    """The output directory already exists; it was not modified."""


# ── selection ──


def strategy_ids() -> tuple[str, ...]:
    return tuple(s.strategy_id for s in baseline_strategies(StrategyConfig()))


def scenario_names() -> tuple[str, ...]:
    return tuple(s.name for s in SCENARIOS)


def cost_names() -> tuple[str, ...]:
    """Full names plus their letter aliases (A, B, C, D)."""
    return tuple(c.name for c in COST_SCENARIOS) + tuple(
        c.name.split("_")[0] for c in COST_SCENARIOS
    )


def slice_names() -> tuple[str, ...]:
    return tuple(s.name for s in DEFAULT_SLICES)


@dataclass(frozen=True)
class Selection:
    mode: str  # "selective" | "full_suite"
    strategies: tuple[str, ...]
    scenarios: tuple[RiskConfig, ...]
    cost: CostScenario
    slices: tuple[SliceSpec, ...]
    compare: bool

    def backtest_config(self) -> BacktestConfig:
        c = self.cost  # the same overrides as CostScenario.backtest_overrides()
        return dataclasses.replace(
            BacktestConfig(),
            slippage_bps=c.bps,
            spread_bps=c.spread_bps,
            commission_per_trade=c.per_order,
            commission_bps=0.0,
            minimum_commission=0.0,
        )

    def strategy_engine(self) -> StrategyEngine:
        """An engine containing ONLY the selected strategies (no other strategy is computed)."""
        chosen = tuple(
            s for s in baseline_strategies(StrategyConfig()) if s.strategy_id in self.strategies
        )
        return StrategyEngine(strategies=chosen)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "strategies": list(self.strategies),
            "risk_scenarios": [s.name for s in self.scenarios],
            "cost_scenario": self.cost.name,
            "slices": [s.name for s in self.slices],
            "compare": self.compare,
        }


def _pick(kind: str, given: Sequence[str], valid: Sequence[str]) -> list[str]:
    values = [v.strip() for v in given]
    if not values or any(not v for v in values):
        raise SelectionError(f"empty {kind} selection")
    unknown = [v for v in values if v not in valid]
    if unknown:
        raise SelectionError(f"unknown {kind} {unknown}; valid: {', '.join(valid)}")
    if len(set(values)) != len(values):
        raise SelectionError(f"duplicate {kind} in {values}")
    return values


def resolve_selection(
    *,
    strategies: Sequence[str] | None,
    scenarios: Sequence[str] | None,
    cost: str | None,
    slices: Sequence[str] | None,
    compare: bool = True,
) -> Selection:
    """Validated selection. With no selection argument at all the previous behaviour is kept
    (every strategy x scenario x slice, cost D). As soon as ONE selection argument is given,
    all four are required: a selective command is never widened implicitly."""
    given = {"--strategy": strategies, "--scenario": scenarios, "--cost": cost, "--slice": slices}
    if all(v is None for v in given.values()):
        if not compare:
            raise SelectionError("--no-compare is only valid for a selective run")
        return Selection(
            "full_suite",
            strategy_ids(),
            SCENARIOS,
            _cost(DEFAULT_COST),
            DEFAULT_SLICES,
            True,
        )
    if strategies is None or scenarios is None or cost is None or slices is None:
        missing = [k for k, v in given.items() if v is None]
        raise SelectionError(
            f"a selective run must specify all of --strategy, --scenario, --cost and --slice; "
            f"missing: {', '.join(missing)}"
        )
    strat = set(_pick("strategy", strategies, strategy_ids()))
    scen = set(_pick("risk scenario", scenarios, scenario_names()))
    sl = set(_pick("slice", slices, slice_names()))
    _pick("cost scenario", [cost], cost_names())
    if CONTROL not in scen:
        raise SelectionError(
            f"{CONTROL} must be selected: the risk suite runs every overlay against the control"
        )
    if compare and len(scen) < 2:
        raise SelectionError("comparisons need the control and at least one overlay "
                             "(or pass --no-compare)")  # fmt: skip
    # canonical (pre-declared) order; the control is first, as the suite requires
    return Selection(
        "selective",
        tuple(s for s in strategy_ids() if s in strat),
        tuple(s for s in SCENARIOS if s.name in scen),
        _cost(cost),
        tuple(s for s in DEFAULT_SLICES if s.name in sl),
        compare,
    )


def _cost(name: str) -> CostScenario:
    for c in COST_SCENARIOS:
        if name in (c.name, c.name.split("_")[0]):
            return c
    raise SelectionError(f"unknown cost scenario {name!r}")


# ── data fingerprint ──


def _num(x: float) -> str:
    return "null" if math.isnan(x) else repr(x)


def bars_fingerprint(bars: pd.DataFrame) -> dict[str, Any]:
    """Content fingerprint of exactly the bars handed to the suite (see FINGERPRINT_METHOD)."""
    if not isinstance(bars, pd.DataFrame) or bars.empty:
        raise ValueError("bars must be a non-empty DataFrame")
    missing = [c for c in FINGERPRINT_COLUMNS if c not in bars.columns]
    if missing:
        raise ValueError(f"bars are missing columns: {missing}")
    idx = bars.index
    if not isinstance(idx, pd.DatetimeIndex) or idx.tz is None:
        raise ValueError("bars must have a timezone-aware DatetimeIndex")
    if not idx.is_unique or not idx.is_monotonic_increasing:
        raise ValueError("bars must be in strictly increasing time order")
    utc = idx.tz_convert("UTC")
    cols = [bars[c].to_numpy("float64") for c in FINGERPRINT_COLUMNS]
    h = hashlib.sha256(
        ("|".join((FINGERPRINT_VERSION, "ts", *FINGERPRINT_COLUMNS)) + "\n").encode()
    )
    for i, ts in enumerate(utc):
        line = "|".join([ts.isoformat(), *(_num(float(c[i])) for c in cols)])
        h.update((line + "\n").encode())
    return {
        "algorithm": "sha256",
        "version": FINGERPRINT_VERSION,
        "method": FINGERPRINT_METHOD,
        "columns": list(FINGERPRINT_COLUMNS),
        "value": h.hexdigest(),
    }


# ── output directory and files ──


def new_run_id(now: datetime) -> str:
    return f"risk-{now.astimezone(UTC).strftime('%Y%m%dT%H%M%S%f')}Z-{uuid.uuid4().hex[:8]}"


def create_run_dir(path: Path) -> Path:
    """Creates `path` atomically; refuses any existing path (even an empty directory). The
    parent directory must already exist. Never deletes or modifies anything that exists."""
    try:
        os.mkdir(path)
    except FileExistsError as exc:
        raise OutputExistsError(f"output directory already exists: {path}") from exc
    return path


def write_new(path: Path, text: str) -> None:
    """Creates a new file; fails if it exists (mode 'x')."""
    with open(path, "x", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, date):  # also datetime / pd.Timestamp
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, str | bool | int | float):
        return value
    raise TypeError(f"cannot record value of type {type(value).__name__}")


def write_manifest(run_dir: Path, manifest: dict[str, Any]) -> None:
    atomic_write(
        run_dir / MANIFEST,
        json.dumps(_jsonable(manifest), indent=2, sort_keys=True, allow_nan=False) + "\n",
    )


def git_branch(repo: Path | None = None) -> str | None:
    """Current branch (read-only git); None when unknown."""
    cwd = repo or Path(__file__).resolve().parents[2]
    try:
        out = subprocess.run(
            ["git", "--no-optional-locks", "rev-parse", "--abbrev-ref", "HEAD"],  # noqa: S607
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out or None


def code_info() -> dict[str, Any]:
    cv = detect_code_version()
    return {
        "app_version": cv.app_version,
        "git_commit": cv.git_commit.value,
        "git_branch": git_branch(),
        "git_dirty": cv.git_dirty,
    }


def environment_info() -> dict[str, Any]:
    """Library versions that affect numerical results (the bootstrap uses numpy's PCG64)."""
    return {"numpy_version": np.__version__}


# ── intervention events (descriptive warnings, not a statistical rule) ──

# Below this many events of an event-driven control, its effect rests on a handful of
# episodes. A documented convention, configurable per run; not a significance threshold.
SPARSE_EVENT_THRESHOLD = 5
NOT_APPLICABLE, NOT_COUNTED = "not_applicable", "not_counted"
NONE, SINGLE, FEW, SEVERAL = "none", "single", "few", "several"
EVENT_NOTES = {
    NOT_APPLICABLE: "no event-driven control (sizing and limits act continuously)",
    NOT_COUNTED: "event count not available for this control",
    NONE: "no event in this window: the control's effect is not observed here "
    "(not evidence that it is ineffective)",
    SINGLE: "one event: the difference vs the control rests on a single episode",
    FEW: "few events (below the documented threshold): the difference is fragile",
    SEVERAL: "",
}


def event_controls(risk: RiskConfig) -> list[tuple[str, str | None]]:
    """(control, diagnostics counter) for each event-driven control enabled in `risk`."""
    out: list[tuple[str, str | None]] = []
    if risk.stop.method != "none":
        out.append(("stop", "stop_triggers"))
    if risk.drawdown.enabled:
        out.append(("drawdown_lock", "drawdown_lock_events"))
    if risk.session.enabled:
        out.append(("session_lock", None))  # no event counter in the risk diagnostics
    return out


def event_level(count: float | None, threshold: int) -> str:
    if threshold < 1:
        raise ValueError("sparse event threshold must be >= 1")
    if count is None or not math.isfinite(count):
        return NOT_COUNTED
    if count <= 0:
        return NONE
    if count == 1:
        return SINGLE
    return FEW if count < threshold else SEVERAL


def _event_row(key: tuple[str, str, str], control: str | None, counter: str | None,
               count: float | None, level: str) -> dict[str, Any]:  # fmt: skip
    sl, sid, sc = key
    return {
        "slice": sl,
        "strategy": sid,
        "scenario": sc,
        "control": control,
        "counter": counter,
        "count": count,
        "level": level,
        "note": EVENT_NOTES[level],
    }


def intervention_events(
    report: RiskSuiteReport, scenarios: Sequence[RiskConfig], threshold: int
) -> list[dict[str, Any]]:
    """One row per (run, event-driven control); runs without such a control get one
    `not_applicable` row. Descriptive only."""
    by_name = {s.name: s for s in scenarios}
    rows: list[dict[str, Any]] = []
    for key, run in report.runs.items():
        controls = event_controls(by_name[key[2]])
        if not controls:
            rows.append(_event_row(key, None, None, None, NOT_APPLICABLE))
            continue
        for control, counter in controls:
            raw = run.diagnostics.get(counter) if counter else None
            count = None if raw is None else float(raw)
            rows.append(_event_row(key, control, counter, count, event_level(count, threshold)))
    return rows


def with_event_columns(table: pd.DataFrame, events: list[dict[str, Any]]) -> pd.DataFrame:
    """Appends `event_levels`, `event_counts` and `event_warning` (';'-joined per control)."""
    levels, counts, warnings = [], [], []
    for _, row in table.iterrows():
        key = (row["slice"], row["strategy"], row["scenario"])
        mine = [e for e in events if (e["slice"], e["strategy"], e["scenario"]) == key]
        levels.append(
            ";".join(
                e["level"] if e["control"] is None else f"{e['control']}:{e['level']}" for e in mine
            )
        )
        counts.append(
            ";".join(f"{e['counter']}={e['count']:g}" for e in mine if e["count"] is not None)
        )
        warnings.append(
            "; ".join(
                f"{e['control']}: {e['note']}"
                for e in mine
                if e["control"] is not None and e["note"]
            )
        )
    return table.assign(event_levels=levels, event_counts=counts, event_warning=warnings)


def effective_config(sel: Selection, validation: ValidationConfig) -> dict[str, Any]:
    bt = sel.backtest_config()
    return {
        "risk_version": RISK_VERSION,
        "risk_scenarios": {
            s.name: {"fingerprint": s.fingerprint(), "config": dataclasses.asdict(s)}
            for s in sel.scenarios
        },
        "cost_scenario": dataclasses.asdict(sel.cost),
        "backtest": {"fingerprint": bt.fingerprint(), "config": dataclasses.asdict(bt)},
        "slices": [dataclasses.asdict(s) for s in sel.slices],
        "strategy_engine_fingerprint": sel.strategy_engine().fingerprint(),
        "comparisons": (
            {
                "method": validation.method,
                "block_length": validation.block_length,
                "n_resamples": validation.n_resamples,
                "confidence": validation.confidence,
                "seed": validation.seed,
                "periods_per_year": validation.periods_per_year,
                "validation_config_fingerprint": validation.fingerprint(),
                "family": "descriptive",
                "interval_kind": INTERVAL_KIND,
                "compared_metrics": list(COMPARED_METRICS),
                "order_dependent_metrics": sorted(ORDER_DEPENDENT_METRICS),
            }
            if sel.compare
            else None
        ),
    }


def _windows(report: RiskSuiteReport) -> dict[str, dict[str, Any]]:
    """Per slice: the sessions actually simulated (from the first run of that slice)."""
    out: dict[str, dict[str, Any]] = {}
    for (sl, _, _), run in report.runs.items():
        if sl in out or run.equity.empty:
            continue
        idx = pd.DatetimeIndex(run.equity.index)
        out[sl] = {"first_ts": idx[0], "last_ts": idx[-1], "sessions": len(idx)}
    return out


# ── run ──


@dataclass(frozen=True)
class LoadResult:
    """Bars plus the evidence of how the database was accessed (`db_access`)."""

    bars: pd.DataFrame
    db_access: dict[str, Any]


class LoadFailedError(RuntimeError):
    """Loading failed; carries the database-access evidence gathered so far."""

    def __init__(self, message: str, db_access: dict[str, Any] | None) -> None:
        super().__init__(message)
        self.db_access = db_access


NO_DB_EVIDENCE = {
    "mechanism": None,
    "read_only_confirmed": False,
    "note": "the loader supplied no database-access evidence (e.g. in-memory bars)",
}

SuiteRunner = Callable[..., RiskSuiteReport]
BarsLoader = Callable[[], "pd.DataFrame | LoadResult"]


@dataclass
class RunOutcome:
    run_id: str
    run_dir: Path
    status: str
    manifest: dict[str, Any]
    error: str | None = None


def execute(
    sel: Selection,
    *,
    symbol: str,
    run_dir: Path,
    run_id: str,
    load_bars: BarsLoader,
    run_suite: SuiteRunner = run_risk_suite,
    clock: Callable[[], datetime] | None = None,
    code: dict[str, Any] | None = None,
    command: list[str] | None = None,
    sparse_event_threshold: int = SPARSE_EVENT_THRESHOLD,
) -> RunOutcome:
    """Runs exactly `sel` into the (already created, exclusive) `run_dir`. Never raises for a
    failure after the directory exists: the manifest records `failed` (best effort)."""
    if sparse_event_threshold < 1:
        raise ValueError("sparse_event_threshold must be >= 1")
    clock = clock or (lambda: datetime.now(UTC))
    validation = ValidationConfig()
    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA,
        "run_id": run_id,
        "status": RUNNING,
        "started_at": clock(),
        "completed_at": None,
        "code": code if code is not None else code_info(),
        "environment": environment_info(),
        "command": command or [],
        "selection": sel.to_dict(),
        "config": effective_config(sel, validation),
        "statistics": {
            "point_estimates": "descriptive (one historical path)",
            "intervals": f"{INTERVAL_KIND}: exploratory, NOT validated confidence intervals; "
            "the realized returns of a path-dependent policy are resampled, the policy is not "
            "re-run on resampled inputs",
            "order_dependent_metrics": {m: ORDER_DEPENDENT_NOTE for m in ORDER_DEPENDENT_METRICS},
            "formal_tests": "none (no p-values, no multiple-comparison correction)",
            "sparse_event_threshold": sparse_event_threshold,
        },
        "db_access": None,
        "input": None,
        "results": None,
        "artifacts": [],
        "error": None,
        "note": "Measurement only; no scenario is selected. Lower risk usually means lower "
        "return. Only status 'complete' is a valid result.",
    }
    try:
        write_manifest(run_dir, manifest)
        loaded = load_bars()
        if isinstance(loaded, LoadResult):
            bars, access = loaded.bars, dict(loaded.db_access)
            if access.get("read_only_confirmed") is not True:
                manifest["db_access"] = access
                raise LoadFailedError("database read-only access was not confirmed", access)
        else:
            bars, access = loaded, dict(NO_DB_EVIDENCE)
        manifest["db_access"] = access
        fp = bars_fingerprint(bars)
        idx = pd.DatetimeIndex(bars.index)
        manifest["input"] = {
            "symbol": symbol,
            "source": "closed bars (app.backtest.data.load_backtest_bars); database access: "
            "see db_access",
            "rows": len(bars),
            "first_ts": idx[0],
            "last_ts": idx[-1],
            "fingerprint": fp,
        }
        write_manifest(run_dir, manifest)
        report = run_suite(
            bars,
            scenarios=sel.scenarios,
            slices=sel.slices,
            backtest=sel.backtest_config(),
            strategy_engine=sel.strategy_engine(),
            validation=validation,
            compare=sel.compare,
        )
        expected = {
            (s.name, st, sc.name)
            for s in sel.slices
            for st in sel.strategies
            for sc in sel.scenarios
        }
        if set(report.runs) != expected:
            raise RuntimeError(
                f"suite produced {sorted(set(report.runs) ^ expected)} outside/missing from "
                "the selection"
            )
        events = intervention_events(report, sel.scenarios, sparse_event_threshold)
        table = with_event_columns(report.table(), events)
        comparisons = pd.DataFrame(report.comparisons)
        artifacts = [TABLE]
        write_new(run_dir / TABLE, table.to_csv(index=False, lineterminator="\n"))
        if sel.compare:
            write_new(run_dir / COMPARISONS, comparisons.to_csv(index=False, lineterminator="\n"))
            artifacts.append(COMPARISONS)
        manifest["results"] = {
            "runs": len(report.runs),
            "table_rows": len(table),
            "metric_columns": [
                c for c in table.columns if c not in ("slice", "strategy", "scenario")
            ],
            "comparisons": len(report.comparisons),
            "windows": _windows(report),
            "backtest_fingerprint": report.backtest_fingerprint,
            "timings_ms": report.timings_ms,
            "intervention_events": events,
            "estimates_outside_own_interval": [
                {k: c[k] for k in ("slice", "strategy", "scenario", "metric")}
                for c in report.comparisons
                if c.get("estimate_in_interval") is False
            ],
        }
        manifest["artifacts"] = [
            {"path": a, "sha256": _sha256(run_dir / a), "bytes": (run_dir / a).stat().st_size}
            for a in artifacts
        ]
        manifest["status"] = COMPLETE
        manifest["completed_at"] = clock()
        write_manifest(run_dir, manifest)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:500]
        manifest.update(status=FAILED, completed_at=clock(), error=error)
        if isinstance(exc, LoadFailedError) and exc.db_access is not None:
            manifest["db_access"] = exc.db_access
        try:
            write_manifest(run_dir, manifest)
        except Exception as wexc:  # the manifest may stay 'running': never 'complete'
            error += f" (could not record the failure: {type(wexc).__name__})"
        return RunOutcome(run_id, run_dir, FAILED, manifest, error)
    return RunOutcome(run_id, run_dir, COMPLETE, manifest)
