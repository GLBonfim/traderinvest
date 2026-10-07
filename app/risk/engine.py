"""Risk overlay simulation: strategy state -> risk decision -> target exposure -> execution.

Generalises the Phase 8 accounting from positions {0, 1} to exposures in [0, 1] WITHOUT
changing Phase 8: the same next-session-open execution (validated against `effective_at`),
the same cost functions (`order_costs`, `buy_notional`) and the same metric functions are
reused. With the control scenario (no overlay) the equity curve is identical to Phase 8
(tested).

For each session t of the window, in order:
  1. OPEN of t: execute the target decided at the close of t-1 (if it differs from the
     current executed target);
  2. CLOSE of t: mark to market;
  3. update drawdown / session monitors with the equity known at that close;
  4. evaluate the stop on the close of t (never on intraday ordering);
  5. risk DECISION for the close of t from: strategy state(t), realized vol(t), ATR(t),
     close(t), equity(t), monitors, current target.
Nothing at t reads data from t+1 or later. The strategy's state is never modified: each
decision row keeps `requested_exposure` next to `approved_exposure`.
"""

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from app.backtest.config import BacktestConfig
from app.backtest.metrics import all_metrics, drawdown, returns_from_equity
from app.backtest.portfolio import target_positions
from app.risk.config import RISK_VERSION, RiskConfig
from app.risk.limits import LOCKED, NORMAL, WARNING
from app.risk.manager import RiskManager, apply_order, plan_order

EPS = 1e-12
TRADE_COLUMNS = (
    "entry_time",
    "exit_time",
    "entry_price",
    "exit_price",
    "net_pnl",
    "net_return",
    "costs",
    "holding_sessions",
    "exit_reason",
)


@dataclass
class RiskRun:
    strategy_id: str
    scenario: str
    decisions: pd.DataFrame  # one row per decision bar: requested vs approved, reasons, state
    equity: pd.DataFrame  # Phase 8-compatible equity curve (position = exposure at close)
    fills: pd.DataFrame
    trades: pd.DataFrame  # round trips flat -> flat
    open_position: dict[str, Any] | None
    pending: dict[str, Any] | None
    metrics: dict[str, float] = field(default_factory=dict)
    diagnostics: dict[str, float] = field(default_factory=dict)
    risk_fingerprint: str = ""
    backtest_fingerprint: str = ""


@dataclass
class _Inputs:
    index: pd.DatetimeIndex
    opens: np.ndarray
    lows: np.ndarray
    closes: np.ndarray
    requested: np.ndarray  # Phase 8 policy: LONG 1, FLAT 0, INSUFFICIENT keeps previous
    states: list[str]
    vol: np.ndarray
    atr: np.ndarray
    observed_at: list[pd.Timestamp]
    effective_at: list[pd.Timestamp]
    start: int


