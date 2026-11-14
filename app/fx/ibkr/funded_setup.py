"""One-time quote-currency buffers for the PAPER candle lab (IBKR paper account only).

The paper account holds EUR only and is not approved for currency leverage, so a protective
BUY (stop of a SELL EURxxx entry) could need more quote currency than the sale produced. This
module prepares, ONCE, a small buffer in each quote currency by converting 1 000 EUR with a
market SELL of EURUSD, EURGBP and EURJPY. It never enables leverage or touches real money.

`await ensure_funding(ib, root)` is safe to call on every cycle; it returns the state:

- `complete`: the 3 conversions filled and were verified (quote cash >= the equivalent of
  500 EUR each, CASH positions compatible with -1 000 per pair). `baseline_positions` holds
  ONLY those 3 preparation positions, so the pilot's position fetcher can subtract them;
- `waiting_market_open`: market closed or rollover window (nothing sent);
- `waiting_quotes`: no LIVE, recent, narrow quote for every pair (nothing sent);
- `waiting_positions`: an order is still unconfirmed, or fills are not yet visible in cash
  and positions; call again (never resent);
- `stopped`: the file `root/STOP` exists. It is checked at the start, before each
  write-ahead and again immediately before each `placeOrder`; nothing new is sent while
  it exists (an intention vetoed before sending is withdrawn, so nothing is left
  ambiguous). Remove the file to resume.

Anything else raises `FundingError` and records `paused` in `funding.json`; a paused setup
stays paused until the owner reviews it (delete `funding.json` only after checking TWS):
- a live port, or a session that is not exactly ONE paper (DU...) account;
- EUR cash below 10 000;
- positions already open in these pairs before the setup;
- an order rejected, partially filled, or not found after 3 lookups;
- fills that never show in cash or positions;
- a Gateway in Read-Only API mode.

Persistence: `funding.json` (written atomically, the intention BEFORE each submit) and a
journal of its own (`funding.sqlite`), separate from the agents' trades.
"""

import json
import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.fx.calendar import FxCalendar
from app.fx.errors import EnvironmentMismatchError
from app.fx.ibkr.execution import IbkrPaperExecutor, SendBlockedError
from app.fx.ibkr.paper import PAPER_PREFIXES, mask
from app.fx.lifecycle import OrderState
from app.fx.models import Quote, Side, pair
from app.fx.orders import ExecutionError
from app.fx.store import FxStore

SCHEMA = "fx-paper-funding/1"
FUND_PAIRS = ("EURUSD", "EURGBP", "EURJPY")
FUND_EUR = 1_000.0
MIN_EUR_CASH = 10_000.0
MIN_QUOTE_EUR = 500.0  # verified quote cash per currency, in EUR
POSITION_TOLERANCE = 1.0
MAX_SPREAD_PIPS = 3.0
ROLLOVER_GUARD = timedelta(minutes=15)
MAX_LOOKUPS = 3  # an intention not found at IBKR this many times pauses the setup
MAX_VERIFY_CALLS = 10  # fills not visible in cash/positions after this many calls: pause
LIVE_PORTS = frozenset({4001, 7496})
CASH_TAG = "$LEDGER-CashBalance"


class FundingError(RuntimeError):
    """The setup is paused: the owner must review before anything else is sent."""


def coid_for(symbol: str) -> str:
    return f"LAB-FUND-{symbol}-1"


