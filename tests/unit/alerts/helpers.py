"""Synthetic inputs for alert tests (real engines on seeded data; no database)."""

from datetime import UTC, date, datetime

import pandas as pd

from app.backtest.config import BacktestConfig
from app.backtest.engine import session_times
from app.dashboard.services import market
from app.data.calendar import TradingCalendar
from app.paper.config import PaperConfig
from app.paper.engine import PaperRun, PaperTradingEngine, build_inputs
from app.risk.config import RiskConfig
from tests.unit.risk.test_overlay import flat_indicators, make_bars
from tests.unit.strategies.helpers import session_walk

CAL = TradingCalendar("XNYS")
FREE = BacktestConfig(
    initial_capital=10_000.0, commission_per_trade=0.0, spread_bps=0.0, slippage_bps=0.0
)


def walk(n: int = 420, seed: int = 7) -> pd.DataFrame:
    b = session_walk(n, seed=seed, start=date(2018, 1, 2))
    return b.assign(adj_close=b["close"])


def paper_run(
    closes: list[float], states: list[str], risk: RiskConfig, vol: float = 0.2, atr: float = 2.0
) -> PaperRun:
    """Paper replay on hand-made bars (open = previous close, high/low around the bar)."""
    rows = []
    prev = closes[0]
    for c in closes:
        rows.append((prev, max(prev, c) + 0.5, min(prev, c) - 0.5, c))
        prev = c
    bars = make_bars(rows)
    times = session_times(pd.DatetimeIndex(bars.index), CAL)
    inputs = build_inputs(
        bars, pd.Series(states, index=bars.index), flat_indicators(bars, vol, atr), times
    )
    return PaperTradingEngine(PaperConfig(risk=risk, backtest=FREE)).replay_inputs(
        inputs, strategy_id="x"
    )


def fake_market(bars: pd.DataFrame, now: datetime | None = None):  # type: ignore[no-untyped-def]
    def load() -> tuple[int, str, pd.DataFrame, market.Freshness]:
        fresh = market.freshness(bars.index[-1], now or datetime.now(UTC))
        return 1, "SPY", bars, fresh

    return load


def no_db_events(instrument_id: int, start: date):  # type: ignore[no-untyped-def]
    return [], [], [{"run_id": 1, "status": "succeeded", "finished_at": "2024-01-01"}]
