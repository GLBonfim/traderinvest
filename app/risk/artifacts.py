"""Daily net-return artifacts of a risk research run, and a read-only run verifier.

`daily_returns.csv` (schema `risk-daily-returns/1`) holds, for every (slice, strategy, risk
scenario) of ONE run, the per-session series the suite actually produced — taken from
`RiskRun.equity`, never reconstructed from aggregate metrics:

    slice,strategy,scenario,cost_scenario,session,ts,equity,daily_return,position,costs

- one row per simulated session; `session` is the 0-based position inside its series and `ts`
  the bar open time, canonical UTC ISO-8601 with the `+00:00` suffix
  (`pandas.Timestamp.isoformat()` of the UTC instant); rows are grouped by series in the run's
  canonical order (slice, strategy, scenario as pre-declared) and strictly increasing in `ts`;
- `equity` = cash + shares x raw close of the session (after the open fill of that session);
  `daily_return` = equity(t) / equity(t-1) - 1 with equity(-1) = the backtest initial capital
  (the series the bootstrap comparisons use); transaction costs of the cost scenario
  (commission, half spread, slippage) are deducted from cash at the fill, so returns are NET;
  raw prices, no dividends; `position` = exposure at the close; `costs` = costs paid that session;
- a session without a position is an ordinary row (position 0; return 0 unless costs were
  paid that session); there are no missing sessions inside a series;
- floats are written with Python `repr` (shortest round-trip, lossless for float64);
  non-finite values are written explicitly as `nan`, `inf`, `-inf` and never imputed.

`verify_run()` checks a run directory WITHOUT modifying anything and never raises: it first
validates the manifest's structure for its schema version, then artifact names (plain file
names inside the run directory only), sizes and SHA-256 of the bytes it then parses, required
and unexpected files, and the consistency of the manifest, the selection, the configuration
fingerprints and the CSV contents. See `GUARANTEE`: this is self-consistency, not authenticity.
"""

import csv
import dataclasses
import hashlib
import io
import json
import math
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from app.backtest.config import BacktestConfig
from app.risk.diagnostics import COMPARED_METRICS

RETURNS_FILE = "daily_returns.csv"
RETURNS_SCHEMA = "risk-daily-returns/1"
RETURNS_COLUMNS = (
    "slice",
    "strategy",
    "scenario",
    "cost_scenario",
    "session",
    "ts",
    "equity",
    "daily_return",
    "position",
    "costs",
)
SERIES_COLUMNS = ("equity", "daily_return", "position", "costs")  # taken from RiskRun.equity
RETURN_CONVENTION = (
    "daily_return = equity(t)/equity(t-1) - 1, equity(-1) = backtest initial capital; equity = "
    "cash + shares x raw close after the open fill of t; costs of the cost scenario deducted "
    "from cash at the fill (net returns); raw prices, no dividends; floats in Python repr; "
    "non-finite values written as nan/inf/-inf, never imputed"
)
MANIFEST = "manifest.json"
TABLE_FILE = "risk_table.csv"  # = research.TABLE (not imported: research imports this module)
COMPARISONS_FILE = "risk_comparisons.csv"  # = research.COMPARISONS
V1, V2, V3 = "risk-research-run/1", "risk-research-run/2", "risk-research-run/3"
SUPPORTED_MANIFESTS = (V1, V2, V3)
STATUSES = ("running", "complete", "failed")
RETURN_TOLERANCE = 1e-12  # relative check of daily_return against the recorded equity
GUARANTEE = (
    "self-consistency only: artifact sizes and SHA-256 and cross-checks against the manifest. "
    "Not authenticity: a coordinated edit of the manifest and its artifacts (with recomputed "
    "hashes) is not detected. Anchoring the manifest's SHA-256 outside the run directory (e.g. "
    "in a hash-chained registry) would be needed for that; it is not implemented."
)
_ARTIFACT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CANONICAL_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?\+00:00$")
_SESSION = re.compile(r"^(0|[1-9][0-9]*)$")


# ── values ──


