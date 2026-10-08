"""Real `Services` adapter: each stage delegates to an existing subsystem.

    INGEST   -> app.data.ingestion.ingest_daily_bars (provider = YFinanceProvider by default; the
                only network access, and only through the existing provider)
    VALIDATE -> stored closed bar of the session: present, unique, positive, consistent OHLC,
                previous calendar session stored
    PAPER    -> every account under the paper root: Phase 12 PaperStore.process_many on inputs
                built from bars <= the session (point-in-time), via the dashboard paper service
    ALERTS   -> app.alerts.engine.run_alerts (market freshness evaluated as of the session)
    HEALTH   -> database, schema, freshness, ingestion, paper ledgers, alert store, safety,
                version, pipeline checkpoint
The adapter never calls a broker, an order API or anything that could move money.
"""

import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app import __version__
from app.alerts.channels import Channel, configured_channels, deliver_pending
from app.alerts.engine import run_alerts, verify_store
from app.alerts.models import AlertEvent
from app.alerts.monitor import (
    ingestion_runs_since,
    paper_account_dirs,
    quality_events_since,
    read_paper_account,
    trading_mode_ok,
)
from app.alerts.store import AlertStore
from app.backtest.data import load_backtest_bars
from app.core.config import Settings
from app.dashboard.services import health as dash_health
from app.dashboard.services import market
from app.dashboard.services import paper as paper_svc
from app.data.calendar import TradingCalendar
from app.data.ingestion import ingest_daily_bars
from app.data.instruments import get_spec
from app.data.providers.base import DataProvider
from app.data.providers.yfinance_provider import YFinanceProvider
from app.database.models import Instrument, PriceBar
from app.operations import sessions as cal_
from app.operations.config import OperationsConfig
from app.operations.store import OpsStore
from app.paper.engine import baseline_inputs
from app.paper.ledger import git_commit


