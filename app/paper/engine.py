"""Historical replay: the PaperTrader state machine run over a whole history in one pass.

Replay and incremental paper trading (`app.paper.store`) share `PaperTrader.process` and the
same `BarInput` construction, so processing the same sessions bar by bar (with restarts) yields
the same records, ledger events and account as one replay (tested). NOT live trading; no
real-money execution exists.
"""

import hashlib
from dataclasses import asdict, dataclass, field, fields
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from app.backtest.engine import session_times
from app.backtest.metrics import (
    all_metrics,
    defined_returns,
    drawdown,
    undefined_mask,
    without_undefined_return_metrics,
)
from app.data.calendar import TradingCalendar
from app.indicators.engine import IndicatorEngine
from app.paper.config import PAPER_VERSION, PaperConfig
from app.paper.ledger import step_events
from app.paper.models import AccountSnapshot, Decision, Fill, Order, Reconciliation
from app.paper.trader import (
    TRADE_COLUMNS,
    BarInput,
    PaperState,
    PaperTrader,
    StepResult,
    split_at_start,
)
from app.strategies.engine import StrategyEngine

OHLCV = ["open", "high", "low", "close", "volume"]


@dataclass
class PaperRun:
    account_id: str
    strategy_id: str
    strategy_version: str
    instrument: str
    timeframe: str
    risk_scenario: str
    config_fingerprint: str
    data_fingerprint: str  # report metadata only; never part of a record identity
    decisions: pd.DataFrame
    orders: pd.DataFrame  # executed/rejected orders + the final pending one (if any)
    fills: pd.DataFrame
    portfolio: pd.DataFrame  # account snapshots
    reconciliation: pd.DataFrame
    equity: pd.DataFrame  # Phase 8-compatible curve (for Phase 8 metric functions)
    trades: pd.DataFrame  # round trips flat -> flat
    pending_order: dict[str, Any] | None
    events: list[dict[str, Any]]
    final_state: PaperState
    metrics: dict[str, float] = field(default_factory=dict)


def data_fingerprint(bars: pd.DataFrame) -> str:
    raw = np.asarray(pd.util.hash_pandas_object(bars[OHLCV])).tobytes()
    return hashlib.sha256(raw).hexdigest()[:16]


def build_inputs(
    bars: pd.DataFrame, states: pd.Series, indicators: pd.DataFrame, times: pd.DataFrame
) -> list[BarInput]:
    """One BarInput per bar; each reads only its own row of every input."""
    idx = pd.DatetimeIndex(bars.index)
    if not idx.is_monotonic_increasing or not idx.is_unique:
        raise ValueError("bars must be in strictly increasing time order")
    st = states.reindex(idx)
    if st.isna().any():
        raise ValueError("strategy states missing for some bars")
    cols = [bars[c].to_numpy(np.float64) for c in ("open", "low", "close")]
    vol = indicators["realized_vol_20"].reindex(idx).to_numpy(np.float64)
    atr = indicators["atr_14"].reindex(idx).to_numpy(np.float64)
    obs = list(times["observed_at"].reindex(idx))
    eff = list(times["effective_at"].reindex(idx))
    return [
        BarInput(
            idx[i],
            float(cols[0][i]),
            float(cols[1][i]),
            float(cols[2][i]),
            str(st.iloc[i]),
            float(vol[i]),
            float(atr[i]),
            pd.Timestamp(obs[i]),
            pd.Timestamp(eff[i]),
        )
        for i in range(len(idx))
    ]


