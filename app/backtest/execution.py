"""Execution convention and cost model.

Default (and only) model: a state decided at the close of bar T executes at the OPEN of the next
regular session, which must be bar T+1 of the data (validated against the strategy's
`effective_at`). Prices from bar T (open/high/low/close) and any intraday price of T+1 other than
its open are never used for execution.
"""

from dataclasses import dataclass

import numpy as np

from app.backtest.config import BacktestConfig
from app.backtest.models import Costs


@dataclass(frozen=True)
class FillQuote:
    bar_index: int
    price: float


class ExecutionModel:
    name = "abstract"

    def quote(self, decision_index: int, opens: np.ndarray) -> FillQuote | None:
        raise NotImplementedError


class NextSessionOpen(ExecutionModel):
    name = "next_session_open"

    def quote(self, decision_index: int, opens: np.ndarray) -> FillQuote | None:
        nxt = decision_index + 1
        if nxt >= len(opens):
            return None  # no next session in the data: never invent one
        return FillQuote(nxt, float(opens[nxt]))


def order_costs(notional: float, cfg: BacktestConfig) -> Costs:
    """Costs of one order of `notional` (>= 0): half the quoted spread + slippage + commission."""
    commission = max(
        cfg.commission_per_trade + notional * cfg.commission_bps / 1e4, cfg.minimum_commission
    )
    return Costs(
        commission=commission,
        spread=notional * cfg.spread_bps / 2 / 1e4,
        slippage=notional * cfg.slippage_bps / 1e4,
    )


def buy_notional(cash: float, cfg: BacktestConfig) -> float:
    """Largest notional N with N + costs(N) == cash (all available cash, no leverage)."""
    rate = (cfg.spread_bps / 2 + cfg.slippage_bps) / 1e4
    n = (cash - cfg.commission_per_trade) / (1 + rate + cfg.commission_bps / 1e4)
    if cfg.commission_per_trade + n * cfg.commission_bps / 1e4 < cfg.minimum_commission:
        n = (cash - cfg.minimum_commission) / (1 + rate)
    if n <= 0:
        raise ValueError("insufficient cash to cover the costs of an order")
    return n
