"""Paper account: cash, position, cost basis and P&L (average-cost method, costs capitalised).

Conventions (docs/paper-trading.md):
    cost_basis      = notional + transaction costs of the shares held; a buy adds its notional
                      and its costs; a sell removes the sold fraction q_sold / q_before of the
                      basis (all of it on a full exit)
    realized_pnl   += (sell notional - sell costs) - basis removed            (per sell)
    unrealized_pnl  = position_quantity x mark - cost_basis                   (0 when flat)
    total_pnl       = realized_pnl + unrealized_pnl
    equity          = cash + position_quantity x mark
Identity (exact in real arithmetic, ~1e-9 relative in floating point):
    total_pnl = equity - initial_capital
For an all-in / all-out round trip, realized P&L equals the Phase 8 trade `net_pnl`
(exit notional - entry notional - entry costs - exit costs).
Cash and quantity updates themselves come from `app.risk.manager.apply_order` (Phase 11), so
the economic path is identical to the Phase 8/11 simulations.
"""

from dataclasses import dataclass

from app.backtest.models import Costs
from app.paper.models import ADD, ENTRY, EXIT, REDUCE

EPS = 1e-12


@dataclass
class Account:
    initial_capital: float
    cash: float
    shares: float = 0.0
    basis_notional: float = 0.0
    basis_costs: float = 0.0
    realized_pnl: float = 0.0
    cumulative_costs: float = 0.0

    @classmethod
    def open(cls, initial_capital: float) -> "Account":
        return cls(initial_capital=initial_capital, cash=initial_capital)

    @property
    def holding(self) -> bool:
        return self.shares > EPS

    @property
    def cost_basis(self) -> float:
        return self.basis_notional + self.basis_costs if self.holding else 0.0

    def equity(self, price: float) -> float:
        return self.cash + self.shares * price

    def exposure(self, price: float) -> float:
        eq = self.equity(price)
        return self.shares * price / eq if eq > 0 else 0.0

    def unrealized_pnl(self, mark: float) -> float:
        return self.shares * mark - self.cost_basis if self.holding else 0.0

    def apply_fill(
        self, *, side: str, notional: float, costs: Costs, new_cash: float, new_shares: float
    ) -> tuple[str, float]:
        """Books one fill whose cash/quantity result was computed by `apply_order`.
        Returns (action, realized P&L of this fill)."""
        before = self.shares
        realized = 0.0
        if side == "buy":
            action = ENTRY if before <= EPS else ADD
            self.basis_notional += notional
            self.basis_costs += costs.total
        else:
            if new_shares <= EPS:
                action = EXIT
                sold_notional, sold_costs = self.basis_notional, self.basis_costs
                self.basis_notional = self.basis_costs = 0.0
            else:
                action = REDUCE
                frac = (before - new_shares) / before
                sold_notional = self.basis_notional * frac
                sold_costs = self.basis_costs * frac
                self.basis_notional -= sold_notional
                self.basis_costs -= sold_costs
            realized = (notional - sold_notional) - (sold_costs + costs.total)
            self.realized_pnl += realized
        self.cash, self.shares = new_cash, new_shares
        self.cumulative_costs += costs.total
        return action, realized
