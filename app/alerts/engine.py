"""Alert evaluation run: existing domain outputs -> rules -> store (dedup) -> delivery.

    existing domain event -> alert rule -> alert event -> delivery channel

Nothing here calls the paper broker, a store's processing method, an order function or any
engine with side effects; the run only reads (database, engine outputs, paper ledgers) and
writes to the alert store. A delivery failure is recorded and never affects anything else.

Reporting window: a new store starts at `since` (default: the latest stored session), so the
first run does not replay decades of history; bar-based alerts are reported for sessions on or
after `since`, database events detected on or after it. Rules always see the full history up
to each session (state machines are point-in-time), only reporting is limited.
"""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from app import __version__
from app.alerts import rules
from app.alerts.channels import Channel, DeliveryReport, configured_channels, deliver_pending
from app.alerts.models import AlertEvent, make_alert
from app.alerts.monitor import (
    DEGRADED,
    FAILED,
    OK,
    component,
    ingestion_runs_since,
    paper_account_dirs,
    quality_events_since,
    read_paper_account,
    summarize,
    trading_mode_ok,
)
from app.alerts.store import AlertStore, AlertStoreError
from app.backtest.engine import session_times
from app.core.config import Settings
from app.data.calendar import TradingCalendar
from app.regimes.engine import RegimeEngine
from app.strategies.engine import StrategyEngine

OHLCV = ["open", "high", "low", "close", "volume"]
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ALERTS_ROOT = REPO_ROOT / "data" / "alerts"
DEFAULT_PAPER_ROOT = REPO_ROOT / "data" / "paper" / "accounts"


def market_alerts(
    bars: pd.DataFrame, *, instrument: str, since: pd.Timestamp | None
) -> list[AlertEvent]:
    """Regime, strategy-state and missing-session alerts from the stored bars (causal engines;
    an alert at T depends only on bars <= T)."""
    ohlcv = bars[OHLCV]
    cal = TradingCalendar("XNYS")
    idx = pd.DatetimeIndex(bars.index)
    times = session_times(idx, cal)
    regimes = RegimeEngine().analyze(ohlcv).state
    signals = StrategyEngine().run(ohlcv).signals
    expected = pd.DatetimeIndex(cal.sessions(idx[0].date(), idx[-1].date())["open_utc"])
    return [
        *rules.regime_alerts(
            regimes,
            times["observed_at"],
            instrument=instrument,
            since=since,
            app_version=__version__,
        ),
        *rules.strategy_alerts(
            signals, instrument=instrument, since=since, app_version=__version__
        ),
        *rules.missing_session_alerts(
            idx, expected, instrument=instrument, since=since, app_version=__version__
        ),
    ]


def condition_alert(
    store: AlertStore, name: str, active: bool, now: datetime, build: Any
) -> AlertEvent | None:
    """Episode semantics for wall-clock conditions: one alert when a condition starts (keyed by
    the episode start), none while it persists, re-armed once it clears."""
    current = store.condition(name)
    if active and current is None:
        start = now.isoformat()
        store.set_condition(name, {"since": start})
        return build(start)  # type: ignore[no-any-return]
    if not active and current is not None:
        store.set_condition(name, None)
    return None


