"""Pipeline orchestration with a fake Services: session awareness, ordering, idempotency,
failure policy per stage, crash recovery, catch-up, locking, dry-run, operational alerts."""

import hashlib
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from app.data.calendar import TradingCalendar
from app.operations import models as m
from app.operations.config import OperationsConfig
from app.operations.lock import PipelineLock
from app.operations.pipeline import Pipeline
from app.operations.store import OpsStore, OpsStoreError
from tests.unit.operations.fakes import Crash, FakeServices, at

CAL = TradingCalendar("XNYS")
CFG = OperationsConfig(
    grace_minutes=30,
    max_catch_up_sessions=10,
    data_retry_attempts=3,
    data_retry_interval_minutes=30,
)
# July 2024: 07-01 Mon, 07-02 Tue, 07-03 Wed (early close 17:00 UTC), 07-04 holiday,
# 07-05 Fri, weekend, 07-08 Mon, 07-09 Tue ... regular close 20:00 UTC (EDT)
D = {n: date(2024, 7, n) for n in (1, 2, 3, 5, 8, 9, 10, 11, 12)}


def pipe(tmp: Path, svc: FakeServices, now: str, cfg: OperationsConfig = CFG) -> Pipeline:
    return Pipeline(cfg, svc, OpsStore(tmp), clock=lambda: at(now), calendar=CAL)


def base(stored_until: int = 1, published: tuple[int, ...] = (2, 3, 5, 8, 9)) -> FakeServices:
    return FakeServices(
        stored={D[1]} if stored_until >= 1 else set(), published={D[n] for n in published}
    )


def dupes(svc: FakeServices) -> list[tuple[str, str, date]]:
    return [e for e in svc.effects if svc.effects.count(e) > 1]


# ── session awareness ──


def test_completed_session_rule_and_first_run_verification(tmp_path: Path) -> None:
    svc = base()
    # Tue 07-02 19:00 UTC: 07-02 not closed yet -> only 07-01 (stored) gets a verification pass
    r = pipe(tmp_path, svc, "2024-07-02T19:00:00").run()
    assert r.status == m.COMPLETED and r.target_sessions == ["2024-07-01"]
    s = r.sessions[0]
    assert [st.stage for st in s.stages] == [m.INGEST, m.VALIDATE, m.PAPER, m.ALERTS]
    assert s.stages[0].status == m.SKIPPED  # bar already durable: no ingestion
    assert ("ingest", D[1]) not in svc.calls
    # 20:15 UTC: closed but inside the 30-minute grace -> nothing new
    assert pipe(tmp_path, svc, "2024-07-02T20:15:00").run().status == m.NO_NEW_COMPLETED_SESSION
    # 20:31 UTC: eligible
    r2 = pipe(tmp_path, svc, "2024-07-02T20:31:00").run()
    assert r2.target_sessions == ["2024-07-02"] and r2.status == m.COMPLETED


def test_early_close_holiday_and_weekend(tmp_path: Path) -> None:
    svc = base()
    pipe(tmp_path, svc, "2024-07-02T21:00:00").run()
    # early close 07-03 at 17:00 UTC: eligible from 17:30 UTC, not 20:30
    assert pipe(tmp_path, svc, "2024-07-03T17:20:00").run().status == m.NO_NEW_COMPLETED_SESSION
    assert pipe(tmp_path, svc, "2024-07-03T17:31:00").run().target_sessions == ["2024-07-03"]
    # Independence Day: no session; Friday 07-05 is next; Saturday/Sunday are not sessions
    assert pipe(tmp_path, svc, "2024-07-04T23:00:00").run().status == m.NO_NEW_COMPLETED_SESSION
    assert pipe(tmp_path, svc, "2024-07-06T23:00:00").run().target_sessions == ["2024-07-05"]
    assert pipe(tmp_path, svc, "2024-07-07T23:00:00").run().status == m.NO_NEW_COMPLETED_SESSION
    assert pipe(tmp_path, svc, "2024-07-08T20:00:00").run().status == m.NO_NEW_COMPLETED_SESSION
    assert pipe(tmp_path, svc, "2024-07-08T21:00:00").run().target_sessions == ["2024-07-08"]


# ── idempotency / catch-up ──