def fmt(x: float) -> str:
    """Lossless, explicit float text: repr for finite values, nan / inf / -inf otherwise."""
    v = float(x)
    if math.isnan(v):
        return "nan"
    if math.isinf(v):
        return "inf" if v > 0 else "-inf"
    return repr(v)


def parse(text: str) -> float:
    if text not in ("nan", "inf", "-inf"):
        try:
            v = float(text)
        except ValueError as exc:
            raise ValueError(f"not a number: {text!r}") from exc
        if not math.isfinite(v):  # only the explicit tokens may be non-finite
            raise ValueError(f"non-canonical non-finite value: {text!r}")
        return v
    return float(text)


def parse_ts(text: str) -> pd.Timestamp:
    """A canonical UTC timestamp (`+00:00` suffix, exactly as written); anything else —
    naive, `Z`, another offset even for the same instant, other layouts — is refused."""
    if not _CANONICAL_TS.match(text):
        raise ValueError(f"non-canonical timestamp {text!r} (expected UTC ISO-8601 +00:00)")
    ts = pd.Timestamp(text)
    if ts.tz is None or ts.tz_convert("UTC").isoformat() != text:
        raise ValueError(f"non-canonical timestamp {text!r}")
    return ts.tz_convert("UTC")


# ── writing ──


@dataclass(frozen=True)
class SeriesInfo:
    slice: str
    strategy: str
    scenario: str
    cost_scenario: str
    rows: int
    first_ts: str | None
    last_ts: str | None
    non_finite_returns: int
    risk_fingerprint: str
    backtest_fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def build_returns(
    runs: Sequence[tuple[tuple[str, str, str], Any]], cost_scenario: str
) -> tuple[str, list[SeriesInfo]]:
    """CSV text and per-series metadata for `runs` = [((slice, strategy, scenario), RiskRun)]
    in the order given (the caller passes the canonical order). Refuses duplicated keys,
    missing columns and unordered or duplicated sessions."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(RETURNS_COLUMNS)
    seen: set[tuple[str, str, str]] = set()
    infos: list[SeriesInfo] = []
    for key, run in runs:
        if key in seen:
            raise ValueError(f"duplicated series {key}")
        seen.add(key)
        eq: pd.DataFrame = run.equity
        missing = [c for c in SERIES_COLUMNS if c not in eq.columns]
        if missing:
            raise ValueError(f"series {key} lacks columns {missing}")
        idx = pd.DatetimeIndex(eq.index)
        if idx.tz is None:
            raise ValueError(f"series {key} has a naive time index")
        if not idx.is_unique or not idx.is_monotonic_increasing:
            raise ValueError(f"series {key} is not strictly increasing in time")
        ts = [t.isoformat() for t in idx.tz_convert("UTC")]
        cols = {c: eq[c].to_numpy("float64") for c in SERIES_COLUMNS}
        for i, t in enumerate(ts):
            w.writerow((*key, cost_scenario, i, t, *(fmt(cols[c][i]) for c in SERIES_COLUMNS)))
        rets = cols["daily_return"]
        infos.append(
            SeriesInfo(
                *key,
                cost_scenario=cost_scenario,
                rows=len(ts),
                first_ts=ts[0] if ts else None,
                last_ts=ts[-1] if ts else None,
                non_finite_returns=int(sum(not math.isfinite(float(r)) for r in rets)),
                risk_fingerprint=str(getattr(run, "risk_fingerprint", "")),
                backtest_fingerprint=str(getattr(run, "backtest_fingerprint", "")),
            )
        )
    return buf.getvalue(), infos


def write_new_atomic(path: Path, text: str) -> None:
    """Publishes `path` atomically and never over an existing file.

    The content is written, flushed and fsynced to `<name>.tmp` (created exclusively: an
    existing temporary file is not ours and is never touched), then hard-linked to `path`
    (fails if `path` exists: an existing artifact is never replaced), then the temporary name
    is removed. Any failure before publication removes the temporary file when possible; if
    that cleanup fails too, the residue is reported as a note on the ORIGINAL exception. A
    failure to remove the temporary name after publication raises (fail-closed). The
    directory is not fsynced: durability of the new name after a power loss is not guaranteed.
    """
    tmp = path.with_name(path.name + ".tmp")
    fh = open(tmp, "x", encoding="utf-8", newline="\n")  # noqa: SIM115 - closed below
    try:
        with fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.link(tmp, path)
    except BaseException as exc:
        try:
            os.remove(tmp)
        except OSError as cleanup:
            exc.add_note(
                f"temporary file left behind: {tmp.name} ({type(cleanup).__name__}: {cleanup})"
            )
        raise
    try:
        os.remove(tmp)
    except OSError as cleanup:
        raise OSError(
            f"{path.name} was published but the temporary {tmp.name} could not be removed"
        ) from cleanup


# ── reading ──


def parse_returns(data: bytes, name: str = RETURNS_FILE) -> pd.DataFrame:
    """Long table from the exact bytes of a returns file; every row is validated on its own
    (values lossless, canonical timestamps, integer sessions). Raises ValueError."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{name}: not UTF-8") from exc
    rows = list(csv.reader(io.StringIO(text, newline="")))
    if not rows or tuple(rows[0]) != RETURNS_COLUMNS:
        raise ValueError(f"{name}: unexpected header")
    records: list[dict[str, Any]] = []
    stamps: list[pd.Timestamp] = []
    for n, r in enumerate(rows[1:], start=2):
        if len(r) != len(RETURNS_COLUMNS):
            raise ValueError(f"{name} line {n}: {len(r)} fields")
        rec: dict[str, Any] = dict(zip(RETURNS_COLUMNS, r, strict=True))
        if not _SESSION.match(rec["session"]):
            raise ValueError(f"{name} line {n}: invalid session {rec['session']!r}")
        rec["session"] = int(rec["session"])
        try:
            stamps.append(parse_ts(rec["ts"]))
            for c in SERIES_COLUMNS:
                rec[c] = parse(rec[c])
        except ValueError as exc:
            raise ValueError(f"{name} line {n}: {exc}") from exc
        records.append(rec)
    frame = pd.DataFrame.from_records(records, columns=list(RETURNS_COLUMNS))
    frame["ts"] = pd.DatetimeIndex(stamps, tz="UTC") if stamps else pd.DatetimeIndex([], tz="UTC")
    return frame


