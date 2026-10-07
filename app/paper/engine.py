"""Paper-trading engine (historical replay). NOT live trading; no real-money execution exists.

Per session t, in order (the Phase 8/11 temporal contract):
  1. OPEN of t: the PaperBroker executes the risk-approved instruction decided at the close of
     t-1 (scheduled for this session); fills at the raw open with Phase 8 costs;
  2. CLOSE of t: portfolio valued; risk monitors updated; stop evaluated on the close;
  3. DECISION at the close of t: strategy state -> requested target -> RiskManager ->
     approved target -> Instruction scheduled for the next XNYS session open.
A decision is never executed on the bar that produced it; a decision on the last bar with no
next session stays PENDING. The strategy state is never modified.
"""

import hashlib
from dataclasses import asdict, dataclass, field, fields
from typing import Any

import numpy as np
import pandas as pd

from app.backtest.engine import session_times
from app.backtest.metrics import all_metrics, drawdown, returns_from_equity
from app.backtest.portfolio import target_positions
from app.data.calendar import TradingCalendar
from app.indicators.engine import IndicatorEngine
from app.paper.broker import PaperBroker
from app.paper.config import PAPER_VERSION, PaperConfig
from app.paper.models import (
    ORDER_TYPE,
    PENDING,
    Decision,
    Fill,
    Instruction,
    Order,
    PortfolioSnapshot,
    Reconciliation,
)
from app.risk.manager import EPS, RiskManager
from app.strategies.engine import StrategyEngine

TRADE_COLUMNS = ("entry_time", "exit_time", "net_pnl", "net_return", "holding_sessions")


@dataclass
class PaperRun:
    run_id: str
    strategy_id: str
    risk_scenario: str
    config_fingerprint: str
    data_fingerprint: str
    decisions: pd.DataFrame
    orders: pd.DataFrame
    fills: pd.DataFrame
    portfolio: pd.DataFrame
    reconciliation: pd.DataFrame
    equity: pd.DataFrame  # Phase 8-compatible curve (for Phase 8 metric functions)
    trades: pd.DataFrame
    pending_order: dict[str, Any] | None
    metrics: dict[str, float] = field(default_factory=dict)