def test_catch_up_in_order_and_second_run_is_noop(tmp_path: Path) -> None:
    svc = base()
    r = pipe(tmp_path, svc, "2024-07-09T22:00:00").run()  # machine was off since 07-01
    assert r.target_sessions == [
        "2024-07-01",
        "2024-07-02",
        "2024-07-03",
        "2024-07-05",
        "2024-07-08",
        "2024-07-09",
    ]
    order = [c for c in svc.calls if c[0] in ("ingest", "paper", "alerts")]
    sessions = [c[1] for c in order]
    assert sessions == sorted(sessions)  # each session fully before the next
    assert [c[0] for c in order[:3]] == ["paper", "alerts", "ingest"]  # 07-01 then 07-02 ingest
    assert {d for (_, acct, d) in svc.effects if acct == "acct_a"} == {
        D[n] for n in (1, 2, 3, 5, 8, 9)
    }
    before = (list(svc.effects), len(OpsStore(tmp_path).events()))
    again = pipe(tmp_path, svc, "2024-07-09T22:05:00").run()
    assert again.status == m.NO_NEW_COMPLETED_SESSION
    assert (svc.effects, len(OpsStore(tmp_path).events())) == before  # no duplicate anything
    assert OpsStore(tmp_path).state["counters"]["noop_runs"] == 1


def test_max_catch_up_processes_oldest_first_and_alerts(tmp_path: Path) -> None:
    svc = base(published=(2, 3, 5, 8, 9, 10, 11, 12))
    cfg = OperationsConfig(max_catch_up_sessions=3)
    r = pipe(tmp_path, svc, "2024-07-12T22:00:00", cfg).run()
    assert r.target_sessions == ["2024-07-01", "2024-07-02", "2024-07-03"]
    assert [a.event_type for a in svc.ops_alerts] == ["OPS_CATCH_UP_REQUIRED"]
    r2 = pipe(tmp_path, svc, "2024-07-12T22:01:00", cfg).run()
    assert r2.target_sessions == ["2024-07-05", "2024-07-08", "2024-07-09"]
    assert not dupes(svc)


# ── failure policy ──


def test_ingestion_failure_stops_before_paper(tmp_path: Path) -> None:
    svc = base()
    pipe(tmp_path, svc, "2024-07-01T21:00:00").run()
    svc.fail["INGEST"] = ConnectionError("provider down")
    r = pipe(tmp_path, svc, "2024-07-03T21:00:00").run()
    assert r.status == m.RUN_FAILED and [s.session for s in r.sessions] == ["2024-07-02"]
    assert ("paper", D[2]) not in svc.calls and ("ingest", D[3]) not in svc.calls
    assert OpsStore(tmp_path).last_completed_session == D[1]
    assert [a.event_type for a in svc.ops_alerts] == ["OPS_STAGE_FAILED"]
    assert "provider down" in svc.ops_alerts[0].message
    del svc.fail["INGEST"]
    r2 = pipe(tmp_path, svc, "2024-07-03T21:05:00").run()  # recovery
    assert r2.target_sessions == ["2024-07-02", "2024-07-03"] and r2.status == m.COMPLETED
    assert not dupes(svc)


def test_late_data_bounded_retries_then_data_missing(tmp_path: Path) -> None:
    svc = base(published=())
    pipe(tmp_path, svc, "2024-07-01T21:00:00").run()
    r1 = pipe(tmp_path, svc, "2024-07-02T20:31:00").run(mode="scheduled")
    assert r1.status == m.WAITING_FOR_DATA and ("paper", D[2]) not in svc.calls
    # scheduled: retry interval not elapsed -> not due (nothing called)
    n = len(svc.calls)
    assert pipe(tmp_path, svc, "2024-07-02T20:45:00").run(mode="scheduled").status == m.NOT_DUE
    assert [c for c in svc.calls[n:] if c[0] != "precheck"] == []
    assert (
        pipe(tmp_path, svc, "2024-07-02T21:02:00").run(mode="scheduled").status
        == m.WAITING_FOR_DATA
    )
    r3 = pipe(tmp_path, svc, "2024-07-02T21:33:00").run(mode="scheduled")
    assert r3.status == m.RUN_FAILED and r3.sessions[0].status == m.SESSION_DATA_MISSING
    assert "OPS_SESSION_DATA_MISSING" in [a.event_type for a in svc.ops_alerts]
    # exhausted: scheduled runs wait for the next eligible session, manual runs still try
    assert pipe(tmp_path, svc, "2024-07-02T23:00:00").run(mode="scheduled").status == m.NOT_DUE
    svc.published.add(D[2])
    r4 = pipe(tmp_path, svc, "2024-07-02T23:30:00").run()  # manual
    assert r4.status == m.COMPLETED and ("paper", D[2]) in svc.calls
    assert not any(e[2] == D[2] and e[0] == "paper" for e in svc.effects[:0])
    assert OpsStore(tmp_path).data_attempts(D[2]) == {"attempts": 0}  # cleared on completion


def test_invalid_bar_fails_without_paper(tmp_path: Path) -> None:
    svc = base()
    svc.invalid.add(D[2])
    r = pipe(tmp_path, svc, "2024-07-02T21:00:00").run()
    assert r.status == m.RUN_FAILED and ("paper", D[2]) not in svc.calls
    assert r.sessions[-1].stages[-1].stage == m.VALIDATE