def db_events(
    instrument_id: int, start: date
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """(ingestion runs since start, quality events since start, [latest run])."""
    from app.database.session import get_session_factory

    with get_session_factory()() as s:
        return (
            ingestion_runs_since(s, instrument_id, start),
            quality_events_since(s, start),
            ingestion_runs_since(s, instrument_id, date.min)[-1:],
        )


@dataclass
class RunResult:
    evaluated_at: str
    since: str | None
    candidates: int
    new_alerts: list[AlertEvent]
    delivery: DeliveryReport | None
    health: dict[str, Any] = field(default_factory=dict)


def run_alerts(
    *,
    now: datetime | None = None,
    alerts_root: Path = DEFAULT_ALERTS_ROOT,
    paper_root: Path = DEFAULT_PAPER_ROOT,
    settings: Settings,
    load_market: Any,
    dashboard_results_missing: Any = None,
    load_db_events: Any = None,
    channels: list[Channel] | None = None,
    deliver: bool = True,
    since: date | None = None,
) -> RunResult:
    """`load_market()` -> (instrument_id, symbol, bars, freshness) or raises on DB failure;
    `dashboard_results_missing()` -> list of missing persisted result names (optional)."""
    now = now or datetime.now(UTC)
    store = AlertStore(alerts_root, since=since)
    candidates: list[AlertEvent] = []
    health: dict[str, Any] = {}
    av = __version__

    def mk(
        etype: str,
        key: tuple[Any, ...],
        title: str,
        message: str,
        payload: dict[str, Any],
        **kw: Any,
    ) -> AlertEvent:
        return make_alert(
            etype,
            key_parts=key,
            occurred_at=kw.pop("occurred_at", now.isoformat()),
            title=title,
            message=message,
            payload=payload,
            app_version=av,
            **kw,
        )

    # safety: trading mode
    mode_ok = trading_mode_ok(settings)
    health["safety"] = component(OK if mode_ok else FAILED, f"trading mode {settings.trading_mode}")
    a = condition_alert(
        store,
        "unsupported_trading_mode",
        not mode_ok,
        now,
        lambda s: mk(
            "SYSTEM_UNSUPPORTED_TRADING_MODE",
            ("trading_mode", s),
            "Unsupported trading mode",
            f"Configured trading mode {settings.trading_mode!r} is not in the allowed set "
            "{disabled, paper}. No action is taken automatically.",
            {"trading_mode": str(settings.trading_mode)},
        ),
    )
    candidates += [a] if a else []

    # database + market data
    err = ""
    try:
        instrument_id, symbol, bars, fresh = load_market()
        db_ok = True
    except Exception as exc:
        db_ok, err = False, type(exc).__name__
    a = condition_alert(
        store,
        "database_unavailable",
        not db_ok,
        now,
        lambda s: mk(
            "SYSTEM_DATABASE_UNAVAILABLE",
            ("database", s),
            "Database unavailable",
            f"PostgreSQL could not be reached ({err}). Market-data and dashboard checks skipped.",
            {"error_type": err},
        ),
    )
    candidates += [a] if a else []
    health["database"] = component(OK if db_ok else FAILED, "reachable" if db_ok else err)

    if db_ok:
        last_session = pd.Timestamp(bars.index[-1]).tz_convert("America/New_York").date()
        store.set_since(since or last_session)
        start = store.since or last_session
        since_ts = pd.Timestamp(start, tz="America/New_York").tz_convert("UTC")
        candidates += market_alerts(bars, instrument=symbol, since=since_ts)
        stale = rules.stale_data_alert(
            instrument=symbol,
            last_bar_session=fresh.last_bar_session,
            expected_session=fresh.expected_session,
            missing_sessions=fresh.missing_sessions,
            detected_at=now.isoformat(),
            app_version=av,
        )
        candidates += [stale] if stale else []
        health["market_data"] = component(
            DEGRADED if fresh.stale else OK,
            f"last session {fresh.last_bar_session}; {fresh.missing_sessions} missing",
            last_session=fresh.last_bar_session.isoformat(),
            bars=len(bars),
        )
        runs, qevents, last_run = (load_db_events or db_events)(instrument_id, start)
        candidates += rules.ingestion_alerts(runs, instrument=symbol, app_version=av)
        candidates += rules.quality_event_alerts(qevents, instrument=symbol, app_version=av)
        health["ingestion"] = component(
            OK if (last_run and last_run[0]["status"] == "succeeded") else DEGRADED,
            "no ingestion run"
            if not last_run
            else f"run {last_run[0]['run_id']} {last_run[0]['status']} at "
            f"{last_run[0]['finished_at']}",
        )
        if dashboard_results_missing is not None:
            missing = list(dashboard_results_missing())
            a = condition_alert(
                store,
                f"dashboard_stale:{','.join(missing)}",
                bool(missing),
                now,
                lambda s: mk(
                    "SYSTEM_DASHBOARD_RESULTS_STALE",
                    ("dashboard", ",".join(missing), s),
                    "Dashboard research results not computed for the current dataset",
                    "Persisted dashboard results are missing for the current dataset: "
                    f"{', '.join(missing)}. They are recomputed only by an explicit action.",
                    {"missing": missing},
                ),
            )
            candidates += [a] if a else []
            health["dashboard"] = component(
                DEGRADED if missing else OK,
                "missing: " + ", ".join(missing) if missing else "persisted results current",
            )
    if store.since is None:  # database down on the very first run
        store.set_since(since or now.date())
    since_ts_paper = (
        pd.Timestamp(store.since, tz="America/New_York").tz_convert("UTC") if store.since else None
    )

    # paper accounts (read-only, verified)
    accounts = []
    for path in paper_account_dirs(paper_root):
        acc = read_paper_account(path)
        broken = acc.error_kind is not None
        a = condition_alert(
            store,
            f"paper:{acc.account_id}",
            broken,
            now,
            lambda s, acc=acc: mk(
                acc.error_kind,
                (acc.account_id, s),
                f"PAPER SIMULATION account {acc.account_id}: "
                f"{str(acc.error_kind).lower().replace('_', ' ')}",
                f"PAPER SIMULATION. Account {acc.account_id} cannot be read safely: {acc.error}. "
                "Nothing was repaired; no trade is triggered.",
                {"error": acc.error},
                account_id=acc.account_id,
            ),
        )
        candidates += [a] if a else []
        if not broken:
            candidates += rules.paper_alerts(
                acc.events,
                account_id=acc.account_id,
                strategy_id=acc.strategy_id,
                instrument=acc.instrument,
                initial_capital=acc.initial_capital,
                since=since_ts_paper,
                app_version=av,
            )
        accounts.append(
            {
                "account_id": acc.account_id,
                "status": FAILED if broken else OK,
                "detail": acc.error or f"{len(acc.events)} committed events verified",
            }
        )
    health["paper_accounts"] = component(
        FAILED if any(x["status"] == FAILED for x in accounts) else OK,
        f"{len(accounts)} account(s)",
        accounts=accounts,
    )

    new = store.add(candidates, recorded_at=now.isoformat())
    report = deliver_pending(store, channels or configured_channels(), now) if deliver else None
    health["alerts"] = component(OK, f"{len(store.alerts())} stored", **store.metrics())
    health["application"] = component(OK, f"version {__version__}")
    health["overall"] = summarize({k: v for k, v in health.items() if "status" in v})
    return RunResult(now.isoformat(), store.state["since"], len(candidates), new, report, health)


def verify_store(root: Path = DEFAULT_ALERTS_ROOT) -> dict[str, Any]:
    try:
        st = AlertStore(root, create=False)
    except AlertStoreError as exc:
        return {"status": FAILED, "error": str(exc)}
    return {"status": OK, "alerts": len(st.alerts()), "deliveries": len(st.delivery_log())}
