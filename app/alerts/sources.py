"""Read-only inputs for an alert run, from the local database and local files.

Reuses the dashboard's read-only market helpers (dataset identity, bars, XNYS freshness) and
persisted-result locations, so alerts and dashboard agree on what "stale" means.
"""

from datetime import UTC, datetime
from typing import Any

import pandas as pd

from app.dashboard.config import RESEARCH_ROOT, SYMBOL
from app.dashboard.services import market, research
from app.database.session import get_session_factory


def load_market(
    symbol: str = SYMBOL, now: datetime | None = None
) -> tuple[int, str, pd.DataFrame, market.Freshness]:
    with get_session_factory()() as session:
        ident = market.dataset_identity(session, symbol)
        bars = market.load_bars(session, ident)
    fresh = market.freshness(ident.last_ts, now or datetime.now(UTC))
    return ident.instrument_id, symbol, bars, fresh


def dashboard_results_missing(symbol: str = SYMBOL) -> list[str]:
    """Names of persisted dashboard research results missing for the current dataset."""
    with get_session_factory()() as session:
        ident = market.dataset_identity(session, symbol)
        bars = market.load_bars(session, ident)
    missing: list[Any] = []
    try:
        research.load_validation(RESEARCH_ROOT, ident.key)
    except research.ResultsMissingError:
        missing.append("statistical_validation")
    try:
        research.load_ml(RESEARCH_ROOT, research.ml_experiment_id(bars))
    except research.ResultsMissingError:
        missing.append("machine_learning")
    return missing