def read_returns(path: Path) -> pd.DataFrame:
    """Long table of a returns file (read-only). Prefer `load_returns_matrix`, which parses
    the very bytes it verified."""
    return parse_returns(path.read_bytes(), path.name)


class ReturnsMatrixError(ValueError):
    """The returns matrix cannot be built; the message states why. Nothing was modified."""


def load_returns_matrix(
    run_dir: Path, *, slice_name: str, strategy: str, scenarios: Sequence[str]
) -> pd.DataFrame:
    """Session x scenario matrix of net daily returns from a run that VERIFIES, parsed from the
    same bytes whose SHA-256 was checked (no verify-then-reread). Refuses an empty or
    duplicated scenario list, missing series and series not aligned session by session; never
    aligns, imputes or merges. Persisting returns does not validate any statistical method."""
    if not isinstance(slice_name, str) or not isinstance(strategy, str):
        raise ReturnsMatrixError("slice_name and strategy must be strings")
    names = list(scenarios) if not isinstance(scenarios, str) else [scenarios]
    if not names:
        raise ReturnsMatrixError("no scenario requested")
    if not all(isinstance(s, str) for s in names):
        raise ReturnsMatrixError("scenario identifiers must be strings")
    if len(set(names)) != len(names):
        raise ReturnsMatrixError(f"duplicated scenario identifiers: {names}")
    report, df = _verify_safely(run_dir)
    if not report.ok:
        raise ReturnsMatrixError(f"run does not verify: {report.problems}")
    if df is None:
        raise ReturnsMatrixError("run has no daily returns artifact")
    cols: dict[str, Any] = {}
    index: pd.DatetimeIndex | None = None
    for sc in names:
        part = df[
            (df["slice"] == slice_name) & (df["strategy"] == strategy) & (df["scenario"] == sc)
        ]
        if part.empty:
            raise ReturnsMatrixError(f"no series for {(slice_name, strategy, sc)}")
        idx = pd.DatetimeIndex(part["ts"])
        if index is not None and not idx.equals(index):
            raise ReturnsMatrixError(f"series {sc} is not aligned with {names[0]}")
        index = idx
        cols[sc] = part["daily_return"].to_numpy()
    return pd.DataFrame(cols, index=index)