def simulate(
    inp: _Inputs,
    risk: RiskConfig,
    bt: BacktestConfig,
    forced_targets: dict[int, float] | None = None,
) -> dict[str, Any]:
    """Runs the overlay. `forced_targets` (decision index -> target) replays the targets of
    another run (used for the zero-cost gross track: same decisions, no costs)."""
    n = len(inp.index)
    cash, shares = float(bt.initial_capital), 0.0
    rm = RiskManager(risk, cash)
    entry: dict[str, Any] | None = None
    targets: dict[int, float] = {}
    decisions, rows, fills, trades = [], [], [], []

    def decide(t: int, equity: float, triggered: bool, touch: bool) -> None:
        if forced_targets is not None:
            targets[t] = forced_targets.get(t, rm.current_target)
            return
        d = rm.decide(
            requested=float(inp.requested[t]),
            close=inp.closes[t],
            realized_vol=inp.vol[t],
            atr=inp.atr[t],
            equity=equity,
            triggered=triggered,
            touch=touch,
        )
        targets[t] = d.approved_exposure
        decisions.append(
            {
                "bar_ts": inp.index[t],
                "observed_at": inp.observed_at[t],
                "effective_at": inp.effective_at[t],
                "strategy_state": inp.states[t],
                "requested_exposure": d.requested_exposure,
                "sized_exposure": d.sized_exposure,
                "approved_exposure": d.approved_exposure,
                "target_exposure": d.approved_exposure,
                "intervention": d.intervention,
                "reasons": ";".join(d.reasons),
                "risk_state": d.risk_state,
                "drawdown": d.drawdown,
                "high_water_mark": d.high_water_mark,
                "stop_level": d.stop_level,
                "stop_triggered": d.stop_triggered,
                "stop_intraday_touch": d.stop_intraday_touch,
                "realized_vol": inp.vol[t],
                "atr": inp.atr[t],
            }
        )

    if inp.start >= 1:  # decision on the bar before the window (fresh capital, no history)
        decide(inp.start - 1, cash, False, False)

    for t in range(inp.start, n):
        cost_today, traded, executed = 0.0, 0.0, False
        target = targets.get(t - 1, rm.current_target)
        price = float(inp.opens[t])
        # Trade on entry/exit, or when the ACTUAL exposure at this open differs from the target
        # by at least the rebalance band (drift is corrected, small moves are not).
        order = plan_order(
            target, cash=cash, shares=shares, price=price, band=risk.rebalance_band, bt=bt
        )
        if order is not None:
            if inp.index[t] != pd.Timestamp(inp.effective_at[t - 1]):
                raise ValueError(
                    "next bar is not the next exchange session (missing session in data)"
                )
            eq_open = cash + shares * price
            was_flat = shares <= EPS
            cash, shares, costs = apply_order(order, cash=cash, shares=shares, price=price, bt=bt)
            notional = order.notional
            fills.append(
                {
                    "ts": inp.index[t],
                    "side": order.side,
                    "price": price,
                    "notional": notional,
                    "commission": costs.commission,
                    "spread_cost": costs.spread,
                    "slippage_cost": costs.slippage,
                    "total_cost": costs.total,
                    "target_exposure": target,
                    "signal_observed_at": inp.observed_at[t - 1],
                }
            )
            entered = was_flat and shares > EPS
            if entered:
                entry = {"t": t, "equity": eq_open, "price": price, "costs": 0.0}
            if entry is not None:
                entry["costs"] += costs.total
            pnl: float | None = None
            if shares <= EPS and entry is not None:
                pnl = cash - entry["equity"]
                last = decisions[-1]["reasons"] if decisions else ""
                trades.append(
                    {
                        "entry_time": inp.index[entry["t"]],
                        "exit_time": inp.index[t],
                        "entry_price": entry["price"],
                        "exit_price": price,
                        "net_pnl": pnl,
                        "net_return": pnl / entry["equity"],
                        "costs": entry["costs"],
                        "holding_sessions": t - entry["t"],
                        "exit_reason": _exit_reason(last),
                    }
                )
                entry = None
            rm.on_fill(
                target=target,
                entered=entered,
                exited=pnl is not None,
                price=price,
                atr_at_decision=float(inp.atr[t - 1]),
                round_trip_pnl=pnl,
            )
            cost_today, traded, executed = costs.total, notional, True

        equity = cash + shares * inp.closes[t]
        triggered, touch = rm.on_close(
            equity, low=inp.lows[t], close=inp.closes[t], holding=shares > EPS
        )
        rows.append(
            {
                "ts": inp.index[t],
                "state": inp.states[t],
                "position": shares * inp.closes[t] / equity,
                "shares": shares,
                "open": inp.opens[t],
                "close": inp.closes[t],
                "executed": executed,
                "traded_notional": traded,
                "costs": cost_today,
                "cash": cash,
                "equity": equity,
                "risk_state": rm.risk_state,
            }
        )
        decide(t, equity, triggered, touch)

    pending = None
    last_target = targets.get(n - 1, rm.current_target)
    if n and (last_target <= EPS) != (shares <= EPS):
        pending = {
            "target_exposure": last_target,
            "observed_at": inp.observed_at[n - 1],
            "status": "not_executed_no_next_session_in_data",
        }
    open_position = None
    if entry is not None and n:
        open_position = {
            "entry_time": inp.index[entry["t"]],
            "entry_price": entry["price"],
            "mark_time": inp.observed_at[n - 1],
            "mark_price": float(inp.closes[n - 1]),
            "unrealized_net_pnl": rows[-1]["equity"] - entry["equity"],
            "status": "open_marked_to_market",
        }
    return {
        "decisions": decisions,
        "rows": rows,
        "fills": fills,
        "trades": trades,
        "targets": targets,
        "pending": pending,
        "open_position": open_position,
    }


def _exit_reason(reasons: str) -> str:
    for key in ("stop_triggered", "drawdown_lock_flat"):
        if key in reasons:
            return key
    return "strategy_or_sizing"


