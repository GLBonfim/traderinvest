"""System / data health. Never exposes secrets: connection errors are reported by exception
type only, and no setting value other than the trading mode is shown."""

from typing import Any

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session

from app import __version__
from app.core.config import Settings, TradingMode
from app.core.safety import SAFE_TRADING_MODES
from app.dashboard.config import REPO_ROOT
from app.database.models import DataQualityEvent
from app.paper.ledger import git_commit


def database_status(engine: Engine) -> dict[str, Any]:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok", "error": None}
    except Exception as exc:
        return {"status": "unavailable", "error": type(exc).__name__}


def schema_status(session: Session) -> dict[str, Any]:
    current = session.execute(text("SELECT version_num FROM alembic_version")).scalar()
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    head = ScriptDirectory.from_config(cfg).get_current_head()
    return {"current": current, "head": head, "at_head": current == head}


def quality_events(session: Session) -> dict[str, int]:
    rows = session.execute(
        select(DataQualityEvent.severity, func.count(DataQualityEvent.id)).group_by(
            DataQualityEvent.severity
        )
    ).all()
    return {str(sev): int(n) for sev, n in rows}


def safety_status(settings: Settings) -> dict[str, Any]:
    return {
        "trading_mode": settings.trading_mode.value,
        "allowed_modes": sorted(m.value for m in TradingMode),
        "mode_is_safe": settings.trading_mode in SAFE_TRADING_MODES,
        "real_money_execution": "blocked in code (refuse_real_money_order always raises)",
        "broker_integrations": "none",
    }


def app_status() -> dict[str, str]:
    return {"app_version": __version__, "git_commit": git_commit()}