# ── read-only verification ──


@dataclass
class VerifyReport:
    run_dir: str
    schema: str | None = None
    status: str | None = None
    has_returns: bool = False
    checked_artifacts: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "ok": self.ok, "guarantee": GUARANTEE}


def verify_run(run_dir: Path) -> VerifyReport:
    """Checks a run directory; reads only, never writes, never repairs, never raises."""
    return _verify_safely(run_dir)[0]


def _verify_safely(run_dir: Path) -> tuple[VerifyReport, pd.DataFrame | None]:
    rep = VerifyReport(str(run_dir))
    try:
        return rep, _verify(Path(run_dir), rep)
    except Exception as exc:  # defensive: any unforeseen error is a failed verification
        rep.problems.append(f"verification error: {type(exc).__name__}: {exc}")
        return rep, None


# ── structure ──


Check = Callable[[Any], bool]


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


def _is_str(v: Any) -> bool:
    return isinstance(v, str)


def _is_dict(v: Any) -> bool:
    return isinstance(v, dict)


def _is_bool(v: Any) -> bool:
    return isinstance(v, bool)


def _str_list(v: Any) -> bool:
    return isinstance(v, list) and bool(v) and all(isinstance(x, str) and x for x in v)


def _opt_str(v: Any) -> bool:
    return v is None or isinstance(v, str)


def _need(obj: Any, key: str, check: Check, where: str, problems: list[str]) -> bool:
    if not isinstance(obj, dict) or key not in obj:
        problems.append(f"{where}.{key}: missing")
        return False
    if not check(obj[key]):
        problems.append(f"{where}.{key}: invalid type or value")
        return False
    return True