class RiskOverlay:
    """Applies a RiskConfig to one strategy's states over a backtest window."""

    def __init__(self, risk: RiskConfig, backtest: BacktestConfig | None = None) -> None:
        self.risk = risk
        self.backtest = backtest or BacktestConfig()

    def run(
        self,
        bars: pd.DataFrame,
        states: pd.Series,
        indicators: pd.DataFrame,
        times: pd.DataFrame,
        *,
        strategy_id: str,
        start_index: int = 0,
    ) -> RiskRun:
        idx = pd.DatetimeIndex(bars.index)
        st = states.reindex(idx)
        if st.isna().any():
            raise ValueError("strategy states missing for some bars")
        inp = _Inputs(
            index=idx,
            opens=bars["open"].to_numpy(np.float64),
            lows=bars["low"].to_numpy(np.float64),
            closes=bars["close"].to_numpy(np.float64),
            requested=target_positions(st.tolist()),
            states=st.tolist(),
            vol=indicators["realized_vol_20"].reindex(idx).to_numpy(np.float64),
            atr=indicators["atr_14"].reindex(idx).to_numpy(np.float64),
            observed_at=list(times["observed_at"].reindex(idx)),
            effective_at=list(times["effective_at"].reindex(idx)),
            start=start_index,
        )
        net = simulate(inp, self.risk, self.backtest)
        gross = simulate(inp, self.risk, _no_costs(self.backtest), forced_targets=net["targets"])
        return _assemble(strategy_id, self.risk, self.backtest, net, gross)


def _no_costs(bt: BacktestConfig) -> BacktestConfig:
    return bt.without_costs()


def _assemble(
    sid: str, risk: RiskConfig, bt: BacktestConfig, net: dict[str, Any], gross: dict[str, Any]
) -> RiskRun:
    initial = bt.initial_capital
    eq = pd.DataFrame(net["rows"]).set_index("ts")
    eq["daily_return"] = returns_from_equity(eq["equity"], initial)
    eq["cumulative_return"] = eq["equity"] / initial - 1
    eq = eq.join(drawdown(eq["equity"], initial))
    gross_equity = pd.DataFrame(gross["rows"]).set_index("ts")["equity"]
    eq["gross_equity"] = gross_equity
    trades = pd.DataFrame(net["trades"], columns=list(TRADE_COLUMNS))
    metrics = all_metrics(eq, gross_equity, trades, initial, bt.periods_per_year, bt.risk_free_rate)
    decisions = pd.DataFrame(net["decisions"])
    window = decisions[decisions["bar_ts"] >= eq.index[0]]
    states = eq["risk_state"].value_counts(normalize=True)
    reasons = window["reasons"].str.split(";").explode()
    diag = {
        "mean_requested_exposure": float(window["requested_exposure"].mean()),
        "mean_approved_exposure": float(window["approved_exposure"].mean()),
        "mean_executed_exposure": float(eq["position"].mean()),
        "exposure_reduction": float(
            (window["requested_exposure"] - window["approved_exposure"]).mean()
        ),
        "interventions": float(window["intervention"].sum()),
        "intervention_rate": float(window["intervention"].mean()),
        "stop_triggers": float(window["stop_triggered"].sum()),
        "stop_intraday_touches_not_triggered": float(window["stop_intraday_touch"].sum()),
        "drawdown_lock_events": float(
            ((eq["risk_state"] == LOCKED) & (eq["risk_state"].shift(1) != LOCKED)).sum()
        ),
        **{f"time_in_{s.lower()}": float(states.get(s, 0.0)) for s in (NORMAL, WARNING, LOCKED)},
        **{f"reason_{r}": float(c) for r, c in reasons[reasons != ""].value_counts().items()},
    }
    realized = float(trades["net_pnl"].sum()) if len(trades) else 0.0
    metrics.update(realized_pnl=realized, total_pnl=float(eq["equity"].iloc[-1]) - initial)
    metrics["unrealized_pnl"] = metrics["total_pnl"] - realized
    return RiskRun(
        strategy_id=sid,
        scenario=risk.name,
        decisions=decisions,
        equity=eq,
        fills=pd.DataFrame(net["fills"]),
        trades=trades,
        open_position=net["open_position"],
        pending=net["pending"],
        metrics=metrics,
        diagnostics=diag,
        risk_fingerprint=risk.fingerprint(),
        backtest_fingerprint=bt.fingerprint(),
    )


__all__ = ["RISK_VERSION", "RiskOverlay", "RiskRun"]
