"""Shared UI context: cached immutable research data + the sidebar selections.

Caching rules
- Every cached function takes `key` = dataset identity (app version, bar count, first/last bar,
  last ingestion) + `CONFIG_KEY` (fingerprints of all engine/backtest/validation/ML/risk
  configurations). A new ingestion, a code version or a configuration change => a new key.
- Only immutable, deterministic outputs are cached. Paper accounts are NEVER cached: they are
  re-read (and their ledger re-verified) on every rerun.
"""

import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime

import pandas as pd
import streamlit as st

from app.backtest.config import BacktestConfig
from app.backtest.models import BacktestResult
from app.dashboard.config import DISCLAIMERS, SYMBOL, SYSTEM_LABEL
from app.dashboard.services import market, research
from app.dashboard.services.analysis import STRATEGIES, EngineBundle, compute_engines
from app.database.session import get_session_factory
from app.ml.config import MLConfig
from app.paper.engine import baseline_inputs
from app.paper.trader import BarInput
from app.risk.config import SCENARIOS
from app.risk.engine import RiskRun
from app.strategies.engine import StrategyEngine
from app.validation.config import ValidationConfig

CONFIG_KEY = hashlib.sha256(
    "|".join(
        [
            StrategyEngine().fingerprint(),
            BacktestConfig().fingerprint(),
            ValidationConfig().fingerprint(),
            MLConfig().fingerprint(),
            *(s.fingerprint() for s in SCENARIOS),
        ]
    ).encode()
).hexdigest()[:16]


@st.cache_data(show_spinner="Loading stored bars…", max_entries=2)
def cached_bars(key: str, symbol: str) -> pd.DataFrame:
    with get_session_factory()() as session:
        ident = market.dataset_identity(session, symbol)
        if f"{ident.key}-{CONFIG_KEY}" != key:
            raise market.NoDataError("dataset changed while loading; reload the page")
        return market.load_bars(session, ident)


@st.cache_resource(show_spinner="Computing engine outputs (once per dataset)…", max_entries=2)
def cached_engines(key: str, symbol: str) -> EngineBundle:
    return compute_engines(cached_bars(key, symbol))


@st.cache_resource(show_spinner="Running the Phase 8 backtests…", max_entries=2)
def cached_backtest(key: str, symbol: str) -> dict[str, object]:
    return research.backtest_suite(cached_bars(key, symbol))


@st.cache_resource(show_spinner="Running the risk overlay…", max_entries=24)
def cached_risk_run(key: str, symbol: str, strategy_id: str, scenario: str) -> RiskRun:
    return research.risk_run(
        cached_bars(key, symbol), cached_engines(key, symbol), strategy_id, scenario
    )


@st.cache_data(show_spinner="Running all risk scenarios for this strategy…", max_entries=12)
def cached_risk_table(key: str, symbol: str, strategy_id: str) -> pd.DataFrame:
    return research.risk_scenario_table(
        cached_bars(key, symbol), cached_engines(key, symbol), strategy_id
    )


@st.cache_resource(show_spinner="Preparing paper inputs…", max_entries=2)
def cached_paper_inputs(key: str, symbol: str) -> dict[str, tuple[str, list[BarInput]]]:
    return baseline_inputs(cached_bars(key, symbol))


@dataclass(frozen=True)
class Context:
    ident: market.DatasetIdentity
    key: str
    bars: pd.DataFrame
    session: date
    ts: pd.Timestamp
    strategy_id: str
    scenario: str

    def engines(self) -> EngineBundle:
        return cached_engines(self.key, self.ident.symbol)

    def backtest(self) -> dict[str, BacktestResult]:
        return cached_backtest(self.key, self.ident.symbol)["results"]  # type: ignore[return-value]

    def backtest_table(self) -> pd.DataFrame:
        return cached_backtest(self.key, self.ident.symbol)["table"]  # type: ignore[return-value]

    def risk_run(self, strategy_id: str | None = None, scenario: str | None = None) -> RiskRun:
        return cached_risk_run(
            self.key, self.ident.symbol, strategy_id or self.strategy_id, scenario or self.scenario
        )


def build_context() -> Context:
    """Sidebar + dataset loading. Stops the page with a clear message if data is unavailable."""
    st.sidebar.markdown(f"**{SYSTEM_LABEL}** · {SYMBOL} daily")
    st.sidebar.caption(DISCLAIMERS["system"])
    try:
        with get_session_factory()() as session:
            ident = market.dataset_identity(session, SYMBOL)
    except market.NoDataError as exc:
        st.error(f"No data: {exc}. Ingest bars with `python -m app.data.cli ingest`.")
        st.stop()
    except Exception as exc:
        st.error(
            f"PostgreSQL is unavailable ({type(exc).__name__}). Start it with "
            "`docker compose up -d db`. No numbers are shown without the database."
        )
        st.stop()
    key = f"{ident.key}-{CONFIG_KEY}"
    bars = cached_bars(key, ident.symbol)
    sessions = market.session_dates(bars)
    chosen = st.sidebar.date_input(
        "Session (as of the close)",
        value=sessions[-1],
        min_value=sessions[0],
        max_value=sessions[-1],
        key="session_date",
    )
    if not isinstance(chosen, date):
        st.stop()
    selected = market.session_on_or_before(sessions, chosen)
    if selected != chosen:
        st.sidebar.caption(f"{chosen} is not a stored session; showing {selected}.")
    ts = market.bar_ts_for(bars, selected)
    strategy = st.sidebar.selectbox("Research strategy", STRATEGIES, index=1, key="strategy")
    scenario = st.sidebar.selectbox("Risk scenario", [s.name for s in SCENARIOS], key="scenario")
    fresh = market.freshness(ident.last_ts, datetime.now(UTC))
    if fresh.stale:
        st.sidebar.warning(
            f"Stale data: last stored session {fresh.last_bar_session}; "
            f"{fresh.missing_sessions} completed session(s) not ingested."
        )
    st.sidebar.caption(f"Dataset key `{ident.key}` · config `{CONFIG_KEY}`")
    return Context(ident, key, bars, selected, ts, str(strategy), str(scenario))