def data_fingerprint(bars: pd.DataFrame) -> str:
    ohlcv = bars[["open", "high", "low", "close", "volume"]]
    raw = np.asarray(pd.util.hash_pandas_object(ohlcv)).tobytes()
    return hashlib.sha256(raw).hexdigest()[:16]


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
        data_fp: str = "",
    ) -> PaperRun:
        net = self._replay(
            bars, states, indicators, times, strategy_id, strategy_version, instrument, data_fp
        )
        gross_cfg = PaperConfig(
            risk=self.config.risk,
            backtest=self.config.backtest.without_costs(),
            max_gross_exposure=self.config.max_gross_exposure,
        )
        gross = PaperTradingEngine(gross_cfg)._replay(
            bars,
            states,
            indicators,
            times,
            strategy_id,
            strategy_version,
            instrument,
            data_fp,
            forced_targets=net["targets"],
        )
        return self._assemble(net, gross["equity_rows"], strategy_id, data_fp)

    # ── core loop ──

    def _replay(
        self,
        bars: pd.DataFrame,
        states: pd.Series,
        indicators: pd.DataFrame,
        times: pd.DataFrame,
        strategy_id: str,
        strategy_version: str,
        instrument: str,
        data_fp: str,
        forced_targets: dict[int, float] | None = None,
    ) -> dict[str, Any]:
        cfg, risk = self.config, self.config.risk
        idx = pd.DatetimeIndex(bars.index)
        if not idx.is_monotonic_increasing or not idx.is_unique:
            raise ValueError("bars must be in strictly increasing time order")
        st = states.reindex(idx)
        if st.isna().any():
            raise ValueError("strategy states missing for some bars")
        state_list = st.tolist()
        requested = target_positions(state_list)  # Phase 8 policy, strategy state untouched
        opens, lows, closes = (bars[c].to_numpy(np.float64) for c in ("open", "low", "close"))
        vol = indicators["realized_vol_20"].reindex(idx).to_numpy(np.float64)
        atr = indicators["atr_14"].reindex(idx).to_numpy(np.float64)
        observed = list(times["observed_at"].reindex(idx))
        effective = list(times["effective_at"].reindex(idx))
        run_id = hashlib.sha256(
            f"{cfg.fingerprint()}|{strategy_id}|{data_fp}".encode()
        ).hexdigest()[:12]

        broker = PaperBroker(cfg)
        rm = RiskManager(risk, float(cfg.backtest.initial_capital))
        out: dict[str, Any] = {
            k: []
            for k in ("decisions", "orders", "fills", "snapshots", "recon", "equity_rows", "trades")
        }
        out["targets"], out["run_id"] = {}, run_id
        instruction: Instruction | None = None
        entry: dict[str, Any] | None = None
        last_reasons = ""

        for t in range(len(idx)):
            cost_today, traded, executed = 0.0, 0.0, False
            if instruction is not None:  # 1. open of t
                eq_open, was_flat = broker.equity(opens[t]), broker.shares <= EPS
                res = broker.execute(
                    instruction,
                    session_ts=idx[t],
                    open_price=float(opens[t]),
                    band=risk.rebalance_band,
                    id_prefix=run_id,
                )
                if res.order is not None:
                    out["orders"].append(res.order)
                if res.fill is not None:
                    out["fills"].append(res.fill)
                    entered = was_flat and broker.shares > EPS
                    if entered:
                        entry = {"t": t, "equity": eq_open}
                    pnl = None
                    if broker.shares <= EPS and entry is not None:
                        pnl = broker.cash - entry["equity"]
                        out["trades"].append(
                            {
                                "entry_time": idx[entry["t"]],
                                "exit_time": idx[t],
                                "net_pnl": pnl,
                                "net_return": pnl / entry["equity"],
                                "holding_sessions": t - entry["t"],
                            }
                        )
                        entry = None
                    rm.on_fill(
                        target=instruction.risk_approved_target,
                        entered=entered,
                        exited=pnl is not None,
                        price=float(opens[t]),
                        atr_at_decision=float(atr[t - 1]),
                        round_trip_pnl=pnl,
                    )
                    cost_today, traded, executed = res.fill.total_cost, res.fill.notional, True
                out["recon"].append(
                    self._reconcile(instruction, idx[t], broker, float(opens[t]), res, last_reasons)
                )
                instruction = None

            equity = broker.equity(closes[t])  # 2. close of t
            out["snapshots"].append(
                PortfolioSnapshot(
                    idx[t],
                    broker.cash,
                    broker.shares,
                    broker.shares * closes[t],
                    broker.shares * closes[t] / equity,
                    equity,
                    broker.realized_pnl,
                    (closes[t] - broker.avg_cost) * broker.shares if broker.shares > EPS else 0.0,
                    broker.cumulative_costs,
                    rm.risk_state,
                )
            )
            out["equity_rows"].append(
                {
                    "ts": idx[t],
                    "position": broker.shares * closes[t] / equity,
                    "executed": executed,
                    "traded_notional": traded,
                    "costs": cost_today,
                    "cash": broker.cash,
                    "shares": broker.shares,
                    "equity": equity,
                }
            )
            triggered, touch = rm.on_close(
                equity, low=lows[t], close=closes[t], holding=broker.shares > EPS
            )

            decision_id = f"{run_id}-D{t:06d}"  # 3. decision at the close of t
            if forced_targets is not None:
                approved, reasons, intervention, rstate = (
                    forced_targets[t],
                    "",
                    False,
                    rm.risk_state,
                )
            else:
                d = rm.decide(
                    requested=float(requested[t]),
                    close=closes[t],
                    realized_vol=vol[t],
                    atr=atr[t],
                    equity=equity,
                    triggered=triggered,
                    touch=touch,
                )
                approved, reasons, intervention, rstate = (
                    d.approved_exposure,
                    ";".join(d.reasons),
                    d.intervention,
                    d.risk_state,
                )
            out["targets"][t] = approved
            last_reasons = reasons
            out["decisions"].append(
                Decision(
                    decision_id,
                    strategy_id,
                    strategy_version,
                    instrument,
                    idx[t],
                    observed[t],
                    state_list[t],
                    float(requested[t]),
                    approved,
                    intervention,
                    reasons,
                    rstate,
                    effective[t],
                    cfg.fingerprint(),
                    data_fp,
                )
            )
            instruction = Instruction(
                decision_id, observed[t], effective[t], float(requested[t]), approved
            )

        out["pending"] = None
        if instruction is not None and (instruction.risk_approved_target <= EPS) != (
            broker.shares <= EPS
        ):
            side = "buy" if instruction.risk_approved_target > EPS else "sell"
            order = Order(
                f"{run_id}-O-PENDING",
                instruction.decision_id,
                ORDER_TYPE,
                side,
                float("nan"),
                float("nan"),
                instruction.scheduled_for,
                PENDING,
                "no next session in the data; never executed on an invented bar",
            )
            out["orders"].append(order)
            out["pending"] = asdict(order)
        return out

    @staticmethod
    def _reconcile(
        ins: Instruction,
        ts: pd.Timestamp,
        broker: PaperBroker,
        price: float,
        res: Any,
        reasons: str,
    ) -> Reconciliation:
        exposure = broker.exposure(price)
        if res.order is None:
            outcome = f"no_order_required:{res.no_order_reason}"
            why = (
                "actual exposure within the rebalance band of the approved target"
                if res.no_order_reason == "within_rebalance_band"
                else "approved target 0 and no position"
            )
            return Reconciliation(
                ts,
                ins.decision_id,
                ins.strategy_requested_target,
                ins.risk_approved_target,
                exposure,
                "",
                outcome,
                why,
            )
        if res.fill is None:
            return Reconciliation(
                ts,
                ins.decision_id,
                ins.strategy_requested_target,
                ins.risk_approved_target,
                exposure,
                res.order.order_id,
                f"rejected:{res.order.rejection_reason}",
                "order rejected by broker safety check; position unchanged",
            )
        why = "risk-approved target executed"
        if abs(ins.strategy_requested_target - ins.risk_approved_target) > EPS:
            why += f"; differs from strategy request because: {reasons or 'risk decision'}"
        return Reconciliation(
            ts,
            ins.decision_id,
            ins.strategy_requested_target,
            ins.risk_approved_target,
            exposure,
            res.order.order_id,
            "filled",
            why,
        )

    def _assemble(
        self, net: dict[str, Any], gross_rows: list[dict[str, Any]], sid: str, data_fp: str
    ) -> PaperRun:
        bt = self.config.backtest
        initial = bt.initial_capital
        eq = pd.DataFrame(net["equity_rows"]).set_index("ts")
        eq["daily_return"] = returns_from_equity(eq["equity"], initial)
        eq["cumulative_return"] = eq["equity"] / initial - 1
        eq = eq.join(drawdown(eq["equity"], initial))
        gross_equity = pd.DataFrame(gross_rows).set_index("ts")["equity"]
        eq["gross_equity"] = gross_equity
        trades = pd.DataFrame(net["trades"], columns=list(TRADE_COLUMNS))
        metrics = all_metrics(
            eq, gross_equity, trades, initial, bt.periods_per_year, bt.risk_free_rate
        )
        decisions = _frame(net["decisions"], Decision)
        orders = _frame(net["orders"], Order)
        return PaperRun(
            run_id=net["run_id"],
            strategy_id=sid,
            risk_scenario=self.config.risk.name,
            config_fingerprint=self.config.fingerprint(),
            data_fingerprint=data_fp,
            decisions=decisions,
            orders=orders,
            fills=_frame(net["fills"], Fill),
            portfolio=_frame(net["snapshots"], PortfolioSnapshot),
            reconciliation=_frame(net["recon"], Reconciliation),
            equity=eq,
            trades=trades,
            pending_order=net["pending"],
            metrics=metrics,
        )


