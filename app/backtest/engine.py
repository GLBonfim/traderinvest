"""Backtester: strategy states + bars + explicit assumptions -> hypothetical trades and metrics.

Research tool only: nothing is ever sent to a broker. Results describe a historical simulation
under stated assumptions and are not evidence of future profitability.
"""

from dataclasses import asdict
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd

from app.backtest.config import ENGINE_VERSION, BacktestConfig
from app.backtest.execution import ExecutionModel, NextSessionOpen
from app.backtest.metrics import all_metrics, drawdown, returns_from_equity
from app.backtest.models import TRADE_COLUMNS, BacktestResult
from app.backtest.portfolio import costs_frame, simulate
from app.data.calendar import TradingCalendar
from app.strategies.base import LONG
from app.strategies.engine import StrategyEngine, StrategyRun

BENCHMARK_PRICE = "benchmark_buy_and_hold_price"
BENCHMARK_TOTAL = "benchmark_buy_and_hold_total"


def session_times(index: pd.DatetimeIndex, calendar: TradingCalendar) -> pd.DataFrame:
    """observed_at = session close of each bar; effective_at = next session open (calendar)."""
    sessions = calendar.sessions(index[0].date(), index[-1].date() + timedelta(days=15))
    opens = pd.DatetimeIndex(sessions["open_utc"])
    pos = opens.get_indexer(index)
    if (pos < 0).any():
        raise ValueError("bar ts is not an exchange session open")
    return pd.DataFrame(
        {"observed_at": sessions["close_utc"].to_numpy()[pos], "effective_at": opens[pos + 1]},
        index=index,
    )


