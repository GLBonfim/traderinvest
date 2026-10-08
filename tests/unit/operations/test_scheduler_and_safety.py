"""Scheduler (frozen clock, injected sleep — no test waits), XNYS session helpers, and the
safety/network boundary of the operations package."""

import ast
import itertools
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from app.core.config import TradingMode
from app.data.calendar import TradingCalendar
from app.operations import models as m
from app.operations import sessions as cal_
from app.operations.config import OperationsConfig
from app.operations.pipeline import Pipeline
from app.operations.scheduler import Scheduler, scheduler_status
from app.operations.store import OpsStore
from tests.unit.operations.fakes import FakeServices, at

CAL = TradingCalendar("XNYS")
CFG = OperationsConfig(grace_minutes=30, scheduler_poll_seconds=300)
REPO = Path(__file__).resolve().parents[3]


# ── calendar helpers ──


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        ("2024-07-02T20:29:00", date(2024, 7, 1)),  # close 20:00 UTC + 30 min not yet passed
        ("2024-07-02T20:30:00", date(2024, 7, 2)),
        ("2024-07-03T17:29:00", date(2024, 7, 2)),  # early close 13:00 ET = 17:00 UTC
        ("2024-07-03T17:30:00", date(2024, 7, 3)),
        ("2024-07-04T23:59:00", date(2024, 7, 3)),  # holiday
        ("2024-07-07T12:00:00", date(2024, 7, 5)),  # Sunday
        ("2024-12-02T21:31:00", date(2024, 12, 2)),  # EST: close 21:00 UTC
        (
            "2024-12-02T21:29:00",
            date(2024, 11, 29),
        ),  # previous session is the day after Thanksgiving
    ],
)
def test_latest_eligible_session(now: str, expected: date) -> None:
    assert cal_.latest_eligible_session(CAL, at(now), 30) == expected


def test_session_sequence_and_next_eligibility() -> None:
    assert cal_.sessions_after(CAL, date(2024, 7, 3), date(2024, 7, 9)) == [
        date(2024, 7, 5),
        date(2024, 7, 8),
        date(2024, 7, 9),
    ]
    assert cal_.previous_session(CAL, date(2024, 7, 8)) == date(2024, 7, 5)
    assert cal_.next_eligibility(CAL, at("2024-07-03T12:00:00"), 30) == at("2024-07-03T17:30:00")
    assert cal_.next_eligibility(CAL, at("2024-07-03T18:00:00"), 30) == at("2024-07-05T20:30:00")
    with pytest.raises(ValueError):
        cal_.latest_eligible_session(CAL, datetime(2024, 7, 3), 30)  # naive time rejected


# ── scheduler ──


class Clock:
    def __init__(self, start: str) -> None:
        self.t = at(start)
        self.slept: list[float] = []

    def __call__(self) -> datetime:
        return self.t

    def sleep(self, seconds: float) -> None:  # advances the frozen clock; never waits
        self.slept.append(seconds)
        self.t += timedelta(seconds=seconds)


def make(tmp: Path, svc: FakeServices, clock: Clock) -> Scheduler:
    store = OpsStore(tmp)

    def factory() -> Pipeline:
        return Pipeline(CFG, svc, OpsStore(tmp), clock=clock, calendar=CAL)

    return Scheduler(CFG, factory, store, clock=clock, sleep=clock.sleep, calendar=CAL)


def test_scheduler_runs_only_when_a_session_becomes_eligible(tmp_path: Path) -> None:
    svc = FakeServices(stored={date(2024, 7, 1)}, published={date(2024, 7, 2)})
    clock = Clock("2024-07-02T19:50:00")  # before the close
    sch = make(tmp_path, svc, clock)
    ticks = sch.run_forever(max_ticks=12)  # 19:50 .. 20:45 every 5 minutes
    ran = [(t.at[11:16], t.run_status) for t in ticks if t.ran]
    # first tick: no checkpoint yet -> verification run of the stored 07-01;
    # 20:00-20:25: closed but inside the grace -> not due; 20:30: 07-02 eligible -> one run
    assert ran == [("19:50", m.COMPLETED), ("20:30", m.COMPLETED)]
    assert all(not t.ran for t in ticks[1:8])
    assert len(clock.slept) == 11 and set(clock.slept) == {300}
    assert [c[1] for c in svc.calls if c[0] == "paper"] == [date(2024, 7, 1), date(2024, 7, 2)]
    st = scheduler_status(OpsStore(tmp_path), clock.t, 300)
    assert st["status"] == "running" and st["next_eligible_at"].startswith("2024-07-03T17:30")