class SystemServices:
    def __init__(
        self,
        config: OperationsConfig,
        *,
        engine: Engine,
        settings: Settings,
        paper_root: Path,
        alerts_root: Path,
        ops_root: Path,
        provider: DataProvider | None = None,
        calendar: TradingCalendar | None = None,
        channels: list[Channel] | None = None,
    ) -> None:
        self.cfg = config
        self.engine = engine
        self.sf = sessionmaker(bind=engine, expire_on_commit=False)
        self.settings = settings
        self.paper_root, self.alerts_root, self.ops_root = paper_root, alerts_root, ops_root
        self.provider = provider or YFinanceProvider()
        self.cal = calendar or TradingCalendar(config.exchange)
        self.channels = channels
        self.spec = get_spec(config.symbol)

    # ── precheck ──

    def precheck(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        out["trading_mode"] = {
            "ok": trading_mode_ok(self.settings),
            "critical": True,
            "detail": str(
                self.settings.trading_mode.value
                if hasattr(self.settings.trading_mode, "value")
                else self.settings.trading_mode
            ),
        }
        db = dash_health.database_status(self.engine)
        out["database"] = {
            "ok": db["status"] == "ok",
            "critical": True,
            "detail": db["error"] or "reachable",
        }
        if db["status"] == "ok":
            with self.sf() as s:
                sch = dash_health.schema_status(s)
                inst = s.scalar(select(Instrument.id).where(Instrument.symbol == self.cfg.symbol))
            out["schema"] = {
                "ok": bool(sch["at_head"]),
                "critical": True,
                "detail": f"{sch['current']} (head {sch['head']})",
            }
            out["instrument"] = {
                "ok": self.spec.calendar == self.cfg.exchange,
                "critical": True,
                "detail": f"{self.spec.symbol} on {self.spec.exchange}, calendar "
                f"{self.spec.calendar}; "
                + ("registered" if inst else "not yet registered (first ingest)"),
            }
        try:
            today = datetime.now().date()
            ok = not self.cal.sessions(today - timedelta(days=10), today).empty
            ok = ok and self.cal.last_session >= today
            out["calendar"] = {
                "ok": ok,
                "critical": True,
                "detail": f"{self.cal.exchange} sessions until {self.cal.last_session}",
            }
        except Exception as exc:
            out["calendar"] = {"ok": False, "critical": True, "detail": type(exc).__name__}
        for name, root in (
            ("paper_storage", self.paper_root),
            ("alert_storage", self.alerts_root),
            ("ops_storage", self.ops_root),
        ):
            target = root if root.exists() else root.parent
            while not target.exists() and target != target.parent:
                target = target.parent
            ok = os.access(target, os.R_OK | os.W_OK)
            out[name] = {
                "ok": ok,
                "critical": True,
                "detail": f"{'readable/writable' if ok else 'not accessible'}",
            }
        bad = [
            a.account_id
            for a in map(read_paper_account, paper_account_dirs(self.paper_root))
            if a.error_kind
        ]
        out["paper_accounts"] = {
            "ok": not bad,
            "critical": False,
            "detail": "all readable" if not bad else f"unreadable: {bad}",
        }
        return out

    # ── data ──

    def _instrument_id(self, s: Session) -> int | None:
        return s.scalar(select(Instrument.id).where(Instrument.symbol == self.cfg.symbol))

    def latest_stored_session(self) -> date | None:
        with self.sf() as s:
            iid = self._instrument_id(s)
            if iid is None:
                return None
            ts = s.scalar(
                select(func.max(PriceBar.ts)).where(
                    PriceBar.instrument_id == iid,
                    PriceBar.provider == self.provider.name,
                    PriceBar.timeframe == "1d",
                    PriceBar.is_closed.is_(True),
                )
            )
        return None if ts is None else pd.Timestamp(ts).tz_convert(self.cal.tz).date()

    def _bars_of(self, s: Session, day: date) -> list[PriceBar]:
        iid = self._instrument_id(s)
        if iid is None:
            return []
        open_utc, _ = cal_.session_times(self.cal, day)
        return list(
            s.scalars(
                select(PriceBar).where(
                    PriceBar.instrument_id == iid,
                    PriceBar.provider == self.provider.name,
                    PriceBar.timeframe == "1d",
                    PriceBar.ts == open_utc.to_pydatetime(),
                )
            ).all()
        )

    def bar_exists(self, session: date) -> bool:
        with self.sf() as s:
            return any(b.is_closed for b in self._bars_of(s, session))

    def ingest(self, session: date) -> dict[str, Any]:
        with self.sf() as s:
            res = ingest_daily_bars(
                s, self.provider, self.spec, session, session, calendar=self.cal
            )
        summary = res.summary()
        if res.status != "succeeded":
            raise RuntimeError(f"ingestion run {res.run_id} {res.status}: {res.error}")
        return summary

    def validate(self, session: date) -> dict[str, Any]:
        with self.sf() as s:
            bars = [b for b in self._bars_of(s, session) if b.is_closed]
            prev = cal_.previous_session(self.cal, session)
            has_earlier = s.scalar(
                select(func.count(PriceBar.id)).where(
                    PriceBar.provider == self.provider.name,
                    PriceBar.timeframe == "1d",
                    PriceBar.ts < cal_.session_times(self.cal, session)[0].to_pydatetime(),
                )
            )
            prev_ok = (
                prev is None or not has_earlier or any(b.is_closed for b in self._bars_of(s, prev))
            )
        if not bars:
            return {"ok": False, "missing": True, "detail": f"no closed bar stored for {session}"}
        if len(bars) > 1:
            return {"ok": False, "missing": False, "detail": f"{len(bars)} bars for {session}"}
        b = bars[0]
        o, h, lo, c = (float(b.open), float(b.high), float(b.low), float(b.close))
        problems = []
        if min(o, h, lo, c) <= 0:
            problems.append("non-positive price")
        if h < max(o, c, lo) or lo > min(o, c, h):
            problems.append("inconsistent OHLC")
        if float(b.volume) < 0:
            problems.append("negative volume")
        if not prev_ok:
            problems.append(f"previous session {prev} not stored")
        bar = {
            "ts": pd.Timestamp(b.ts).isoformat(),
            "open": o,
            "high": h,
            "low": lo,
            "close": c,
            "volume": float(b.volume),
        }
        if problems:
            return {"ok": False, "missing": False, "detail": "; ".join(problems), "bar": bar}
        return {"ok": True, "missing": False, "detail": "bar present and consistent", "bar": bar}

    def _bars_upto(self, session: date) -> pd.DataFrame:
        with self.sf() as s:
            _, bars = load_backtest_bars(s, self.cfg.symbol, provider=self.provider.name)
        open_utc, _ = cal_.session_times(self.cal, session)
        return bars[bars.index <= open_utc]

    # ── paper ──

    def paper(self, session: date) -> dict[str, Any]:
        refs = paper_svc.list_accounts(self.paper_root)
        if not refs:
            return {"accounts": {}, "failed": [], "detail": "no paper accounts configured"}
        inputs = baseline_inputs(self._bars_upto(session))  # point-in-time: bars <= session
        accounts: dict[str, dict[str, Any]] = {}
        failed: list[str] = []
        for ref in refs:
            try:
                if ref.strategy_id not in inputs:
                    raise paper_svc.AccountError(f"unknown strategy {ref.strategy_id!r}")
                version, bar_inputs = inputs[ref.strategy_id]
                if version != ref.strategy_version:
                    raise paper_svc.AccountError(
                        f"strategy version {version} differs from the account's "
                        f"{ref.strategy_version}"
                    )
                store = paper_svc.open_account(ref, self.paper_root)
                counts = paper_svc.paper_process_sessions(store, bar_inputs)
                last = store.trader.state.last_session
                accounts[ref.account_id] = {
                    "status": "ok",
                    **counts,
                    "last_session": None if last is None else last.isoformat(),
                }
            except Exception as exc:
                accounts[ref.account_id] = {
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
                failed.append(ref.account_id)
        return {"accounts": accounts, "failed": failed}

    def paper_accounts(self) -> list[dict[str, Any]]:
        out = []
        for path in paper_account_dirs(self.paper_root):
            acc = read_paper_account(path)
            snaps = [e for e in acc.events if e["event_type"] == "snapshot"]
            out.append(
                {
                    "account_id": acc.account_id,
                    "strategy_id": acc.strategy_id,
                    "status": acc.error_kind or "ok",
                    "last_session": snaps[-1]["session"] if snaps else None,
                }
            )
        return out

    # ── alerts ──

    def _db_events(self, instrument_id: int, start: date) -> Any:
        with self.sf() as s:
            return (
                ingestion_runs_since(s, instrument_id, start),
                quality_events_since(s, start),
                ingestion_runs_since(s, instrument_id, date.min)[-1:],
            )

    def _market(self, as_of: datetime) -> Any:
        def load() -> tuple[int, str, pd.DataFrame, market.Freshness]:
            with self.sf() as s:
                ident = market.dataset_identity(s, self.cfg.symbol, provider=self.provider.name)
                bars = market.load_bars(s, ident)
            return (
                ident.instrument_id,
                self.cfg.symbol,
                bars,
                market.freshness(ident.last_ts, as_of),
            )

        return load

    def alerts(self, session: date, now: datetime) -> dict[str, Any]:
        as_of = min(
            now, cal_.eligible_at(self.cal, session, self.cfg.grace_minutes).to_pydatetime()
        )
        res = run_alerts(
            now=now,
            alerts_root=self.alerts_root,
            paper_root=self.paper_root,
            settings=self.settings,
            load_market=self._market(as_of),
            load_db_events=self._db_events,
            channels=self.channels,
        )
        d = res.delivery
        return {
            "new_alerts": len(res.new_alerts),
            "candidates": res.candidates,
            "new_by_type": {
                t: sum(1 for a in res.new_alerts if a.event_type == t)
                for t in sorted({a.event_type for a in res.new_alerts})
            },
            "delivered": None if d is None else d.delivered,
            "delivery_failed": None if d is None else d.failed,
            "freshness_as_of": as_of.isoformat(),
        }

    def emit_ops_alerts(self, alerts: list[AlertEvent], now: datetime) -> None:
        store = AlertStore(self.alerts_root)
        store.add(alerts, recorded_at=now.isoformat())
        deliver_pending(store, self.channels or configured_channels(), now)

    # ── health ──

    def health(self, now: datetime) -> dict[str, Any]:
        comp: dict[str, dict[str, Any]] = {}
        db = dash_health.database_status(self.engine)
        comp["database"] = {
            "status": "ok" if db["status"] == "ok" else "critical",
            "detail": db["error"] or "reachable",
        }
        if db["status"] == "ok":
            with self.sf() as s:
                sch = dash_health.schema_status(s)
                comp["schema"] = {
                    "status": "ok" if sch["at_head"] else "critical",
                    "detail": f"{sch['current']} (head {sch['head']})",
                }
                try:
                    ident = market.dataset_identity(s, self.cfg.symbol, provider=self.provider.name)
                    fresh = market.freshness(ident.last_ts, now)
                    comp["market_data"] = {
                        "status": "warning" if fresh.stale else "ok",
                        "detail": f"latest bar {fresh.last_bar_session}; latest completed "
                        f"session {fresh.expected_session}; {fresh.missing_sessions} missing",
                    }
                    last_run = market.latest_ingestion(s, ident.instrument_id)
                    comp["ingestion"] = {
                        "status": "ok"
                        if last_run and last_run["status"] == "succeeded"
                        else "warning",
                        "detail": "none"
                        if not last_run
                        else f"run {last_run['run_id']} {last_run['status']} at "
                        f"{last_run['finished_at']}",
                    }
                except market.NoDataError as exc:
                    comp["market_data"] = {"status": "critical", "detail": str(exc)}
        accts = [read_paper_account(p) for p in paper_account_dirs(self.paper_root)]
        bad = [a for a in accts if a.error_kind]
        comp["paper_accounts"] = {
            "status": "critical" if bad else "ok",
            "detail": f"{len(accts)} account(s); ledgers "
            + (
                "verified" if not bad else "; ".join(f"{a.account_id}: {a.error_kind}" for a in bad)
            ),
        }
        v = verify_store(self.alerts_root)
        comp["alert_store"] = {
            "status": "ok" if v["status"] == "ok" else "warning",
            "detail": v.get("error") or f"{v['alerts']} alerts verified",
        }
        comp["safety"] = {
            "status": "ok" if trading_mode_ok(self.settings) else "critical",
            "detail": f"trading mode {self.settings.trading_mode}",
        }
        comp["application"] = {"status": "ok", "detail": f"version {__version__}"}
        ops = OpsStore.read_only(self.ops_root)
        comp["pipeline"] = {
            "status": "ok",
            "detail": "checkpoint " + str(ops.last_completed_session if ops else None),
        }
        states = {c["status"] for c in comp.values()}
        overall = "critical" if "critical" in states else "warning" if "warning" in states else "ok"
        return {"overall": overall, "components": comp}

    def git_commit(self) -> str:
        return git_commit()

    def database_ping(self) -> bool:
        try:
            with self.engine.connect() as c:
                c.execute(text("SELECT 1"))
            return True
        except Exception:
            return False