class Backtester:
    def __init__(
        self, config: BacktestConfig | None = None, model: ExecutionModel | None = None
    ) -> None:
        self.config = config or BacktestConfig()
        self.model = model or NextSessionOpen()
        self.calendar = TradingCalendar("XNYS")

    # ── windows ──

    def _window(self, bars: pd.DataFrame) -> tuple[pd.DataFrame, int]:
        """Bars up to `end` (nothing later is ever used) and the index where the window starts."""
        cfg = self.config
        dates = pd.DatetimeIndex(bars.index).date
        upto = bars[dates <= cfg.end] if cfg.end else bars
        if upto.empty:
            raise ValueError("no bars on or before end")
        start_idx = 0
        if cfg.start:
            later = np.flatnonzero(pd.DatetimeIndex(upto.index).date >= cfg.start)
            if not later.size:
                raise ValueError("no bars on or after start")
            start_idx = int(later[0])
        return upto, start_idx

    # ── one state series ──

    def run_states(
        self,
        bars: pd.DataFrame,
        states: pd.Series,
        *,
        strategy_id: str,
        observed_at: pd.Series | None = None,
        effective_at: pd.Series | None = None,
    ) -> BacktestResult:
        upto, start_idx = self._window(bars)
        states = states.reindex(upto.index)
        if states.isna().any():
            raise ValueError("states missing for some bars")
        if observed_at is None or effective_at is None:
            times = session_times(pd.DatetimeIndex(upto.index), self.calendar)
            observed_at, effective_at = times["observed_at"], times["effective_at"]
        else:
            observed_at, effective_at = (
                observed_at.reindex(upto.index),
                effective_at.reindex(upto.index),
            )
        common = {
            "strategy_id": strategy_id,
            "index": pd.DatetimeIndex(upto.index),
            "opens": upto["open"].to_numpy(dtype=np.float64),
            "closes": upto["close"].to_numpy(dtype=np.float64),
            "states": states.tolist(),
            "observed_at": list(observed_at),
            "effective_at": list(effective_at),
            "window_start": start_idx,
            "model": self.model,
        }
        net = simulate(cfg=self.config, **common)  # type: ignore[arg-type]
        gross = simulate(cfg=self.config.without_costs(), **common)  # type: ignore[arg-type]
        return self._result(strategy_id, net, gross.equity["equity"])

    def _result(self, strategy_id: str, net: Any, gross_equity: pd.Series) -> BacktestResult:
        cfg = self.config
        eq = net.equity.copy()
        initial = cfg.initial_capital
        eq["daily_return"] = returns_from_equity(eq["equity"], initial)
        eq["cumulative_return"] = eq["equity"] / initial - 1
        eq = eq.join(drawdown(eq["equity"], initial))
        eq["gross_equity"] = gross_equity
        eq["gross_daily_return"] = returns_from_equity(gross_equity, initial)
        eq["gross_cumulative_return"] = gross_equity / initial - 1
        # Fixed schema: the table looks the same whether or not any trade exists.
        trades = pd.DataFrame([t.as_record() for t in net.trades], columns=list(TRADE_COLUMNS))
        metrics = all_metrics(
            eq, gross_equity, trades, initial, cfg.periods_per_year, cfg.risk_free_rate
        )
        open_pos = net.open_position.as_record() if net.open_position else None
        metrics["realized_pnl"] = float(trades["net_pnl"].sum()) if len(trades) else 0.0
        metrics["unrealized_pnl"] = (
            float(eq["equity"].iloc[-1]) - initial - metrics["realized_pnl"] if len(eq) else 0.0
        )
        metrics["total_pnl"] = float(eq["equity"].iloc[-1]) - initial if len(eq) else 0.0
        return BacktestResult(
            strategy_id=strategy_id,
            equity=eq,
            fills=costs_frame(net.fills),
            trades=trades,
            open_position=open_pos,
            pending_state=net.pending,
            metrics=metrics,
            config_fingerprint=cfg.fingerprint(),
            meta={
                "engine_version": ENGINE_VERSION,
                "execution": self.model.name,
                "window_start": eq.index[0] if len(eq) else None,
                "window_end": eq.index[-1] if len(eq) else None,
                "period_label": cfg.period_label,
                "config": asdict(cfg),
            },
        )

    # ── strategies ──

    def run(self, bars: pd.DataFrame, strategy_run: StrategyRun) -> dict[str, BacktestResult]:
        results = {}
        for sid, g in strategy_run.signals.groupby("strategy_id", sort=False):
            g = g.set_index("bar_ts")
            results[str(sid)] = self.run_states(
                bars, g["state"], strategy_id=str(sid),
                observed_at=g["observed_at"], effective_at=g["effective_at"],
            )  # fmt: skip
        return results

    # ── benchmarks ──

    def benchmarks(self, bars: pd.DataFrame) -> dict[str, BacktestResult]:
        """Buy & hold under the same execution convention and costs (one entry, no exit).

        price  : valued with raw prices (price return only)
        total  : valued with adjusted prices (open * adj_close / close, adj_close) — dividends
                 reinvested as reflected by the provider's adjustment; unavailable without adj_close
        """
        always_long = pd.Series(LONG, index=bars.index)
        out = {BENCHMARK_PRICE: self.run_states(bars, always_long, strategy_id=BENCHMARK_PRICE)}
        if "adj_close" in bars.columns and bars["adj_close"].notna().all():
            factor = bars["adj_close"] / bars["close"]
            adjusted = bars.assign(open=bars["open"] * factor, close=bars["adj_close"])
            out[BENCHMARK_TOTAL] = self.run_states(
                adjusted, always_long, strategy_id=BENCHMARK_TOTAL
            )
        return out


def run_suite(
    bars: pd.DataFrame,
    *,
    config: BacktestConfig | None = None,
    strategy_engine: StrategyEngine | None = None,
    instrument_id: int | None = None,
) -> dict[str, BacktestResult]:
    """Strategies are computed on bars up to `end` only, then backtested over [start, end]."""
    bt = Backtester(config)
    end: date | None = bt.config.end
    upto = bars[pd.DatetimeIndex(bars.index).date <= end] if end else bars
    features_bars = upto.drop(columns=[c for c in ("adj_close",) if c in upto.columns])
    run = (strategy_engine or StrategyEngine()).run(features_bars, instrument_id=instrument_id)
    return {**bt.run(upto, run), **bt.benchmarks(upto)}


def summary_table(results: dict[str, BacktestResult]) -> pd.DataFrame:
    return pd.DataFrame({sid: r.metrics for sid, r in results.items()}).T
