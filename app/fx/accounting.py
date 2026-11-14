"""Multi-currency netting account: positions, realized/unrealized P&L, margin and equity.

- P&L of a pair accrues in its QUOTE currency and is converted to the account currency at the
  conversion rate of the moment (realized: at the fill; unrealized: at the latest quotes).
- Conversion uses mid prices of a direct pair, its inverse, or a cross through USD.
- Unrealized P&L marks a long at the bid and a short at the ask (the price it could be closed
  at), so the spread is a cost from the moment a position opens.
- Margin = notional in account currency / leverage; notional = |units| x rate(base -> account).
- Swap / overnight financing is not modelled (documented limitation, ADR-0036).
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.fx.models import Quote, Side, pair
from app.fx.orders import AccountSnapshot, Position

EPS = 1e-9


class ConversionUnavailableError(LookupError):
    pass


class Converter:
    def __init__(self, quotes: Mapping[str, Quote]) -> None:
        self._mid = {sym: q.mid for sym, q in quotes.items()}

    def rate(self, src: str, dst: str) -> float:
        """Units of `dst` per unit of `src`."""
        if src == dst:
            return 1.0
        direct = self._mid.get(src + dst)
        if direct:
            return direct
        inverse = self._mid.get(dst + src)
        if inverse:
            return 1.0 / inverse
        if "USD" not in (src, dst):
            return self.rate(src, "USD") * self.rate("USD", dst)
        raise ConversionUnavailableError(f"no rate for {src}->{dst}")


@dataclass
class FillResult:
    realized_quote: float  # quote currency
    realized: float  # account currency
    opened: bool  # opened or increased exposure
    closed_position: Position | None  # the position fully closed by this fill, if any


@dataclass
class Book:
    currency: str
    balance: float
    leverage: float
    positions: dict[str, Position] = field(default_factory=dict)
    commissions: float = 0.0
    realized: float = 0.0
    seq: int = 0

    def next_id(self, prefix: str) -> str:
        self.seq += 1
        return f"{prefix}-{self.seq:06d}"

    def apply_fill(
        self,
        symbol: str,
        side: Side,
        units: float,
        price: float,
        ts: datetime,
        commission: float,
        conv: Converter,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> FillResult:
        """Nets a fill into the pair's position and books realized P&L and costs."""
        quote_ccy = pair(symbol).quote
        signed = side.sign * units
        pos = self.positions.get(symbol)
        realized_q = 0.0
        opened = False
        closed: Position | None = None
        if pos is None or abs(pos.units) < EPS:
            self.positions[symbol] = Position(
                symbol, signed, price, ts, self.next_id("pos"), stop_loss, take_profit
            )
            opened = True
        elif pos.units * signed > 0:  # same direction: increase, average the price
            total = pos.units + signed
            pos.avg_price = (pos.avg_price * pos.units + price * signed) / total
            pos.units = total
            pos.stop_loss = stop_loss if stop_loss is not None else pos.stop_loss
            pos.take_profit = take_profit if take_profit is not None else pos.take_profit
            opened = True
        else:  # opposite direction: reduce, close or reverse
            closing = min(abs(signed), abs(pos.units))
            realized_q = closing * (price - pos.avg_price) * (1 if pos.units > 0 else -1)
            remaining = pos.units + signed
            if abs(remaining) < EPS:
                closed = pos
                del self.positions[symbol]
            elif remaining * pos.units > 0:
                pos.units = remaining
            else:  # reversal: the excess opens a new position at the fill price
                closed = pos
                self.positions[symbol] = Position(
                    symbol, remaining, price, ts, self.next_id("pos"), stop_loss, take_profit
                )
                opened = True
        realized = realized_q * conv.rate(quote_ccy, self.currency)
        self.realized += realized
        self.commissions += commission
        self.balance += realized - commission
        return FillResult(realized_q, realized, opened, closed)

    def unrealized(self, quotes: Mapping[str, Quote]) -> float:
        conv = Converter(quotes)
        total = 0.0
        for sym, pos in self.positions.items():
            q = quotes.get(sym)
            if q is None:
                continue
            mark = q.mark_for(pos.units)
            pnl_q = pos.units * (mark - pos.avg_price)
            total += pnl_q * conv.rate(pair(sym).quote, self.currency)
        return total

    def notional(self, symbol: str, units: float, conv: Converter) -> float:
        """|units| of the pair's base currency, in account currency."""
        return abs(units) * conv.rate(pair(symbol).base, self.currency)

    def gross_notional(self, quotes: Mapping[str, Quote]) -> float:
        conv = Converter(quotes)
        return sum(self.notional(s, p.units, conv) for s, p in self.positions.items())

    def margin_used(self, quotes: Mapping[str, Quote]) -> float:
        return self.gross_notional(quotes) / self.leverage

    def snapshot(self, quotes: Mapping[str, Quote], ts: datetime) -> AccountSnapshot:
        unreal = self.unrealized(quotes)
        equity = self.balance + unreal
        used = self.margin_used(quotes)
        return AccountSnapshot(self.currency, self.balance, unreal, equity, used, equity - used, ts)

    def currency_exposure(self, quotes: Mapping[str, Quote]) -> dict[str, float]:
        """Net exposure per currency, in account currency (long base / short quote)."""
        conv = Converter(quotes)
        out: dict[str, float] = {}
        for sym, pos in self.positions.items():
            p = pair(sym)
            base_value = pos.units * conv.rate(p.base, self.currency)
            out[p.base] = out.get(p.base, 0.0) + base_value
            out[p.quote] = out.get(p.quote, 0.0) - base_value
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "currency": self.currency,
            "balance": self.balance,
            "leverage": self.leverage,
            "positions": [p.to_dict() for p in self.positions.values()],
            "commissions": self.commissions,
            "realized": self.realized,
            "seq": self.seq,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Book":
        book = cls(str(d["currency"]), float(d["balance"]), float(d["leverage"]))
        book.positions = {
            p.pair: p for p in (Position.from_dict(x) for x in d.get("positions", []))
        }
        book.commissions = float(d.get("commissions", 0.0))
        book.realized = float(d.get("realized", 0.0))
        book.seq = int(d.get("seq", 0))
        return book
