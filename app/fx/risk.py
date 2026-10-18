"""Central risk manager (ADR-0038). Every order intent passes here; agents never send orders.

Definitions:
- **Trading day**: the FX day ending at 17:00 New York (`FxCalendar.trading_day`).
- **Day-start equity**: the first equity observed in the trading day (at or after rollover).
- **Daily P&L**: equity now - day-start equity. Equity = balance + unrealized P&L marked at the
  liquidation side of the quote, so it is net of spread, commissions and realized losses.
- **Entry**: an authorized intent that opens or increases exposure, including the opening leg
  of a reversal. It is RESERVED at authorization and CONSUMED when it fills (a partial fill
  consumes it); a rejection or cancellation without any fill releases it. Exits never count.
  A reservation still in flight at the rollover is carried over and counts on the day it
  fills.
- **Locks**: daily P&L <= -loss limit -> `daily_loss` lock; >= +profit target ->
  `profit_target` lock. A lock blocks new entries until the next trading day; it survives
  restarts (persisted state) and cannot be released by hand.
- **Lock precedence** (deterministic): `daily_loss` > `profit_target` > none. The loss limit is
  checked on every observation, independently of an earlier profit-target lock: if the day
  first reaches the target and then falls to the loss limit, the lock is upgraded to
  `daily_loss` (and its flatten policy applies). A `daily_loss` lock is never downgraded.
- **Kill switch**: blocks every new entry until released manually with confirmation. Exits are
  always allowed so risk can be reduced.
"""

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Any

from app.fx.accounting import EPS, Book, Converter
from app.fx.calendar import FxCalendar
from app.fx.environment import Environment
from app.fx.models import DataOrigin, Direction, Quote, Side, pair, utc
from app.fx.orders import AccountSnapshot, Position


@dataclass(frozen=True)
class RiskConfig:
    max_entries_per_day: int = 5
    daily_loss_limit_pct: float = 5.0
    daily_profit_target_pct: float = 15.0
    risk_per_trade_pct: float = 0.5  # equity lost if the stop is hit (before gaps/slippage)
    max_leverage: float = 30.0  # gross notional / equity
    max_open_positions: int = 4
    max_currency_exposure_pct: float = 600.0  # net exposure per currency / equity
    max_spread_pips: float = 3.0
    min_score: float = 0.55
    min_risk_reward: float = 1.2
    max_quote_age_seconds: float = 120.0
    no_entry_minutes_before_rollover: int = 15
    no_entry_minutes_after_rollover: int = 15
    min_units: float = 1_000.0
    unit_step: float = 1_000.0
    flatten_on_daily_loss: bool = True
    flatten_on_profit_target: bool = False

    def __post_init__(self) -> None:
        if self.max_entries_per_day < 0:
            raise ValueError("max_entries_per_day must be >= 0")
        for name in (
            "daily_loss_limit_pct",
            "daily_profit_target_pct",
            "risk_per_trade_pct",
            "max_leverage",
        ):
            if not getattr(self, name) > 0:
                raise ValueError(f"{name} must be > 0")
        if self.risk_per_trade_pct > self.daily_loss_limit_pct:
            raise ValueError("risk_per_trade_pct cannot exceed the daily loss limit")


class Lock(StrEnum):
    DAILY_LOSS = "daily_loss"
    PROFIT_TARGET = "profit_target"


LOCK_LABELS = {
    Lock.DAILY_LOSS: "Limite de perda diária atingido — novas entradas bloqueadas até amanhã",
    Lock.PROFIT_TARGET: "Objetivo de lucro diário atingido — novas entradas bloqueadas até amanhã",
}


@dataclass
class RiskState:
    trading_day: str | None = None  # ISO date
    day_start_equity: float | None = None
    entries_used: int = 0
    reservations: dict[str, str] = field(default_factory=dict)  # intent_id -> pair
    lock: str | None = None
    lock_at: str | None = None
    lock_pnl_pct: float | None = None
    kill_switch: bool = False
    kill_reason: str | None = None
    kill_at: str | None = None
    last_equity: float | None = None
    day_low_pnl_pct: float = 0.0
    day_high_pnl_pct: float = 0.0
    version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RiskState":
        if d.get("version") != 1:
            raise ValueError("unsupported risk state version")
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass(frozen=True)
class Check:
    code: str
    passed: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True)
class TradeRequest:
    """What the coordinator asks the risk manager to authorize."""

    pair: str
    direction: Direction  # LONG/SHORT to open; FLAT to close
    stop_loss: float | None
    take_profit: float | None
    score: float
    proposal_id: str


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    is_entry: bool
    side: Side | None
    units: float
    checks: tuple[Check, ...]
    summary: str

    @property
    def refusals(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]

    def to_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "is_entry": self.is_entry,
            "side": None if self.side is None else self.side.value,
            "units": self.units,
            "checks": [c.to_dict() for c in self.checks],
            "summary": self.summary,
        }