def test_paper_account_failures_are_isolated(tmp_path: Path) -> None:
    svc = base()
    svc.failing_accounts = {"acct_b"}
    r = pipe(tmp_path, svc, "2024-07-02T21:00:00").run()
    assert r.status == m.COMPLETED_WITH_WARNINGS
    assert {d for (_, a, d) in svc.effects if a == "acct_a"} == {D[1], D[2]}
    assert not [e for e in svc.effects if e[1] == "acct_b"]
    assert OpsStore(tmp_path).last_completed_session == D[2]
    assert {a.account_id for a in svc.ops_alerts} == {"acct_b"}
    svc.failing_accounts = set()  # repaired: the account catches up in order next session
    pipe(tmp_path, svc, "2024-07-03T21:00:00").run()
    assert sorted(d for (_, a, d) in svc.effects if a == "acct_b") == [D[1], D[2], D[3]]


def test_paper_stage_failure_does_not_advance(tmp_path: Path) -> None:
    svc = base()
    svc.fail["PAPER"] = RuntimeError("bars unreadable")
    r = pipe(tmp_path, svc, "2024-07-01T21:00:00").run()
    assert r.status == m.RUN_FAILED and OpsStore(tmp_path).last_completed_session is None
    del svc.fail["PAPER"]
    assert pipe(tmp_path, svc, "2024-07-01T21:10:00").run().status == m.COMPLETED


@pytest.mark.parametrize("stage", ["ALERTS", "HEALTH"])
def test_alert_and_health_failures_never_roll_back(tmp_path: Path, stage: str) -> None:
    svc = base()
    svc.fail[stage] = RuntimeError(f"{stage} broken")
    r = pipe(tmp_path, svc, "2024-07-02T21:00:00").run()
    assert r.status == m.COMPLETED_WITH_WARNINGS
    assert OpsStore(tmp_path).last_completed_session == D[2]
    assert {d for (_, a, d) in svc.effects if a == "acct_a"} == {D[1], D[2]}


def test_precheck_failure_stops_everything(tmp_path: Path) -> None:
    svc = base()
    svc.precheck_ok = False
    r = pipe(tmp_path, svc, "2024-07-02T21:00:00").run()
    assert r.status == m.PRECHECK_FAILED and svc.mutating_calls() == []
    assert "database" in r.errors[0]


def test_repeated_failure_alert_once_at_threshold(tmp_path: Path) -> None:
    svc = base()
    svc.precheck_ok = False
    for i in range(5):
        pipe(tmp_path, svc, f"2024-07-02T21:0{i}:00").run()
    assert [a.event_type for a in svc.ops_alerts].count("OPS_REPEATED_FAILURE") == 1


# ── crash recovery ──


@pytest.mark.parametrize("stage", ["INGEST", "PAPER", "PAPER_AFTER_COMMIT", "ALERTS"])
def test_crash_then_restart_has_no_duplicate_effects(tmp_path: Path, stage: str) -> None:
    svc = base()
    pipe(tmp_path, svc, "2024-07-01T21:00:00").run()
    svc.crash[(stage, D[3])] = 1
    with pytest.raises(Crash):
        pipe(tmp_path, svc, "2024-07-05T21:00:00").run()
    assert not (tmp_path / "pipeline.lock").exists()  # released on the way out
    hist = OpsStore(tmp_path).history()
    assert hist[0]["status"] == "INTERRUPTED"
    r = pipe(tmp_path, svc, "2024-07-05T21:01:00").run()  # restart
    assert r.status == m.COMPLETED
    assert not dupes(svc)
    assert {d for (_, a, d) in svc.effects if a == "acct_a"} == {D[1], D[2], D[3], D[5]}
    assert svc.alert_sessions == sorted(svc.alert_sessions) and D[3] in svc.alert_sessions


