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

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from app.backtest.config import BacktestConfig
from app.backtest.execution import buy_notional, order_costs
from app.backtest.metrics import all_metrics, drawdown, returns_from_equity
from app.backtest.portfolio import target_positions
from app.risk.config import RISK_VERSION, RiskConfig
from app.risk.limits import LOCKED, NORMAL, WARNING, DrawdownMonitor, SessionMonitor, cap_exposure
from app.risk.sizing import size_exposure
from app.risk.stops import evaluate_stop, stop_level

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
    cash, shares, current = float(bt.initial_capital), 0.0, 0.0
    dd = DrawdownMonitor(risk.drawdown, hwm=cash)
    sess = SessionMonitor(risk.session, last_equity=cash)
    stop = math.nan
    stop_lockout = False
    entry: dict[str, Any] | None = None
    targets: dict[int, float] = {}
    decisions, rows, fills, trades = [], [], [], []

    def decide(t: int, equity: float, triggered: bool, touch: bool) -> None:
        nonlocal stop_lockout
        req = float(inp.requested[t])
        reasons: list[str] = []
        if forced_targets is not None:
            targets[t] = forced_targets.get(t, current)
            return
        if req > 0:
            sized, why = size_exposure(
                risk.sizing, close=inp.closes[t], realized_vol=inp.vol[t], atr=inp.atr[t]
            )
            if why == "risk_input_undefined":
                reasons.append(why)
            elif sized < req:
                reasons.append(f"sizing_{why}")
        else:
            sized = 0.0
        approved, lim = cap_exposure(sized, risk.limits, equity=equity)
        if lim:
            reasons.append(lim)
        if triggered:
            stop_lockout = True
        if stop_lockout:
            if req == 0:
                stop_lockout = False  # strategy signal reset: re-entry allowed again
            elif approved > 0:
                approved = 0.0
                reasons.append("stop_triggered" if triggered else "stop_lockout_until_signal_reset")
        if dd.state == LOCKED:
            if risk.drawdown.action == "force_flat" and approved > 0:
                approved = 0.0
                reasons.append("drawdown_lock_flat")
            elif approved > current:
                approved = current
                reasons.append("drawdown_lock_no_increase")
        if sess.locked and approved > current:
            approved = current
            reasons.append("session_lock_no_increase")
        target = approved
        targets[t] = target
        decisions.append(
            {
                "bar_ts": inp.index[t],
                "observed_at": inp.observed_at[t],
                "effective_at": inp.effective_at[t],
                "strategy_state": inp.states[t],
                "requested_exposure": req,
                "sized_exposure": sized,
                "approved_exposure": approved,
                "target_exposure": target,
                "intervention": bool(abs(approved - req) > EPS),
                "reasons": ";".join(reasons),
                "risk_state": LOCKED if (dd.state == LOCKED or sess.locked) else dd.state,
                "drawdown": equity / dd.hwm - 1 if dd.hwm > 0 else math.nan,
                "high_water_mark": dd.hwm,
                "stop_level": stop,
                "stop_triggered": triggered,
                "stop_intraday_touch": touch,
                "realized_vol": inp.vol[t],
                "atr": inp.atr[t],
            }
        )

    if inp.start >= 1:  # decision on the bar before the window (fresh capital, no history)
        decide(inp.start - 1, cash, False, False)

    for t in range(inp.start, n):
        cost_today, traded, executed = 0.0, 0.0, False
        target = targets.get(t - 1, current)
        price_open = float(inp.opens[t])
        eq_now = cash + shares * price_open
        actual = shares * price_open / eq_now if eq_now > 0 else 0.0
        # Trade when entering, exiting, or when the ACTUAL exposure at this open differs from
        # the target by at least the rebalance band (drift is corrected, small moves are not).
        enter_or_exit = (target <= EPS) != (shares <= EPS)
        if enter_or_exit or (target > EPS and abs(actual - target) >= risk.rebalance_band):
            if inp.index[t] != pd.Timestamp(inp.effective_at[t - 1]):
                raise ValueError(
                    "next bar is not the next exchange session (missing session in data)"
                )
            price = float(inp.opens[t])
            eq_open = cash + shares * price
            was_flat = shares <= EPS
            if target <= EPS:
                notional, side = shares * price, "sell"
            else:
                desired, value = target * eq_open, shares * price
                if desired > value:
                    notional = min(desired - value, buy_notional(cash, bt)) if cash > 0 else 0.0
                    side = "buy"
                else:
                    notional, side = value - desired, "sell"
            costs = order_costs(notional, bt)
            if side == "buy":
                shares += notional / price
                cash -= notional + costs.total
            else:
                shares = 0.0 if target <= EPS else shares - notional / price
                cash += notional - costs.total
            fills.append(
                {
                    "ts": inp.index[t],
                    "side": side,
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
            if was_flat and shares > EPS:
                entry = {"t": t, "equity": eq_open, "price": price, "costs": 0.0}
                stop = stop_level(
                    risk.stop, entry_price=price, atr_at_decision=float(inp.atr[t - 1])
                )
            if entry is not None:
                entry["costs"] += costs.total
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
                sess.record_round_trip(pnl)
                entry, stop = None, math.nan
            current, cost_today, traded, executed = target, costs.total, notional, True

        equity = cash + shares * inp.closes[t]
        dd.update(equity)
        sess.update(equity)
        triggered, touch = (
            evaluate_stop(stop, low=inp.lows[t], close=inp.closes[t])
            if shares > EPS
            else (False, False)
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
                "risk_state": LOCKED if (dd.state == LOCKED or sess.locked) else dd.state,
            }
        )
        decide(t, equity, triggered, touch)

    pending = None
    last_target = targets.get(n - 1, current)
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
        "dd": dd,
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