class _Setup:
    def __init__(
        self,
        ib: Any,
        root: Path,
        executor: IbkrPaperExecutor,
        port: int | None,
        clock: Callable[[], datetime],
        calendar: FxCalendar,
    ) -> None:
        self.ib, self.root, self.ex = ib, root, executor
        self.port, self.clock, self.calendar = port, clock, calendar
        self.path = root / "funding.json"
        self.stop_file = root / "STOP"
        self.store = FxStore(root / "funding.sqlite")
        self.state: dict[str, Any] = (
            json.loads(self.path.read_text(encoding="utf-8"))
            if self.path.exists()
            else {"schema": SCHEMA, "state": "new", "orders": {}, "verify_calls": 0}
        )

    # --- persistence ----------------------------------------------------------------------

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2, default=str), encoding="utf-8")
        tmp.replace(self.path)

    def journal(self, kind: str, /, **payload: Any) -> None:
        self.store.append(f"funding_{kind}", {"label": "preparação de caixa paper", **payload})

    def result(self, state: str, **extra: Any) -> dict[str, Any]:
        self.state["state"] = state
        self.state["updated_at"] = self.clock().isoformat()
        self.state.update(extra)
        self.save()
        return dict(self.state)

    def pause(self, reason: str) -> FundingError:
        self.state["state"] = "paused"
        self.state["reason"] = reason
        self.state["updated_at"] = self.clock().isoformat()
        self.save()
        self.journal("paused", reason=reason)
        return FundingError(reason)

    # --- gates ---------------------------------------------------------------------------

    def single_paper_account(self) -> str:
        """Exactly one managed account, and it is paper: never pick a DU among others."""
        ids = [str(a).strip() for a in self.ib.managedAccounts() if a and str(a).strip()]
        if len(ids) != 1 or not ids[0].startswith(PAPER_PREFIXES):
            shown = ", ".join(mask(a) for a in ids) or "nenhuma"
            raise self.pause(f"a sessão tem de ter exatamente UMA conta paper (DU…): {shown}")
        self.ex.verify_paper()
        account: str = ids[0]
        return account

    def send_guard(self) -> None:
        """Runs immediately before each placeOrder (after all awaits)."""
        if self.stop_file.exists():
            raise SendBlockedError(f"{self.stop_file} presente")
        self.single_paper_account()

    # --- broker reads ---------------------------------------------------------------------

    def cash(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for v in self.ib.accountValues():
            if v.tag == CASH_TAG and v.currency not in ("", "BASE"):
                try:
                    amount = float(v.value)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(amount):
                    out[v.currency] = out.get(v.currency, 0.0) + amount
        return out

    async def positions(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for p in await self.ib.reqPositionsAsync():
            c = p.contract
            if getattr(c, "secType", "") == "CASH":
                sym = f"{c.symbol}{c.currency}"
                out[sym] = out.get(sym, 0.0) + float(p.position)
        return out

    # --- flow -----------------------------------------------------------------------------

    async def run(self) -> dict[str, Any]:
        if self.state["state"] == "paused":
            raise FundingError(f"preparação pausada: {self.state.get('reason')}")
        if self.port is None or self.port in LIVE_PORTS:
            raise self.pause(f"porta {self.port} não é uma porta paper conhecida")
        try:
            self.single_paper_account()
        except EnvironmentMismatchError as e:
            raise self.pause(str(e)) from e
        if self.state["state"] == "complete":
            return dict(self.state)
        if self.stop_file.exists():
            return self.result("stopped")
        orders: dict[str, Any] = self.state["orders"]
        if not orders:  # nothing sent yet: every precondition, then the first submit
            now = self.clock()
            if not self.calendar.is_open(now) or self._rollover(now):
                return self.result("waiting_market_open")
            before = await self.positions()
            busy = {s: u for s, u in before.items() if s in FUND_PAIRS and abs(u) > 1e-9}
            if busy:
                raise self.pause(f"posições prévias nos pares de preparação: {busy}")
            cash = self.cash()
            if cash.get("EUR", 0.0) < MIN_EUR_CASH:
                raise self.pause(f"EUR disponível {cash.get('EUR', 0.0):.2f} < {MIN_EUR_CASH:.0f}")
            quotes = await self._quotes()
            if quotes is None:
                return self.result("waiting_quotes")
            self.state["cash_before"] = cash
            self.state["positions_before"] = before
        pending = await self._reconcile(orders)
        if pending is not None:
            return pending
        missing = [s for s in FUND_PAIRS if s not in orders]
        if missing:
            now = self.clock()
            if not self.calendar.is_open(now) or self._rollover(now):
                return self.result("waiting_market_open")
            quotes = await self._quotes()
            if quotes is None:
                return self.result("waiting_quotes")
            for sym in missing:
                outcome = await self._submit(sym, orders)
                if outcome is not None:
                    return outcome
        return await self._verify(orders)

    def _rollover(self, now: datetime) -> bool:
        start = self.calendar.day_bounds(self.calendar.trading_day(now))[0]
        return (self.calendar.next_close(now) - now <= ROLLOVER_GUARD) or (
            now - start < ROLLOVER_GUARD
        )

    async def _quotes(self) -> dict[str, Quote] | None:
        quotes = await self.ex.live_quotes(list(FUND_PAIRS))
        for sym in FUND_PAIRS:
            q = quotes.get(sym)
            if q is None or pair(sym).pips(q.spread) > MAX_SPREAD_PIPS:
                return None
        return quotes

    async def _submit(self, sym: str, orders: dict[str, Any]) -> dict[str, Any] | None:
        coid = coid_for(sym)
        if self.stop_file.exists():  # re-checked after the quote/position awaits
            return self.result("stopped")
        self.single_paper_account()
        orders[sym] = {
            "coid": coid,
            "side": "SELL",
            "units": FUND_EUR,
            "status": "submitting",
            "lookups": 0,
            "at": self.clock().isoformat(),
        }
        self.save()  # write-ahead: a crash after this is reconciled, never resent
        self.journal("order_sent", symbol=sym, coid=coid, units=FUND_EUR)
        try:
            st = await self.ex.place_market(sym, Side.SELL, FUND_EUR, coid, self.send_guard)
        except SendBlockedError as e:
            orders.pop(sym, None)  # vetoed before placeOrder: nothing was sent
            self.journal("order_withdrawn", symbol=sym, coid=coid, reason=str(e))
            return self.result("stopped")
        except FundingError:  # the guard paused (account set changed): nothing was sent
            orders.pop(sym, None)
            self.save()
            self.journal("order_withdrawn", symbol=sym, coid=coid, reason="conta(s) inválida(s)")
            raise
        except ExecutionError as e:
            if e.ambiguous:
                orders[sym]["status"] = "unknown"
                self.journal("order_ambiguous", symbol=sym, coid=coid)
                return self.result("waiting_positions")
            raise self.pause(f"conversão {coid} recusada: {e}") from e
        return self._record(sym, st)

    def _record(self, sym: str, st: Any) -> dict[str, Any] | None:
        info = self.state["orders"][sym]
        if st.state is OrderState.FILLED:
            info.update(
                status="filled",
                filled=st.filled_units,
                price=st.avg_price,
                fills=[f.fill_id for f in st.fills],
                commission_eur=sum(f.commission for f in st.fills),
            )
            self.save()
            self.journal("order_filled", symbol=sym, coid=info["coid"], price=st.avg_price)
            return None
        if st.filled_units > 0:
            raise self.pause(
                f"conversão {info['coid']} parcial ({st.filled_units:.0f}): rever na TWS"
            )
        if st.state in (OrderState.REJECTED, OrderState.CANCELLED):
            raise self.pause(f"conversão {info['coid']} recusada: {st.reason or st.state.value}")
        info["status"] = "working"
        info["working_seen"] = info.get("working_seen", 0) + 1
        self.save()
        if info["working_seen"] >= MAX_VERIFY_CALLS:  # a market order should not linger
            raise self.pause(f"conversão {info['coid']} continua pendente: rever na TWS")
        return self.result("waiting_positions")

    async def _reconcile(self, orders: dict[str, Any]) -> dict[str, Any] | None:
        for sym, info in orders.items():
            if info["status"] == "filled":
                continue
            st = await self.ex.find(info["coid"])
            if st is None or st.state is OrderState.UNKNOWN:
                info["lookups"] = info.get("lookups", 0) + 1
                self.save()
                if info["lookups"] >= MAX_LOOKUPS:
                    raise self.pause(
                        f"conversão {info['coid']} sem confirmação do IBKR: rever na TWS"
                    )
                return self.result("waiting_positions")
            outcome = self._record(sym, st)
            if outcome is not None:
                return outcome
        return None

    async def _verify(self, orders: dict[str, Any]) -> dict[str, Any]:
        quotes = await self.ex.live_quotes(list(FUND_PAIRS))
        cash = self.cash()
        positions = await self.positions()
        problems: list[str] = []
        quote_cash_eur: dict[str, float] = {}
        for sym in FUND_PAIRS:
            ccy = sym[3:]
            q = quotes.get(sym)
            price = q.mid if q is not None else float(orders[sym].get("price") or 0.0)
            eur = cash.get(ccy, 0.0) / price if price > 0 else 0.0
            quote_cash_eur[ccy] = round(eur, 2)
            if eur < MIN_QUOTE_EUR:
                problems.append(f"caixa {ccy} ≈ {eur:.0f} EUR < {MIN_QUOTE_EUR:.0f}")
            got = positions.get(sym, 0.0)
            if abs(got - (-FUND_EUR)) > POSITION_TOLERANCE:
                problems.append(f"posição {sym} {got:+.0f} (esperado -{FUND_EUR:.0f})")
        if problems:
            self.state["verify_calls"] = self.state.get("verify_calls", 0) + 1
            self.journal("verify_waiting", problems=problems)
            if self.state["verify_calls"] >= MAX_VERIFY_CALLS:
                raise self.pause(
                    "conversões preenchidas mas caixa/posições não confirmados ("
                    + "; ".join(problems)
                    + "): estratégias continuam DESARMADAS"
                )
            return self.result("waiting_positions", problems=problems)
        baseline = {sym: -FUND_EUR for sym in FUND_PAIRS}
        self.journal("complete", baseline_positions=baseline, quote_cash_eur=quote_cash_eur)
        self.state.pop("problems", None)
        return self.result(
            "complete",
            baseline_positions=baseline,
            quote_cash_eur=quote_cash_eur,
            completed_at=self.clock().isoformat(),
        )


async def ensure_funding(
    ib: Any,
    root: Path,
    *,
    executor: IbkrPaperExecutor | None = None,
    port: int | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    calendar: FxCalendar | None = None,
) -> dict[str, Any]:
    """Prepares (once) and verifies the quote-currency buffers. See the module docstring."""
    from app.fx.ibkr.execution import ReadOnlyApiError

    root.mkdir(parents=True, exist_ok=True)
    if port is None:
        port = getattr(getattr(ib, "client", None), "port", None)
    ex = executor or IbkrPaperExecutor(ib, currency="EUR", clock=clock)
    setup = _Setup(ib, root, ex, port, clock, calendar or FxCalendar())
    try:
        return await setup.run()
    except ReadOnlyApiError as e:
        raise setup.pause(str(e)) from e
    finally:
        setup.store.close()


def funding_status(root: Path) -> dict[str, Any] | None:
    """The recorded setup state (no broker access), or None before the first call."""
    path = root / "funding.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