def test_crash_before_recording_completion(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    svc = base()

    def boom(*a, **k):  # type: ignore[no-untyped-def]
        raise Crash("crash before run_finished")

    monkeypatch.setattr(OpsStore, "finish_run", boom)
    with pytest.raises(Crash):
        pipe(tmp_path, svc, "2024-07-02T21:00:00").run()
    monkeypatch.undo()
    effects = list(svc.effects)
    assert pipe(tmp_path, svc, "2024-07-02T21:05:00").run().status == m.NO_NEW_COMPLETED_SESSION
    assert svc.effects == effects


# ── locking ──


def test_lock_contention_blocks_a_second_run(tmp_path: Path) -> None:
    svc = base()
    holder = PipelineLock(tmp_path)
    holder.acquire()
    try:
        r = pipe(tmp_path, svc, "2024-07-02T21:00:00").run()
    finally:
        holder.release()
    assert r.status == m.LOCKED and svc.mutating_calls() == []
    assert [a.event_type for a in svc.ops_alerts] == ["OPS_LOCK_CONFLICT"]
    assert pipe(tmp_path, svc, "2024-07-02T21:01:00").run().status == m.COMPLETED


def test_stale_lock_of_dead_local_process_is_broken(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"],
        capture_output=True,
        text=True,
        check=True,
    )
    dead_pid = int(proc.stdout.strip())
    import json
    import socket

    (tmp_path / "pipeline.lock").write_text(
        json.dumps({"lock_id": "old", "pid": dead_pid, "host": socket.gethostname()})
    )
    r = pipe(tmp_path, base(), "2024-07-02T21:00:00").run()
    assert r.status == m.COMPLETED_WITH_WARNINGS  # breaking a stale lock is reported
    assert any(f"stale lock of dead process {dead_pid}" in w for w in r.warnings)
    assert not (tmp_path / "pipeline.lock").exists()


def test_foreign_or_live_locks_are_never_broken(tmp_path: Path) -> None:
    import json
    import os

    for owner in (
        {"lock_id": "x", "pid": os.getpid(), "host": "other-host"},
        {"lock_id": "y", "pid": os.getpid(), "host": __import__("socket").gethostname()},
    ):
        (tmp_path / "pipeline.lock").write_text(json.dumps(owner))
        assert pipe(tmp_path, base(), "2024-07-02T21:00:00").run().status == m.LOCKED
        assert (tmp_path / "pipeline.lock").exists()
    removed = PipelineLock(tmp_path).force_release()
    assert removed is not None and removed["lock_id"] == "y"


def test_release_only_removes_own_lock(tmp_path: Path) -> None:
    a = PipelineLock(tmp_path)
    a.acquire()
    (tmp_path / "pipeline.lock").write_text('{"lock_id": "someone-else"}')
    a.release()
    assert (tmp_path / "pipeline.lock").exists()


# ── dry-run / identity / persistence ──


def test_dry_run_changes_nothing(tmp_path: Path) -> None:
    svc = base()
    pipe(tmp_path, svc, "2024-07-01T21:00:00").run()
    digest = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.iterdir()}
    calls = len(svc.mutating_calls())
    r = pipe(tmp_path, svc, "2024-07-05T21:00:00").run(mode="dry_run")
    assert r.status == m.DRY_RUN and r.target_sessions == ["2024-07-02", "2024-07-03", "2024-07-05"]
    assert [s["ingest"] for s in r.plan["sessions"]] == ["ingest from provider"] * 3
    assert len(r.plan["paper_accounts"]) == 2 and r.plan["alerts_would_run"]
    assert len(svc.mutating_calls()) == calls
    assert {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.iterdir()
    } == digest


def test_dry_run_on_a_fresh_machine_creates_no_files(tmp_path: Path) -> None:
    root = tmp_path / "ops"
    svc = base()
    r = Pipeline(
        CFG,
        svc,
        OpsStore(root, no_write=True),
        clock=lambda: at("2024-07-02T21:00:00"),
        calendar=CAL,
    ).run(mode="dry_run")
    assert r.status == m.DRY_RUN and r.target_sessions == ["2024-07-01", "2024-07-02"]
    assert not root.exists() and svc.mutating_calls() == []
    with pytest.raises(OpsStoreError, match="without write access"):
        OpsStore(root, no_write=True).bump("runs")


def test_run_ids_do_not_depend_on_data(tmp_path: Path) -> None:
    r = pipe(tmp_path, base(), "2024-07-02T21:00:00").run()
    assert r.run_id.startswith("ops-20240701-20240702-" + CFG.fingerprint()[:8])
    assert r.config_fingerprint == CFG.fingerprint()


def test_history_and_corruption(tmp_path: Path) -> None:
    svc = base()
    pipe(tmp_path, svc, "2024-07-02T21:00:00").run()
    h = OpsStore(tmp_path).history()
    assert h[0]["status"] == m.COMPLETED and h[0]["sessions"] == {
        "2024-07-01": "COMPLETED",
        "2024-07-02": "COMPLETED",
    }
    stages = [s["stage"] for s in h[0]["stages"]]
    assert stages[0] == m.PRECHECK and stages[-2:] == [m.HEALTH, m.COMPLETE]
    assert len(stages) == len(set(zip(stages, [s["session"] for s in h[0]["stages"]], strict=True)))
    metrics = OpsStore(tmp_path).metrics()
    assert metrics["runs"] == 1 and metrics["sessions_processed"] == 2
    path = tmp_path / "runs.jsonl"
    path.write_text(path.read_text().replace('"COMPLETED"', '"FAILED"', 1))
    with pytest.raises(OpsStoreError, match="hash chain"):
        OpsStore(tmp_path)