def baseline_inputs(
    bars: pd.DataFrame, strategy_engine: StrategyEngine | None = None
) -> dict[str, tuple[str, list[BarInput]]]:
    """strategy_id -> (strategy_version, inputs) for the six Phase 7 baselines. Every engine
    used here is causal, so the inputs of bar T are identical whether computed on bars <= T or
    on a longer history (tested)."""
    ohlcv = bars[OHLCV]
    run = (strategy_engine or StrategyEngine()).run(ohlcv)
    indicators = IndicatorEngine().analyze(ohlcv).values
    times = session_times(pd.DatetimeIndex(bars.index), TradingCalendar("XNYS"))
    out = {}
    for sid, g in run.signals.groupby("strategy_id", sort=False):
        st = pd.Series(g["state"].to_numpy(), index=pd.DatetimeIndex(g["bar_ts"]))
        version = str(g["strategy_version"].iloc[0])
        out[str(sid)] = (version, build_inputs(bars, st, indicators, times))
    return out


class PaperTradingEngine:
    def __init__(self, config: PaperConfig | None = None) -> None:
        self.config = config or PaperConfig()

    def replay(
        self,
        bars: pd.DataFrame,
        states: pd.Series,
        indicators: pd.DataFrame,
        times: pd.DataFrame,
        *,
        strategy_id: str,
        strategy_version: str = "1.0.0",
        instrument: str = "SPY",
        timeframe: str = "1d",
        start: date | None = None,
    ) -> PaperRun:
        inputs = build_inputs(bars, states, indicators, times)
        return self.replay_inputs(
            inputs,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            instrument=instrument,
            timeframe=timeframe,
            start=start,
            data_fp=data_fingerprint(bars),
        )

    def replay_inputs(
        self,
        inputs: list[BarInput],
        *,
        strategy_id: str,
        strategy_version: str = "1.0.0",
        instrument: str = "SPY",
        timeframe: str = "1d",
        start: date | None = None,
        data_fp: str = "",
    ) -> PaperRun:
        history: list[BarInput] = []
        if start is not None:
            history, inputs = split_at_start(inputs, start)
        if not inputs:
            raise ValueError("no sessions to replay")
        net = PaperTrader.new(
            self.config,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            instrument=instrument,
            timeframe=timeframe,
        )
        # convention A: the decision at the close before the window (not a result session)
        reference = net.prime(history) if history else None
        steps = [net.process(b) for b in inputs]
        decided = steps if reference is None else [reference, *steps]
        targets = {s.session: s.decision.risk_approved_target for s in decided}
        gross_cfg = PaperConfig(
            risk=self.config.risk,
            backtest=self.config.backtest.without_costs(),
            max_gross_exposure=self.config.max_gross_exposure,
            cash_tolerance=self.config.cash_tolerance,
        )
        gross = PaperTrader.new(
            gross_cfg,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            instrument=instrument,
            timeframe=timeframe,
            approved_targets=targets,
        )
        if history:
            gross.prime(history)
        gross_equity = pd.Series(
            [gross.process(b).equity for b in inputs],
            index=pd.DatetimeIndex([b.bar_ts for b in inputs], name="ts"),
        )
        return self._assemble(net, steps, gross_equity, data_fp, reference)

    def _assemble(
        self,
        trader: PaperTrader,
        steps: list[StepResult],
        gross_equity: pd.Series,
        data_fp: str,
        reference: StepResult | None = None,
    ) -> PaperRun:
        """Results (equity, returns, metrics, portfolio, trades) come from the window sessions
        only. The `reference` decision before the window is in `decisions` (as in the research
        engine) and, with its reference snapshot, in the ledger `events` only."""
        st = trader.state
        bt = self.config.backtest
        initial = bt.initial_capital
        events: list[dict[str, Any]] = []
        prev = ""
        decided = steps if reference is None else [reference, *steps]
        for s in decided:
            evs = step_events(s, st.account_id, len(events) + 1, prev)
            events += evs
            prev = evs[-1]["hash"]
        eq = pd.DataFrame(
            [
                {
                    "ts": s.session,
                    "position": s.exposure,
                    "executed": s.executed,
                    "traded_notional": s.traded_notional,
                    "costs": s.costs,
                    "cash": s.snapshot.cash,
                    "shares": s.snapshot.position_quantity,
                    "equity": s.equity,
                }
                for s in steps
            ]
        ).set_index("ts")
        eq["daily_return"] = defined_returns(eq["equity"], initial)  # pd.NA where undefined
        eq["cumulative_return"] = eq["equity"] / initial - 1
        eq = eq.join(drawdown(eq["equity"], initial))
        eq["gross_equity"] = gross_equity
        trades = pd.DataFrame([s.trade for s in steps if s.trade], columns=list(TRADE_COLUMNS))
        metrics = without_undefined_return_metrics(
            all_metrics(eq, gross_equity, trades, initial, bt.periods_per_year, bt.risk_free_rate),
            int(undefined_mask(eq["daily_return"]).sum()),
            int(undefined_mask(defined_returns(gross_equity, initial)).sum()),
        )
        # Phase 8 conventions: total = final equity - initial; unrealized = total - realized
        metrics["realized_pnl"] = st.account.realized_pnl
        metrics["total_pnl"] = float(eq["equity"].iloc[-1]) - initial
        metrics["unrealized_pnl"] = metrics["total_pnl"] - metrics["realized_pnl"]
        pending = trader.pending_action()
        orders = [s.order for s in steps if s.order is not None]
        order_rows = [asdict(o) for o in orders]
        if pending is not None:
            order_rows.append(
                {
                    "order_id": pending.order_id,
                    "decision_id": pending.decision_id,
                    "order_type": "market_on_open_target_exposure",
                    "side": pending.side,
                    "action": pending.kind,
                    "requested_quantity": float("nan"),
                    "approved_quantity": float("nan"),
                    "scheduled_execution_at": pending.scheduled_execution_at,
                    "status": pending.status,
                    "rejection_reason": pending.note,
                }
            )
        return PaperRun(
            account_id=st.account_id,
            strategy_id=st.strategy_id,
            strategy_version=st.strategy_version,
            instrument=st.instrument,
            timeframe=st.timeframe,
            risk_scenario=self.config.risk.name,
            config_fingerprint=self.config.fingerprint(),
            data_fingerprint=data_fp,
            decisions=_frame([s.decision for s in decided], Decision),
            orders=pd.DataFrame(order_rows, columns=[f.name for f in fields(Order)]),
            fills=_frame([s.fill for s in steps if s.fill is not None], Fill),
            portfolio=_frame([s.snapshot for s in steps], AccountSnapshot),
            reconciliation=_frame(
                [s.reconciliation for s in steps if s.reconciliation is not None], Reconciliation
            ),
            equity=eq,
            trades=trades,
            pending_order=asdict(pending) if pending is not None else None,
            events=events,
            final_state=st,
            metrics=metrics,
        )


def _frame(rows: list[Any], cls: type) -> pd.DataFrame:
    """Typed records -> DataFrame with the dataclass field order (fixed schema, even if empty)."""
    return pd.DataFrame([asdict(r) for r in rows], columns=[f.name for f in fields(cls)])


def replay_baselines(
    bars: pd.DataFrame,
    config: PaperConfig | None = None,
    strategy_engine: StrategyEngine | None = None,
    *,
    instrument: str = "SPY",
    start: date | None = None,
) -> dict[str, PaperRun]:
    """Historical replay of the six Phase 7 baselines through risk -> paper broker."""
    engine = PaperTradingEngine(config)
    fp = data_fingerprint(bars)
    return {
        sid: engine.replay_inputs(
            inputs,
            strategy_id=sid,
            strategy_version=version,
            instrument=instrument,
            start=start,
            data_fp=fp,
        )
        for sid, (version, inputs) in baseline_inputs(bars, strategy_engine).items()
    }


__all__ = [
    "PAPER_VERSION",
    "PaperRun",
    "PaperTradingEngine",
    "baseline_inputs",
    "build_inputs",
    "data_fingerprint",
    "replay_baselines",
]