def _frame(rows: list[Any], cls: type) -> pd.DataFrame:
    """Typed records -> DataFrame with the dataclass field order (fixed schema, even if empty)."""
    return pd.DataFrame([asdict(r) for r in rows], columns=[f.name for f in fields(cls)])


def replay_baselines(
    bars: pd.DataFrame,
    config: PaperConfig | None = None,
    strategy_engine: StrategyEngine | None = None,
) -> dict[str, PaperRun]:
    """Historical replay of the six Phase 7 baselines through risk -> paper broker."""
    ohlcv = bars[["open", "high", "low", "close", "volume"]]
    run = (strategy_engine or StrategyEngine()).run(ohlcv)
    indicators = IndicatorEngine().analyze(ohlcv).values
    times = session_times(pd.DatetimeIndex(bars.index), TradingCalendar("XNYS"))
    fp = data_fingerprint(bars)
    engine = PaperTradingEngine(config)
    out = {}
    for sid, g in run.signals.groupby("strategy_id", sort=False):
        st = pd.Series(g["state"].to_numpy(), index=pd.DatetimeIndex(g["bar_ts"]))
        version = str(g["strategy_version"].iloc[0])
        out[str(sid)] = engine.replay(
            bars, st, indicators, times, strategy_id=str(sid), strategy_version=version, data_fp=fp
        )
    return out


__all__ = [
    "PAPER_VERSION",
    "PaperRun",
    "PaperTradingEngine",
    "data_fingerprint",
    "replay_baselines",
]