class RiskManager:
    def __init__(
        self,
        config: RiskConfig,
        state: RiskState | None = None,
        calendar: FxCalendar | None = None,
        environment: Environment = Environment.SIMULATION,
    ) -> None:
        self.config = config
        self.state = state or RiskState()
        self.calendar = calendar or FxCalendar()
        self.environment = environment

    # --- day accounting -------------------------------------------------------------------

    def observe(self, equity: float, ts: datetime) -> list[str]:
        """Feeds the current equity. Rolls the trading day and engages locks.
        Returns event codes: `day_rolled`, `lock:<kind>`."""
        events: list[str] = []
        s = self.state
        day = self.calendar.trading_day(ts).isoformat()
        if s.trading_day != day:
            s.trading_day = day
            s.day_start_equity = equity
            s.entries_used = 0  # in-flight reservations carry over: an entry counts when it fills
            s.lock = s.lock_at = s.lock_pnl_pct = None
            s.day_low_pnl_pct = s.day_high_pnl_pct = 0.0
            events.append("day_rolled")
        s.last_equity = equity
        pct = self.daily_pnl_pct()
        s.day_low_pnl_pct = min(s.day_low_pnl_pct, pct)
        s.day_high_pnl_pct = max(s.day_high_pnl_pct, pct)
        # precedence daily_loss > profit_target: the loss limit is evaluated on every
        # observation, whatever lock is already engaged; a daily_loss lock is never replaced
        if s.lock != Lock.DAILY_LOSS.value and pct <= -self.config.daily_loss_limit_pct:
            self._lock(Lock.DAILY_LOSS, pct, ts)
            events.append(f"lock:{Lock.DAILY_LOSS.value}")
        elif s.lock is None and pct >= self.config.daily_profit_target_pct:
            self._lock(Lock.PROFIT_TARGET, pct, ts)
            events.append(f"lock:{Lock.PROFIT_TARGET.value}")
        return events

    def _lock(self, kind: Lock, pct: float, ts: datetime) -> None:
        self.state.lock = kind.value
        self.state.lock_at = utc(ts).isoformat()
        self.state.lock_pnl_pct = round(pct, 4)

    def daily_pnl(self) -> float:
        s = self.state
        if s.day_start_equity is None or s.last_equity is None:
            return 0.0
        return s.last_equity - s.day_start_equity

    def daily_pnl_pct(self) -> float:
        start = self.state.day_start_equity
        return 0.0 if not start else 100.0 * self.daily_pnl() / start

    def should_flatten(self) -> bool:
        lock = self.state.lock
        return (lock == Lock.DAILY_LOSS.value and self.config.flatten_on_daily_loss) or (
            lock == Lock.PROFIT_TARGET.value and self.config.flatten_on_profit_target
        )

    @property
    def entries_available(self) -> int:
        s = self.state
        return max(0, self.config.max_entries_per_day - s.entries_used - len(s.reservations))

    # --- entries ---------------------------------------------------------------------------

    def reserve(self, intent_id: str, symbol: str) -> None:
        if self.entries_available <= 0:
            raise RuntimeError("no daily entry available to reserve")
        self.state.reservations[intent_id] = symbol

    def consume(self, intent_id: str) -> None:
        """The entry filled (fully or partially)."""
        if self.state.reservations.pop(intent_id, None) is not None:
            self.state.entries_used += 1

    def release(self, intent_id: str) -> None:
        """The entry did not fill (rejected / cancelled / refused at the venue)."""
        self.state.reservations.pop(intent_id, None)

    # --- kill switch -----------------------------------------------------------------------

    def engage_kill_switch(self, reason: str, ts: datetime) -> None:
        s = self.state
        s.kill_switch, s.kill_reason, s.kill_at = True, reason, utc(ts).isoformat()

    def release_kill_switch(self, confirmation: str) -> None:
        if confirmation != "LIBERTAR":
            raise ValueError("kill switch release requires the confirmation word LIBERTAR")
        s = self.state
        s.kill_switch, s.kill_reason, s.kill_at = False, None, None

    # --- evaluation -------------------------------------------------------------------------

    def evaluate(
        self,
        req: TradeRequest,
        *,
        quotes: Mapping[str, Quote],
        account: AccountSnapshot,
        positions: Mapping[str, Position],
        now: datetime,
        currency: str,
        venue_leverage: float = 30.0,
        ambiguous_pairs: Mapping[str, str] | None = None,
        entry_block: str | None = None,
    ) -> RiskDecision:
        """Authorizes or refuses one request. `venue_leverage` is the broker's margin leverage
        (the margin a position consumes); `config.max_leverage` caps total exposure.
        `ambiguous_pairs` maps a pair to the client order id of an order whose outcome is still
        unknown: no new entry on that pair until it is reconciled. `entry_block` blocks every
        new entry (e.g. start-up reconciliation still pending). Exits are never blocked by
        either."""
        cfg = self.config
        checks: list[Check] = []
        pos = positions.get(req.pair)
        cur_units = 0.0 if pos is None else pos.units

        def ok(code: str, cond: bool, detail: str) -> bool:
            checks.append(Check(code, cond, detail))
            return cond

        env_ok = ok(
            "environment",
            self.environment is not Environment.REAL,
            f"ambiente {self.environment.value}",
        )

        if req.direction is Direction.FLAT:  # an exit: always allowed when there is something
            has = ok("position_exists", abs(cur_units) > EPS, "posição aberta a fechar")
            approved = env_ok and has
            side = None if not has else (Side.SELL if cur_units > 0 else Side.BUY)
            return RiskDecision(
                approved,
                False,
                side if approved else None,
                abs(cur_units) if approved else 0.0,
                tuple(checks),
                "saída autorizada" if approved else "saída recusada",
            )

        side = req.direction.entry_side
        # ambiguous orders first: whatever else applies, the refusal names them
        pending = (ambiguous_pairs or {}).get(req.pair)
        ok(
            "ambiguous_order",
            pending is None,
            "sem ordens ambíguas no par"
            if pending is None
            else f"ordem {pending} com resultado desconhecido neste par (a reconciliar)",
        )
        ok(
            "reconciliation",
            entry_block is None,
            "sem reconciliação pendente" if entry_block is None else entry_block,
        )
        same_dir = cur_units * side.sign > EPS
        if same_dir:
            ok("no_pyramiding", False, "já existe posição na mesma direção")
            return self._refuse(checks)

        s = self.state
        ok(
            "kill_switch",
            not s.kill_switch,
            "kill switch inativo" if not s.kill_switch else f"kill switch ativo: {s.kill_reason}",
        )
        ok(
            "daily_lock",
            s.lock is None,
            "sem bloqueio diário" if s.lock is None else LOCK_LABELS[Lock(s.lock)],
        )
        ok(
            "entries_available",
            self.entries_available > 0,
            f"entradas usadas {s.entries_used}+{len(s.reservations)} reservadas de "
            f"{cfg.max_entries_per_day}",
        )
        open_ok = self.calendar.is_open(now)
        ok("market_open", open_ok, "mercado aberto" if open_ok else "mercado fechado")
        close_at = self.calendar.next_close(now)
        start_at = self.calendar.day_bounds(self.calendar.trading_day(now))[0]
        quiet = (close_at - now <= timedelta(minutes=cfg.no_entry_minutes_before_rollover)) or (
            now - start_at < timedelta(minutes=cfg.no_entry_minutes_after_rollover)
        )
        ok(
            "rollover_window",
            not quiet,
            "fora da janela de rollover das 17:00 NY"
            if not quiet
            else "janela de rollover (spreads alargados)",
        )

        q = quotes.get(req.pair)
        if q is None:
            ok("quote_available", False, "sem cotação")
            return self._refuse(checks)
        age = (utc(now) - q.ts).total_seconds()
        ok("quote_fresh", age <= cfg.max_quote_age_seconds, f"idade da cotação {age:.0f}s")
        origin_ok = self.environment is Environment.SIMULATION or q.origin is DataOrigin.LIVE
        ok("data_origin", origin_ok, f"origem dos dados {q.origin.value}")
        p = pair(req.pair)
        spread_pips = p.pips(q.spread)
        ok(
            "spread",
            spread_pips <= cfg.max_spread_pips,
            f"spread {spread_pips:.1f} pips (máx {cfg.max_spread_pips})",
        )
        ok(
            "score",
            req.score >= cfg.min_score,
            f"pontuação {req.score:.2f} (mín {cfg.min_score}) — não é probabilidade",
        )

        entry = q.price_for(side)
        stop = req.stop_loss
        if stop is None or not (stop < entry if side is Side.BUY else stop > entry):
            ok("stop_loss", False, "stop loss ausente ou do lado errado")
            return self._refuse(checks)
        ok("stop_loss", True, f"stop loss {stop}")
        dist = abs(entry - stop)
        if req.take_profit is not None:
            rr = abs(req.take_profit - entry) / dist
            ok(
                "risk_reward",
                rr >= cfg.min_risk_reward,
                f"risco/retorno {rr:.2f} (mín {cfg.min_risk_reward})",
            )

        conv = Converter(quotes)
        try:
            quote_to_acct = conv.rate(p.quote, currency)
            base_to_acct = conv.rate(p.base, currency)
        except LookupError:
            ok("conversion", False, "sem taxa de conversão para a moeda da conta")
            return self._refuse(checks)
        equity = account.equity
        risk_amount = equity * cfg.risk_per_trade_pct / 100.0
        units = risk_amount / (dist * quote_to_acct)
        units = (units // cfg.unit_step) * cfg.unit_step
        # leverage cap on the gross notional after the trade
        others = sum(
            abs(x.units) * conv.rate(pair(x.pair).base, currency)
            for k, x in positions.items()
            if k != req.pair
        )
        max_notional = max(0.0, cfg.max_leverage * equity - others)
        cap_units = (max_notional / base_to_acct // cfg.unit_step) * cfg.unit_step
        if units > cap_units:
            checks.append(
                Check(
                    "leverage_cap",
                    True,
                    f"unidades reduzidas de {units:.0f} para {cap_units:.0f} "
                    f"(alavancagem máx {cfg.max_leverage}x)",
                )
            )
            units = cap_units
        ok("min_size", units >= cfg.min_units, f"{units:.0f} unidades (mín {cfg.min_units:.0f})")
        new_pair = abs(cur_units) <= EPS
        n_open = sum(1 for x in positions.values() if abs(x.units) > EPS)
        if new_pair:
            ok(
                "max_positions",
                n_open < cfg.max_open_positions,
                f"{n_open} posições abertas (máx {cfg.max_open_positions})",
            )
        margin_needed = units * base_to_acct / venue_leverage
        ok(
            "free_margin",
            account.free_margin - margin_needed >= 0.5 * equity,
            f"margem livre após a ordem {account.free_margin - margin_needed:.2f}",
        )
        exposure = self._exposure_after(
            positions, quotes, currency, req.pair, cur_units + side.sign * units
        )
        worst = max(exposure.items(), key=lambda kv: abs(kv[1]), default=("-", 0.0))
        limit = cfg.max_currency_exposure_pct / 100.0 * equity
        ok(
            "currency_exposure",
            abs(worst[1]) <= limit,
            f"maior exposição {worst[0]} {worst[1]:.0f} (máx {limit:.0f})",
        )

        approved = all(c.passed for c in checks)
        if not approved:
            return self._refuse(checks)
        total_units = units + abs(cur_units)  # a reversal also closes the current position
        return RiskDecision(
            True,
            True,
            side,
            total_units,
            tuple(checks),
            f"entrada autorizada: {side.value} {units:.0f} {req.pair}"
            + (" (inversão)" if abs(cur_units) > EPS else ""),
        )

    @staticmethod
    def _exposure_after(
        positions: Mapping[str, Position],
        quotes: Mapping[str, Quote],
        currency: str,
        symbol: str,
        new_units: float,
    ) -> dict[str, float]:
        book = Book(currency, 0.0, 1.0)
        for k, x in positions.items():
            if k != symbol:
                book.positions[k] = x
        if abs(new_units) > EPS:
            book.positions[symbol] = Position(
                symbol, new_units, 0.0, datetime.now().astimezone(), "probe"
            )
        return book.currency_exposure(quotes)

    @staticmethod
    def _refuse(checks: list[Check]) -> RiskDecision:
        reasons = "; ".join(c.detail for c in checks if not c.passed)
        return RiskDecision(False, True, None, 0.0, tuple(checks), f"recusada: {reasons}")

    def status(self) -> dict[str, Any]:
        s, cfg = self.state, self.config
        return {
            "trading_day": s.trading_day,
            "day_start_equity": s.day_start_equity,
            "equity": s.last_equity,
            "daily_pnl": self.daily_pnl(),
            "daily_pnl_pct": self.daily_pnl_pct(),
            "entries_used": s.entries_used,
            "entries_reserved": len(s.reservations),
            "entries_max": cfg.max_entries_per_day,
            "entries_available": self.entries_available,
            "loss_limit_pct": cfg.daily_loss_limit_pct,
            "profit_target_pct": cfg.daily_profit_target_pct,
            "lock": s.lock,
            "lock_label": LOCK_LABELS[Lock(s.lock)] if s.lock else None,
            "lock_at": s.lock_at,
            "kill_switch": s.kill_switch,
            "kill_reason": s.kill_reason,
            "kill_at": s.kill_at,
            "day_low_pnl_pct": s.day_low_pnl_pct,
            "day_high_pnl_pct": s.day_high_pnl_pct,
        }


def trading_day_of(state: RiskState) -> date | None:
    return None if state.trading_day is None else date.fromisoformat(state.trading_day)