def _structure(m: Any, problems: list[str]) -> None:
    """Minimum structure of each historical manifest version (problems, never exceptions)."""
    if not isinstance(m, dict):
        problems.append("manifest: not a JSON object")
        return
    schema = m.get("schema_version")
    _need(m, "run_id", _is_str, "manifest", problems)
    if not _need(m, "status", lambda v: v in STATUSES, "manifest", problems):
        return
    if _need(m, "selection", _is_dict, "manifest", problems):
        sel = m["selection"]
        for k in ("strategies", "risk_scenarios", "slices"):
            _need(sel, k, _str_list, "selection", problems)
        _need(sel, "cost_scenario", _is_str, "selection", problems)
        _need(sel, "compare", _is_bool, "selection", problems)
        _need(sel, "mode", _is_str, "selection", problems)
    if _need(m, "artifacts", lambda v: isinstance(v, list), "manifest", problems):
        for i, a in enumerate(m["artifacts"]):
            where = f"artifacts[{i}]"
            if not isinstance(a, dict):
                problems.append(f"{where}: not an object")
                continue
            _need(a, "path", _is_str, where, problems)
            _need(a, "sha256", lambda v: isinstance(v, str) and bool(_SHA256.match(v)),
                  where, problems)  # fmt: skip
            _need(a, "bytes", lambda v: _is_int(v) and v >= 0, where, problems)
    if schema in (V2, V3):
        if _need(m, "environment", _is_dict, "manifest", problems):
            _need(m["environment"], "numpy_version", _is_str, "environment", problems)
        _need(m, "statistics", _is_dict, "manifest", problems)
    if m["status"] != "complete":
        return
    if _need(m, "results", _is_dict, "manifest", problems):
        res = m["results"]
        for k in ("runs", "table_rows", "comparisons"):
            _need(res, k, lambda v: _is_int(v) and v >= 0, "results", problems)
        _need(res, "windows", _is_dict, "results", problems)
        _need(res, "backtest_fingerprint", _is_str, "results", problems)
    if _need(m, "config", _is_dict, "manifest", problems):
        cfg = m["config"]
        if _need(cfg, "backtest", _is_dict, "config", problems):
            _need(cfg["backtest"], "fingerprint", _is_str, "config.backtest", problems)
            _need(cfg["backtest"], "config", _is_dict, "config.backtest", problems)
        if _need(cfg, "risk_scenarios", _is_dict, "config", problems):
            for name, sc in cfg["risk_scenarios"].items():
                _need(sc, "fingerprint", _is_str, f"config.risk_scenarios.{name}", problems)
        if _need(cfg, "slices", lambda v: isinstance(v, list), "config", problems):
            for i, s in enumerate(cfg["slices"]):
                _need(s, "name", _is_str, f"config.slices[{i}]", problems)
                _need(s, "start", _opt_str, f"config.slices[{i}]", problems)
                _need(s, "end", _opt_str, f"config.slices[{i}]", problems)
        if _need(cfg, "cost_scenario", _is_dict, "config", problems):
            _need(cfg["cost_scenario"], "name", _is_str, "config.cost_scenario", problems)
    results = m.get("results")
    if schema == V3 and _need(results, "daily_returns", _is_dict, "results", problems):
        b = m["results"]["daily_returns"]
        for k, check in (("path", _is_str), ("schema", _is_str), ("convention", _is_str),
                         ("columns", _str_list), ("initial_capital", _is_num),
                         ("cost_scenario", _is_str),
                         ("rows", lambda v: _is_int(v) and v >= 0),
                         ("series", lambda v: isinstance(v, list))):  # fmt: skip
            _need(b, k, check, "daily_returns", problems)
        for i, s in enumerate(b.get("series") if isinstance(b.get("series"), list) else []):
            where = f"daily_returns.series[{i}]"
            for k in ("slice", "strategy", "scenario", "cost_scenario",
                      "risk_fingerprint", "backtest_fingerprint"):  # fmt: skip
                _need(s, k, _is_str, where, problems)
            _need(s, "rows", lambda v: _is_int(v) and v >= 0, where, problems)
            _need(s, "non_finite_returns", lambda v: _is_int(v) and v >= 0, where, problems)
            _need(s, "first_ts", _opt_str, where, problems)
            _need(s, "last_ts", _opt_str, where, problems)


# ── files ──


def _safe_name(name: str) -> str | None:
    """Why `name` is not an acceptable artifact name, or None."""
    if name == MANIFEST:
        return "the manifest cannot be an artifact"
    if not _ARTIFACT_NAME.match(name) or name in (".", ".."):
        return "not a plain file name (absolute paths, separators and '..' are refused)"
    return None


def _read_artifacts(run_dir: Path, m: dict[str, Any], rep: VerifyReport) -> dict[str, bytes]:
    """Bytes of every acceptable artifact whose size and SHA-256 match (read once)."""
    contents: dict[str, bytes] = {}
    root = run_dir.resolve()
    paths = [a["path"] for a in m["artifacts"]]
    if len(set(paths)) != len(paths):
        rep.problems.append(f"duplicated artifact entries: {paths}")
    for a in m["artifacts"]:
        name = a["path"]
        why = _safe_name(name)
        if why:
            rep.problems.append(f"artifact {name!r} refused: {why}")
            continue
        p = run_dir / name
        if p.is_symlink():
            rep.problems.append(f"artifact {name!r} refused: symbolic link")
            continue
        if not p.is_file():
            rep.problems.append(f"{name}: missing")
            continue
        if p.resolve().parent != root:
            rep.problems.append(f"artifact {name!r} refused: resolves outside the run directory")
            continue
        data = p.read_bytes()
        if len(data) != a["bytes"]:
            rep.problems.append(f"{name}: size {len(data)} != {a['bytes']}")
        elif hashlib.sha256(data).hexdigest() != a["sha256"]:
            rep.problems.append(f"{name}: SHA-256 mismatch")
        else:
            contents[name] = data
        rep.checked_artifacts.append(name)
    listed = set(paths)
    unexpected = sorted(
        x.name for x in run_dir.iterdir() if x.name != MANIFEST and x.name not in listed
    )
    if unexpected:
        rep.problems.append(f"unexpected files: {unexpected}")
    return contents


