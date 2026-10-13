"""Selective, reproducible risk-research CLI: selection, exclusive output, manifest, fingerprint.

The suite runner and the bar loader are fakes: no backtest, no strategy, no database."""

import dataclasses
import json
import threading
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from app.risk import cli, research
from app.risk.diagnostics import METRIC_KEYS, RiskSuiteReport
from app.risk.engine import RiskRun
from app.validation.config import COST_SCENARIOS

SECRET = "s3cr3t-not-for-manifests-9f2c"
SELECT = [
    "--strategy", "sma_trend",
    "--scenario", "control_no_overlay", "fixed_fraction_50", "drawdown_lock_20", "atr_stop_3",
    "--cost", "D",
    "--slice", "full",
]  # fmt: skip


def make_bars(n: int = 6) -> pd.DataFrame:
    idx = pd.date_range("2024-01-02 14:30", periods=n, freq="D", tz="UTC", name="ts")
    base = np.arange(n, dtype="float64")
    return pd.DataFrame(
        {
            "open": 100.0 + base,
            "high": 101.5 + base,
            "low": 99.25 + base,
            "close": 100.75 + base,
            "volume": 1_000_000.0 + base,
            "adj_close": 100.5 + base,
        },
        index=idx,
    )


class FakeSuite:
    """Records the arguments it receives and returns exactly the requested runs (or `extra`)."""

    def __init__(self, extra: tuple[str, str, str] | None = None, fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.extra, self.fail = extra, fail

    def __call__(self, bars: pd.DataFrame, **kw: Any) -> RiskSuiteReport:
        self.calls.append(kw)
        if self.fail:
            raise RuntimeError("suite exploded")
        scen, bt = kw["scenarios"], kw["backtest"]
        report = RiskSuiteReport("1.0.0", bt.fingerprint(), {s.name: s.fingerprint() for s in scen})
        ids = [s.strategy_id for s in kw["strategy_engine"].strategies]
        keys = [(sl.name, sid, sc.name) for sl in kw["slices"] for sid in ids for sc in scen]
        if self.extra:
            keys.append(self.extra)
        for key in keys:
            report.runs[key] = RiskRun(
                key[1],
                key[2],
                pd.DataFrame(),
                pd.DataFrame({"daily_return": [0.0, 0.01]}, index=bars.index[:2]),
                pd.DataFrame(),
                pd.DataFrame(),
                None,
                None,
                {k: 0.0 for k in METRIC_KEYS},
                {},
            )
        if kw["compare"]:
            report.comparisons.append({"slice": "full", "metric": "sharpe", "estimate": 0.0})
        return report


@pytest.fixture(autouse=True)
def _fixed_code(monkeypatch: pytest.MonkeyPatch) -> None:
    code = {"app_version": "0.0.0", "git_commit": "c" * 40, "git_branch": "b", "git_dirty": False}
    monkeypatch.setattr(research, "code_info", lambda: dict(code))


def run_cli(
    argv: list[str], suite: FakeSuite | None = None, bars: pd.DataFrame | None = None
) -> tuple[int, list[str]]:
    calls: list[str] = []

    def loader() -> pd.DataFrame:
        calls.append("load")
        return make_bars() if bars is None else bars

    code = cli.main(argv, load_bars=loader, run_suite=suite or FakeSuite(), configure=False)
    return code, calls


def manifest(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((path / research.MANIFEST).read_text(encoding="utf-8"))
    return data


# ── 1. exclusive selection, applied before the suite runs ──


def test_selection_is_passed_exactly_to_the_suite(tmp_path: Path) -> None:
    suite = FakeSuite()
    code, _ = run_cli(["run", "--symbol", "spy", "--out", str(tmp_path / "r"), *SELECT], suite)
    assert code == cli.EXIT_OK
    (kw,) = suite.calls
    assert [s.name for s in kw["scenarios"]] == [
        "control_no_overlay", "fixed_fraction_50", "drawdown_lock_20", "atr_stop_3"
    ]  # fmt: skip
    assert [s.name for s in kw["slices"]] == ["full"]
    assert [s.strategy_id for s in kw["strategy_engine"].strategies] == ["sma_trend"]
    d = next(c for c in COST_SCENARIOS if c.name == "D_default")
    bt = kw["backtest"]
    assert (bt.slippage_bps, bt.spread_bps, bt.commission_per_trade) == (
        d.bps, d.spread_bps, d.per_order
    )  # fmt: skip
    assert kw["compare"] is True


def test_scenarios_and_slices_use_canonical_order_with_control_first() -> None:
    sel = research.resolve_selection(
        strategies=["sma_trend", "buy_and_hold"],
        scenarios=["atr_stop_3", "control_no_overlay"],
        cost="D_default",
        slices=["late", "full"],
    )
    assert sel.strategies == ("buy_and_hold", "sma_trend")
    assert [s.name for s in sel.scenarios] == ["control_no_overlay", "atr_stop_3"]
    assert [s.name for s in sel.slices] == ["full", "late"]
    assert sel.cost.name == "D_default" and sel.mode == "selective"


def test_cost_scenario_overrides_are_applied() -> None:
    sel = research.resolve_selection(
        strategies=["sma_trend"], scenarios=["control_no_overlay"], cost="B",
        slices=["full"], compare=False,
    )  # fmt: skip
    bt = sel.backtest_config()
    assert (bt.slippage_bps, bt.spread_bps, bt.commission_per_trade) == (5.0, 0.0, 0.0)


def test_no_selection_keeps_the_previous_full_suite() -> None:
    sel = research.resolve_selection(strategies=None, scenarios=None, cost=None, slices=None)
    assert sel.mode == "full_suite" and len(sel.strategies) == 6
    assert len(sel.scenarios) == 6 and len(sel.slices) == 3 and sel.cost.name == "D_default"


# ── 2/3. invalid selections fail before anything is read or written; no implicit widening ──


@pytest.mark.parametrize(
    "args",
    [
        ["--strategy", "nope", "--scenario", "control_no_overlay", "atr_stop_3", "--cost", "D",
         "--slice", "full"],
        ["--strategy", "sma_trend", "--scenario", "control_no_overlay", "bogus", "--cost", "D",
         "--slice", "full"],
        ["--strategy", "sma_trend", "--scenario", "control_no_overlay", "atr_stop_3",
         "--cost", "Z", "--slice", "full"],
        ["--strategy", "sma_trend", "--scenario", "control_no_overlay", "atr_stop_3",
         "--cost", "D", "--slice", "middle"],
        ["--strategy", "sma_trend", "sma_trend", "--scenario", "control_no_overlay",
         "atr_stop_3", "--cost", "D", "--slice", "full"],
        ["--strategy", "", "--scenario", "control_no_overlay", "atr_stop_3", "--cost", "D",
         "--slice", "full"],
        ["--strategy", "sma_trend", "--scenario", "atr_stop_3", "--cost", "D", "--slice", "full"],
        ["--strategy", "sma_trend", "--scenario", "control_no_overlay", "--cost", "D",
         "--slice", "full"],  # comparisons without an overlay
        ["--strategy", "sma_trend"],  # partial selection is never widened
        ["--strategy", "sma_trend", "--scenario", "control_no_overlay", "atr_stop_3",
         "--cost", "D"],
        ["--no-compare"],  # only meaningful for a selective run
    ],
)  # fmt: skip
def test_invalid_selection_is_refused_before_any_effect(tmp_path: Path, args: list[str]) -> None:
    suite = FakeSuite()
    out = tmp_path / "r"
    code, calls = run_cli(["run", "--symbol", "SPY", "--out", str(out), *args], suite)
    assert code == cli.EXIT_USAGE
    assert calls == [] and suite.calls == [] and not out.exists()


def test_option_without_values_is_a_usage_error(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        run_cli(["run", "--symbol", "SPY", "--out", str(tmp_path / "r"), "--strategy"])
    assert exc.value.code == 2


def test_control_only_without_comparisons_is_valid() -> None:
    sel = research.resolve_selection(
        strategies=["sma_trend"], scenarios=["control_no_overlay"], cost="D",
        slices=["full"], compare=False,
    )  # fmt: skip
    assert [s.name for s in sel.scenarios] == ["control_no_overlay"] and not sel.compare


def test_runs_outside_the_selection_fail_the_run(tmp_path: Path) -> None:
    out = tmp_path / "r"
    suite = FakeSuite(extra=("full", "buy_and_hold", "control_no_overlay"))
    code, _ = run_cli(["run", "--symbol", "SPY", "--out", str(out), *SELECT], suite)
    assert code == cli.EXIT_FAILED
    m = manifest(out)
    assert m["status"] == research.FAILED and "outside/missing" in m["error"]
    assert not (out / research.TABLE).exists()


def test_no_compare_writes_no_comparison_file(tmp_path: Path) -> None:
    out = tmp_path / "r"
    suite = FakeSuite()
    code, _ = run_cli(["run", "--symbol", "SPY", "--out", str(out), *SELECT, "--no-compare"], suite)
    assert code == cli.EXIT_OK and suite.calls[0]["compare"] is False
    assert sorted(p.name for p in out.iterdir()) == [research.MANIFEST, research.TABLE]


# ── 4/5. exclusive output directory ──


def test_existing_empty_directory_is_refused_and_untouched(tmp_path: Path) -> None:
    out = tmp_path / "r"
    out.mkdir()
    suite = FakeSuite()
    code, calls = run_cli(["run", "--symbol", "SPY", "--out", str(out), *SELECT], suite)
    assert code == cli.EXIT_OUTPUT_EXISTS
    assert list(out.iterdir()) == [] and calls == [] and suite.calls == []


def test_existing_results_are_never_overwritten(tmp_path: Path) -> None:
    out = tmp_path / "r"
    out.mkdir()
    (out / research.TABLE).write_text("previous", encoding="utf-8")
    code, _ = run_cli(["run", "--symbol", "SPY", "--out", str(out), *SELECT])
    assert code == cli.EXIT_OUTPUT_EXISTS
    assert [p.name for p in out.iterdir()] == [research.TABLE]
    assert (out / research.TABLE).read_text(encoding="utf-8") == "previous"


def test_existing_file_path_is_refused(tmp_path: Path) -> None:
    out = tmp_path / "r"
    out.write_text("x", encoding="utf-8")
    code, _ = run_cli(["run", "--symbol", "SPY", "--out", str(out), *SELECT])
    assert code == cli.EXIT_OUTPUT_EXISTS and out.read_text(encoding="utf-8") == "x"


def test_default_output_is_a_new_run_directory_per_run(tmp_path: Path) -> None:
    root = tmp_path / "risk"
    for _ in range(2):
        code, _ = run_cli(["run", "--symbol", "SPY", "--out-root", str(root), *SELECT])
        assert code == cli.EXIT_OK
    runs = sorted(p.name for p in root.iterdir())
    assert len(runs) == 2 and all(r.startswith("risk-") for r in runs)
    for r in runs:
        assert manifest(root / r)["run_id"] == r


def test_concurrent_creation_has_exactly_one_winner(tmp_path: Path) -> None:
    target = tmp_path / "race"
    n = 16
    barrier = threading.Barrier(n)
    results: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        try:
            research.create_run_dir(target)
            outcome = "created"
        except research.OutputExistsError:
            outcome = "refused"
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("created") == 1 and results.count("refused") == n - 1


def test_write_new_never_overwrites(tmp_path: Path) -> None:
    p = tmp_path / "f.csv"
    research.write_new(p, "a")
    with pytest.raises(FileExistsError):
        research.write_new(p, "b")
    assert p.read_text(encoding="utf-8") == "a"


# ── 6/7. data fingerprint ──


def test_fingerprint_is_deterministic_and_representation_independent() -> None:
    a, b = make_bars(), make_bars()
    fa = research.bars_fingerprint(a)
    assert fa == research.bars_fingerprint(b) == research.bars_fingerprint(a.copy())
    assert fa["algorithm"] == "sha256" and len(fa["value"]) == 64
    # column order, extra columns and the index time zone do not change the content
    shuffled = b[list(reversed(b.columns))].assign(extra=1.0)
    shuffled.index = pd.DatetimeIndex(shuffled.index).tz_convert("America/New_York")
    assert research.bars_fingerprint(shuffled)["value"] == fa["value"]


def _with(bars: pd.DataFrame, i: int, col: str, value: float) -> pd.DataFrame:
    out = bars.copy()
    out.loc[out.index[i], col] = value
    return out


@pytest.mark.parametrize("col", list(research.FINGERPRINT_COLUMNS))
def test_fingerprint_changes_with_any_relevant_field(col: str) -> None:
    bars = make_bars()
    base = research.bars_fingerprint(bars)["value"]
    nudged = float(np.nextafter(bars[col].iloc[3], np.inf))  # one ulp
    assert research.bars_fingerprint(_with(bars, 3, col, nudged))["value"] != base
    assert research.bars_fingerprint(_with(bars, 3, col, float("nan")))["value"] != base


def test_fingerprint_changes_with_the_bar_sequence() -> None:
    bars = make_bars()
    base = research.bars_fingerprint(bars)["value"]
    assert research.bars_fingerprint(bars.iloc[:-1])["value"] != base  # one bar fewer
    assert research.bars_fingerprint(bars.iloc[1:])["value"] != base
    moved = bars.copy()
    moved.index = moved.index + pd.Timedelta(seconds=1)
    assert research.bars_fingerprint(moved)["value"] != base


@pytest.mark.parametrize("bad", ["unsorted", "duplicate", "naive", "missing", "empty"])
def test_fingerprint_refuses_ambiguous_input(bad: str) -> None:
    bars = make_bars()
    if bad == "unsorted":
        bars = bars.iloc[::-1]
    elif bad == "duplicate":
        bars = pd.concat([bars, bars.iloc[[-1]]])
    elif bad == "naive":
        bars.index = pd.DatetimeIndex(bars.index).tz_localize(None)
    elif bad == "missing":
        bars = bars.drop(columns=["adj_close"])
    else:
        bars = bars.iloc[:0]
    with pytest.raises(ValueError):
        research.bars_fingerprint(bars)


# ── 8. manifest ──


def test_manifest_records_code_selection_config_input_and_artifacts(tmp_path: Path) -> None:
    out = tmp_path / "r"
    bars = make_bars()
    code, _ = run_cli(["run", "--symbol", "spy", "--out", str(out), *SELECT], bars=bars)
    assert code == cli.EXIT_OK
    m = manifest(out)
    assert m["schema_version"] == research.MANIFEST_SCHEMA and m["status"] == research.COMPLETE
    assert m["started_at"] and m["completed_at"] and m["error"] is None
    assert m["code"]["git_commit"] == "c" * 40 and m["code"]["git_branch"] == "b"
    assert m["selection"] == {
        "mode": "selective",
        "strategies": ["sma_trend"],
        "risk_scenarios": ["control_no_overlay", "fixed_fraction_50", "drawdown_lock_20",
                           "atr_stop_3"],
        "cost_scenario": "D_default",
        "slices": ["full"],
        "compare": True,
    }  # fmt: skip
    cfg = m["config"]
    assert cfg["backtest"]["config"]["commission_per_trade"] == 1.0
    assert cfg["backtest"]["config"]["slippage_bps"] == 2.0
    assert cfg["backtest"]["config"]["spread_bps"] == 2.0
    assert cfg["risk_scenarios"]["fixed_fraction_50"]["config"]["sizing"]["fraction"] == 0.5
    assert cfg["slices"] == [{"name": "full", "start": None, "end": None}]
    assert cfg["comparisons"]["n_resamples"] == 2000
    inp = m["input"]
    assert inp["symbol"] == "SPY" and inp["rows"] == len(bars)
    assert inp["first_ts"] == bars.index[0].isoformat()
    assert inp["last_ts"] == bars.index[-1].isoformat()
    assert inp["fingerprint"] == research.bars_fingerprint(bars)
    assert m["results"]["runs"] == 4 and m["results"]["table_rows"] == 4
    assert m["results"]["windows"]["full"]["sessions"] == 2
    paths = [a["path"] for a in m["artifacts"]]
    assert paths == [research.TABLE, research.COMPARISONS]
    for a in m["artifacts"]:
        data = (out / a["path"]).read_bytes()
        assert a["bytes"] == len(data)
        import hashlib

        assert a["sha256"] == hashlib.sha256(data).hexdigest()
    table = pd.read_csv(out / research.TABLE)
    assert set(table["strategy"]) == {"sma_trend"} and set(table["slice"]) == {"full"}
    assert not list(out.glob("*.tmp"))


# ── 9. failures are recorded, never reported as complete ──


def test_suite_failure_is_recorded(tmp_path: Path) -> None:
    out = tmp_path / "r"
    code, _ = run_cli(["run", "--symbol", "SPY", "--out", str(out), *SELECT], FakeSuite(fail=True))
    assert code == cli.EXIT_FAILED
    m = manifest(out)
    assert m["status"] == research.FAILED and "suite exploded" in m["error"]
    assert m["artifacts"] == [] and not (out / research.TABLE).exists()


def test_loader_failure_is_recorded(tmp_path: Path) -> None:
    out = tmp_path / "r"

    def boom() -> pd.DataFrame:
        raise ConnectionError("database unavailable")

    code = cli.main(["run", "--symbol", "SPY", "--out", str(out), *SELECT],
                    load_bars=boom, run_suite=FakeSuite(), configure=False)  # fmt: skip
    assert code == cli.EXIT_FAILED
    m = manifest(out)
    assert m["status"] == research.FAILED and m["input"] is None


def test_result_write_failure_is_recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / "r"

    def disk_full(path: Path, text: str) -> None:
        raise OSError("No space left on device")

    monkeypatch.setattr(research, "write_new", disk_full)
    code, _ = run_cli(["run", "--symbol", "SPY", "--out", str(out), *SELECT])
    assert code == cli.EXIT_FAILED
    m = manifest(out)
    assert m["status"] == research.FAILED and "No space left" in m["error"]


def test_final_manifest_failure_never_leaves_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "r"
    real = research.write_manifest

    def flaky(run_dir: Path, m: dict[str, Any]) -> None:
        if m["status"] != research.RUNNING:
            raise OSError("manifest write failed")
        real(run_dir, m)

    monkeypatch.setattr(research, "write_manifest", flaky)
    code, _ = run_cli(["run", "--symbol", "SPY", "--out", str(out), *SELECT])
    assert code == cli.EXIT_FAILED
    assert manifest(out)["status"] == research.RUNNING  # stale, but never 'complete'


# ── 10. no secrets in the manifest or the output ──


def test_no_secrets_in_manifest_or_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("POSTGRES_PASSWORD", SECRET)
    monkeypatch.setenv("DATABASE_URL", f"postgresql://u:{SECRET}@localhost:5432/quant")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", SECRET)
    out = tmp_path / "r"
    code, _ = run_cli(["run", "--symbol", "SPY", "--out", str(out), *SELECT])
    assert code == cli.EXIT_OK
    captured = capsys.readouterr()
    for path in out.iterdir():
        assert SECRET not in path.read_text(encoding="utf-8")
    assert SECRET not in captured.out and SECRET not in captured.err
    text = (out / research.MANIFEST).read_text(encoding="utf-8").lower()
    assert "password" not in text and "postgresql://" not in text


def test_selection_dataclass_is_frozen() -> None:
    sel = research.resolve_selection(strategies=None, scenarios=None, cost=None, slices=None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        sel.mode = "x"  # type: ignore[misc]


@pytest.mark.parametrize("cost", COST_SCENARIOS, ids=lambda c: c.name)
def test_cost_overrides_match_the_declared_scenario(cost: Any) -> None:
    sel = research.resolve_selection(
        strategies=["sma_trend"],
        scenarios=["control_no_overlay"],
        cost=cost.name,
        slices=["full"],
        compare=False,
    )
    bt = sel.backtest_config()
    assert {k: getattr(bt, k) for k in cost.backtest_overrides()} == cost.backtest_overrides()