def test_scheduler_over_holiday_weekend_and_downtime(tmp_path: Path) -> None:
    svc = FakeServices(
        stored={date(2024, 7, 2)}, published={date(2024, 7, d) for d in (3, 5, 8, 9)}
    )
    clock = Clock("2024-07-02T21:00:00")
    sch = make(tmp_path, svc, clock)
    sch.tick()
    clock.t = at("2024-07-04T15:00:00")  # holiday: 07-03 (early close) eligible since 17:30
    assert sch.tick().ran
    clock.t = at("2024-07-04T22:00:00")
    assert not sch.tick().ran  # holiday: nothing new
    clock.t = at("2024-07-06T12:00:00")
    assert sch.tick().ran  # Friday 07-05
    clock.t = at("2024-07-07T12:00:00")
    assert not sch.tick().ran  # Sunday
    clock.t = at("2024-07-10T08:00:00")  # machine was off Mon/Tue -> catch-up in order
    t = sch.tick()
    assert t.ran and t.run_status == m.COMPLETED
    papers = [c[1] for c in svc.calls if c[0] == "paper"]
    assert papers == sorted(papers) and papers[-2:] == [date(2024, 7, 8), date(2024, 7, 9)]


def test_scheduler_late_data_retry_spacing(tmp_path: Path) -> None:
    svc = FakeServices(stored={date(2024, 7, 1)}, published=set())
    clock = Clock("2024-07-02T20:31:00")
    sch = make(tmp_path, svc, clock)
    statuses = [(t.at[11:16], t.run_status) for t in sch.run_forever(max_ticks=24) if t.ran]
    # first run: verification of 07-01 + 07-02 missing (attempt 1); retries every >= 30 min;
    # after 3 attempts: no more automatic attempts for 07-02 until a newer session is eligible
    assert [s for _, s in statuses] == [m.WAITING_FOR_DATA, m.WAITING_FOR_DATA, m.RUN_FAILED]
    times = [datetime.strptime(x, "%H:%M") for x, _ in statuses]
    assert all((b - a) >= timedelta(minutes=30) for a, b in itertools.pairwise(times))
    assert ("paper", date(2024, 7, 2)) not in svc.calls


def test_scheduler_status_without_heartbeat(tmp_path: Path) -> None:
    assert scheduler_status(None, at("2024-07-02T21:00:00"), 300)["status"].startswith(
        "not running"
    )
    st = OpsStore(tmp_path)
    st.heartbeat({"last_check": "2024-07-02T20:00:00+00:00"})
    assert scheduler_status(st, at("2024-07-02T21:00:00"), 300)["status"].startswith("stopped")


# ── safety / network boundary ──

NETWORK = {
    "requests",
    "httpx",
    "httpx2",
    "urllib",
    "urllib3",
    "http",
    "aiohttp",
    "websocket",
    "websockets",
    "grpc",
    "smtplib",
    "ftplib",
}
VENDORS = {
    "alpaca",
    "alpaca_trade_api",
    "ib_insync",
    "ib_async",
    "ibapi",
    "ccxt",
    "anthropic",
    "openai",
    "apscheduler",
    "celery",
    "redis",
    "schedule",
    "win32com",
}
TRADING = {"app.paper.broker", "app.paper.trader", "app.risk.manager", "app.risk.engine"}


def _imports(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            out.add(node.module or "")
    return out


def test_operations_has_no_ad_hoc_network_scheduler_infra_or_trading_imports() -> None:
    files = sorted((REPO / "app" / "operations").glob("*.py"))
    assert len(files) >= 9
    for f in files:
        imps = _imports(f)
        roots = {i.split(".")[0] for i in imps}
        assert not roots & (NETWORK | VENDORS), f
        assert not imps & TRADING, f
        if "socket" in roots:
            assert f.name in ("lock.py", "scheduler.py"), f  # hostname only
    src = "\n".join(f.read_text(encoding="utf-8") for f in files)
    for word in (
        "schtasks",
        "crontab",
        "place_order",
        "submit_order",
        "PaperBroker",
        "refuse_real_money_order",
    ):
        assert word not in src, word


def test_trading_modes_unchanged() -> None:
    assert {x.value for x in TradingMode} == {"disabled", "paper"}