def _csv_rows(data: bytes, name: str, required: Sequence[str],
              rep: VerifyReport) -> list[dict[str, str]] | None:  # fmt: skip
    try:
        reader = csv.DictReader(io.StringIO(data.decode("utf-8"), newline=""))
        rows = list(reader)
        header = reader.fieldnames or []
    except (UnicodeDecodeError, csv.Error) as exc:
        rep.problems.append(f"{name}: unreadable ({type(exc).__name__})")
        return None
    missing = [c for c in required if c not in header]
    if missing:
        rep.problems.append(f"{name}: missing columns {missing}")
        return None
    return rows


# ── verification ──


def _verify(run_dir: Path, rep: VerifyReport) -> pd.DataFrame | None:
    if run_dir.is_symlink() or not run_dir.is_dir():
        rep.problems.append("run directory missing or not a plain directory")
        return None
    mpath = run_dir / MANIFEST
    if mpath.is_symlink() or not mpath.is_file():
        rep.problems.append("manifest.json missing or not a plain file")
        return None
    try:
        m = json.loads(mpath.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        rep.problems.append(f"manifest.json unreadable: {type(exc).__name__}")
        return None
    if isinstance(m, dict):
        rep.schema, rep.status = m.get("schema_version"), m.get("status")
    if rep.schema not in SUPPORTED_MANIFESTS:
        rep.problems.append(f"unsupported manifest schema {rep.schema!r}")
        return None
    structure: list[str] = []
    _structure(m, structure)
    if structure:  # nothing nested is read from an invalid structure
        rep.problems.extend(structure)
        return None
    if rep.status != "complete":
        rep.problems.append(f"run status is {rep.status!r}, not 'complete'")
    contents = _read_artifacts(run_dir, m, rep)
    if rep.status != "complete":
        return None
    sel = m["selection"]
    required = {TABLE_FILE} | ({COMPARISONS_FILE} if sel["compare"] else set())
    if rep.schema == V3:
        required.add(RETURNS_FILE)
    listed = {a["path"] for a in m["artifacts"]}
    if required - listed:
        rep.problems.append(f"required artifacts not listed: {sorted(required - listed)}")
    if listed - required:
        rep.problems.append(f"artifacts not defined by {rep.schema}: {sorted(listed - required)}")
    _check_results(m, contents, rep)
    if rep.schema != V3:
        return None
    rep.has_returns = True
    if RETURNS_FILE not in contents:
        return None  # missing or failed its hash: already reported, never parsed
    return _check_returns(m, contents[RETURNS_FILE], rep)


def _expected(sel: dict[str, Any]) -> set[tuple[str, str, str]]:
    return {(s, st, sc) for s in sel["slices"] for st in sel["strategies"]
            for sc in sel["risk_scenarios"]}  # fmt: skip


def _check_results(m: dict[str, Any], contents: dict[str, bytes], rep: VerifyReport) -> None:
    """Selection vs result counts and the table / comparison files (every version)."""
    sel, res = m["selection"], m["results"]
    for name, values in (("slices", sel["slices"]), ("strategies", sel["strategies"]),
                         ("risk_scenarios", sel["risk_scenarios"])):  # fmt: skip
        if len(set(values)) != len(values):
            rep.problems.append(f"selection.{name} has duplicates")
    expected = _expected(sel)
    if res["runs"] != len(expected) or res["table_rows"] != len(expected):
        rep.problems.append(f"results runs/table_rows {res['runs']}/{res['table_rows']} != "
                            f"{len(expected)} selected cases")  # fmt: skip
    overlays = sel["risk_scenarios"][1:]
    n_cmp = len(sel["slices"]) * len(sel["strategies"]) * len(overlays) * len(COMPARED_METRICS)
    if res["comparisons"] != (n_cmp if sel["compare"] else 0):
        rep.problems.append(f"results.comparisons {res['comparisons']} != expected "
                            f"{n_cmp if sel['compare'] else 0}")  # fmt: skip
    if TABLE_FILE in contents:
        rows = _csv_rows(contents[TABLE_FILE], TABLE_FILE, ("slice", "strategy", "scenario"), rep)
        if rows is not None:
            keys = [(r["slice"], r["strategy"], r["scenario"]) for r in rows]
            if len(keys) != len(set(keys)) or set(keys) != expected:
                rep.problems.append(f"{TABLE_FILE}: rows differ from the selection")
    if sel["compare"] and COMPARISONS_FILE in contents:
        cols = ("slice", "strategy", "scenario", "vs", "metric")
        rows = _csv_rows(contents[COMPARISONS_FILE], COMPARISONS_FILE, cols, rep)
        if rows is not None:
            keys4 = [(r["slice"], r["strategy"], r["scenario"], r["metric"]) for r in rows]
            want = {(s, st, sc, k) for s in sel["slices"] for st in sel["strategies"]
                    for sc in overlays for k in COMPARED_METRICS}  # fmt: skip
            if len(keys4) != len(set(keys4)) or set(keys4) != want:
                rep.problems.append(f"{COMPARISONS_FILE}: rows differ from the selection")
            if {r["vs"] for r in rows} - {sel["risk_scenarios"][0]}:
                rep.problems.append(f"{COMPARISONS_FILE}: comparisons not against the control")


def _rebuild_backtest(base: dict[str, Any]) -> BacktestConfig:
    """The BacktestConfig recorded in a manifest (dates from ISO text). Raises TypeError /
    ValueError on unknown fields or invalid values; nothing is guessed."""
    values: dict[str, Any] = dict(base)
    for k in ("start", "end"):
        if values.get(k):
            values[k] = date.fromisoformat(values[k])
    return BacktestConfig(**values)


def _slice_fingerprint(base: dict[str, Any], sl: dict[str, Any]) -> str:
    """Fingerprint of the slice-specific backtest configuration the runs carry (as the suite
    builds it), from the configuration recorded in the manifest. Raises on invalid input."""
    start = date.fromisoformat(sl["start"]) if sl["start"] else None
    end = date.fromisoformat(sl["end"]) if sl["end"] else None
    bt = _rebuild_backtest({**base, "start": None, "end": None, "period_label": None})
    return dataclasses.replace(bt, start=start, end=end, period_label=sl["name"]).fingerprint()


def _check_returns(m: dict[str, Any], data: bytes, rep: VerifyReport) -> pd.DataFrame | None:
    sel, cfg, res = m["selection"], m["config"], m["results"]
    block = res["daily_returns"]
    p = rep.problems
    if block["schema"] != RETURNS_SCHEMA or block["path"] != RETURNS_FILE:
        p.append(f"daily_returns: unexpected schema/path {block['schema']!r}/{block['path']!r}")
        return None
    if block["columns"] != list(RETURNS_COLUMNS):
        p.append("daily_returns.columns differ from the schema")
    if block["convention"] != RETURN_CONVENTION:
        p.append("daily_returns.convention differs from the schema's convention")
    base = cfg["backtest"]["config"]
    initial = base.get("initial_capital")
    if not _is_num(initial) or not initial > 0:
        p.append("config.backtest.config.initial_capital missing or invalid")
        return None
    if block["initial_capital"] != initial:
        p.append("daily_returns.initial_capital differs from the backtest configuration")
    cost = sel["cost_scenario"]
    if cost != cfg["cost_scenario"]["name"] or block["cost_scenario"] != cost:
        p.append("cost scenario differs between selection, configuration and daily_returns")
    # configuration fingerprints the series must carry
    try:
        recorded = _rebuild_backtest(base).fingerprint()
    except (TypeError, ValueError) as exc:
        p.append(f"config.backtest.config cannot be rebuilt: {type(exc).__name__}")
        return None
    if recorded != cfg["backtest"]["fingerprint"] or recorded != res["backtest_fingerprint"]:
        p.append(
            "backtest fingerprint not reproducible from the recorded configuration (other "
            "code version or edited configuration): series fingerprints cannot be validated"
        )
        return None
    slices = {s["name"]: s for s in cfg["slices"]}
    if set(slices) != set(sel["slices"]) or set(cfg["risk_scenarios"]) != set(
        sel["risk_scenarios"]
    ):
        p.append("configuration slices/scenarios differ from the selection")
        return None
    try:
        slice_fp = {name: _slice_fingerprint(base, s) for name, s in slices.items()}
    except (TypeError, ValueError) as exc:
        p.append(f"slice configuration cannot be rebuilt: {type(exc).__name__}")
        return None
    try:
        df = parse_returns(data)
    except ValueError as exc:
        p.append(f"{RETURNS_FILE}: {exc}")
        return None
    keys = ["slice", "strategy", "scenario"]
    if df.duplicated([*keys, "ts"]).any() or df.duplicated([*keys, "session"]).any():
        p.append(f"{RETURNS_FILE}: duplicated sessions")
    if set(df["cost_scenario"]) - {cost}:
        p.append(f"{RETURNS_FILE}: rows from another cost scenario")
    expected = _expected(sel)
    found = {(str(a), str(b), str(c)) for a, b, c in df[keys].drop_duplicates().itertuples(False)}
    if found != expected:
        p.append(f"{RETURNS_FILE}: series {sorted(found ^ expected)} differ from the selection")
    listed_keys = [(s["slice"], s["strategy"], s["scenario"]) for s in block["series"]]
    if len(listed_keys) != len(set(listed_keys)):
        p.append("daily_returns.series lists a series more than once")
    listed = dict(zip(listed_keys, block["series"], strict=True))
    if set(listed) != expected:
        p.append("daily_returns.series differ from the selection")
    if block["rows"] != len(df) or sum(s["rows"] for s in block["series"]) != len(df):
        p.append(f"{RETURNS_FILE}: {len(df)} rows differ from the manifest's row counts")
    windows = res["windows"]
    for key, part in df.groupby(keys, sort=False):
        k = (str(key[0]), str(key[1]), str(key[2]))
        ts = pd.DatetimeIndex(part["ts"])
        if not ts.is_monotonic_increasing or not ts.is_unique:
            p.append(f"{k}: sessions not strictly increasing")
        if list(part["session"]) != list(range(len(part))):
            p.append(f"{k}: session numbers are not 0..n-1")
        rets, eq = part["daily_return"].to_numpy(), part["equity"].to_numpy()
        first, last = ts[0].isoformat(), ts[-1].isoformat()
        info = listed.get(k)
        if info is not None:
            got = (len(part), first, last, cost, int(sum(not math.isfinite(r) for r in rets)))
            want = (info["rows"], info["first_ts"], info["last_ts"], info["cost_scenario"],
                    info["non_finite_returns"])  # fmt: skip
            if got != want:
                p.append(f"{k}: rows/dates/cost/non-finite {got} differ from the manifest {want}")
            risk = cfg["risk_scenarios"].get(k[2], {})
            if info["risk_fingerprint"] != risk.get("fingerprint"):
                p.append(f"{k}: risk_fingerprint differs from config.risk_scenarios.{k[2]}")
            if info["backtest_fingerprint"] != slice_fp.get(k[0]):
                p.append(f"{k}: backtest_fingerprint differs from the slice configuration")
        w = windows.get(k[0])
        if not isinstance(w, dict) or (w.get("sessions"), w.get("first_ts"), w.get("last_ts")) != (
            len(part),
            first,
            last,
        ):
            p.append(f"{k}: sessions/dates differ from results.windows.{k[0]}")
        prev = [float(initial), *eq[:-1]]
        for i, (r, e, q) in enumerate(zip(rets, eq, prev, strict=True)):
            if not all(math.isfinite(v) for v in (r, e, q)):
                continue  # non-finite values are counted above, never recomputed
            if not math.isclose(r, e / q - 1, rel_tol=RETURN_TOLERANCE, abs_tol=1e-15):
                p.append(f"{k}: return at session {i} inconsistent with equity")
                break
    return df
