"""The pre-registered trend policy: SMA200 state, vol-scaled weight, state-change/drift rule.

Inputs are precomputed per asset on its own sessions with backward-looking windows only. At
D(T) the policy reads, for each asset, the row of its latest session on or before T.
"""

from dataclasses import dataclass, field

import pandas as pd

from app.trend.allocator import needs_trade, realised_vol, target_weight, vol_is_usable
from app.trend.signal import CASH, LONG, sma_states
from app.trend.simulator import Context


def signal_table(close: pd.Series) -> pd.DataFrame:
    """State, volatility and stale flag per own session (all causal)."""
    table = realised_vol(close)
    table.insert(0, "state", sma_states(close))
    return table


@dataclass
class TrendPolicy:
    tables: dict[str, pd.DataFrame]  # asset -> signal_table on its own sessions
    assets: tuple[str, ...]
    last_state: dict[str, str] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    decisions: list[dict[str, object]] = field(default_factory=list)

    def _row(self, asset: str, day: pd.Timestamp) -> pd.Series | None:
        table = self.tables[asset]
        pos = int(table.index.searchsorted(day, side="right")) - 1
        return None if pos < 0 else table.iloc[pos]

    def decide(self, ctx: Context) -> dict[str, float]:
        n = len(self.assets)
        out: dict[str, float] = {}
        for a in self.assets:
            if a in ctx.pending:
                continue
            row = self._row(a, ctx.day)
            if row is None:
                continue
            state = str(row["state"])
            if state not in (LONG, CASH):
                continue  # INSUFFICIENT_DATA never opens or closes a position
            vol, stale = float(row["vol"]), bool(row["stale"])
            if not vol_is_usable(vol, stale):
                self.flags.append(f"{ctx.day.date()} {a}: unusable volatility, no trade")
                continue
            w_target = target_weight(state, vol, n)
            px = ctx.prices.get(a)
            w_actual = (ctx.units[a] * px / ctx.nav) if px and ctx.nav > 0 else 0.0
            trade = needs_trade(state, self.last_state.get(a), w_actual, w_target)
            self.decisions.append(
                {
                    "date": ctx.day,
                    "asset": a,
                    "state": state,
                    "vol": vol,
                    "w_target": w_target,
                    "w_actual": w_actual,
                    "trade": trade,
                }
            )
            self.last_state[a] = state
            if trade:
                out[a] = w_target
        return out
