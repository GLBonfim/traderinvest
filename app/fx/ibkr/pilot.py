"""`python -m app.fx.ibkr.pilot` — PILOTO EXPERIMENTAL on the IBKR PAPER account (phase B).

What it is: a prospective paper observation of trained candidates (`app.fx.agents.trained`),
NOT an approved strategy and never real money. Selection file:
`data/fx/training/<run>/selected.json` (schema fx-training-run/1, statistically_approved=False).

Safety (each one is tested in tests/unit/fx/test_pilot.py):
- dry-run by default: it computes and journals what it WOULD do; sending orders requires
  `--arm --confirm ARMAR-PAPER` on every start (arming is never persisted). In dry-run the API
  connection itself is read-only;
- before every order the session's accounts must all be paper (DU...); live API ports
  4001/7496 are refused outright;
- decisions use LIVE quotes only (market data type 1) with a real, recent ticker timestamp;
- isolated from the simulator and from the paper balance: own journal/state in
  `data/fx/pilot/`, virtual capital (default 10 000 EUR) for sizing and limits, and it only
  manages positions it opened; a pair with any other position is skipped;
- full central risk rules (environment DEMO: LIVE data required, 5 entries/day, daily locks,
  drawdown breaker, spread, rollover window, stop/target, R/R);
- write-ahead journal before each order, deterministic client order id (`orderRef`), lookup
  before sending, no blind resend: an unanswered order is reconciled up to 3 cycles, then the
  kill switch is engaged for manual review;
- entries are protected at IBKR (stop + target, OCA); exits: stop/target, time stop
  (max_hold bars) and flat before the weekend close;
- disarm: the file `data/fx/pilot/STOP`, the kill switch, Ctrl+C, a Gateway in Read-Only API
  mode (the owner must change that setting manually: never by automation);
- retirement: closed paper trades (deduplicated by execution id) feed
  `retirement_decision`; a retired candidate is replaced by the next eligible one or NO TRADE.
"""

import argparse
import asyncio
import contextlib
import json
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from app.fx.accounting import Converter
from app.fx.agents.trained import BAR_HOURS, LinearModel, load_models, model_signals
from app.fx.calendar import FxCalendar
from app.fx.environment import Environment
from app.fx.errors import EnvironmentMismatchError
from app.fx.ibkr.execution import IbkrPaperExecutor, ReadOnlyApiError
from app.fx.ibkr.paper import mask
from app.fx.lifecycle import OrderState
from app.fx.models import Direction, Quote, Side, pair
from app.fx.orders import AccountSnapshot, ExecutionError, OrderRequest, OrderStatus, Position
from app.fx.research.history import resample, with_spread_floor
from app.fx.risk import RiskConfig, RiskManager, RiskState, TradeRequest
from app.fx.store import FxStore

LABEL = "PILOTO EXPERIMENTAL — conta paper IBKR (não aprovado, sem dinheiro real)"
CONFIRM_WORD = "ARMAR-PAPER"
LIVE_PORTS = frozenset({4001, 7496})
DEFAULT_SELECTION = Path("data/fx/training/run-20261018/selected.json")
DEFAULT_DIR = Path("data/fx/pilot")


def default_selection() -> Path:
    """The executable ships the selected registry in `pilot/`; from source, the repo run."""
    bundle = getattr(sys, "_MEIPASS", None)
    return Path(bundle) / "pilot" / "selected.json" if bundle else DEFAULT_SELECTION


def default_dir() -> Path:
    """Pilot state: next to the app's data (frozen) or `data/fx/pilot` (source)."""
    if getattr(sys, "frozen", False):
        from app.fx.config import data_dir

        return data_dir() / "pilot"
    return DEFAULT_DIR


DEFAULT_SYMBOLS = (
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "AUDUSD",
    "USDCHF",
    "USDCAD",
    "NZDUSD",
    "EURGBP",
    "EURJPY",
)
MAX_RECONCILE_ATTEMPTS = 3
SIGNAL_MAX_DELAY = timedelta(minutes=20)  # act on a closed bar only shortly after its close
MAX_BAR_SPREAD_PIPS = 3.0  # same filter as the tournament
WEEKEND_MARGIN = timedelta(minutes=15)
STATE_VERSION = 1

BarsFetcher = Callable[[str, str], Awaitable[pd.DataFrame]]
PositionsFetcher = Callable[[], Awaitable[dict[str, float]]]


class PilotStopError(RuntimeError):
    """The pilot must stop (disarmed); the message says why."""


@dataclass
class PilotState:
    active_ids: list[str]
    retired_ids: list[str] = field(default_factory=list)
    returns: dict[str, list[float]] = field(default_factory=dict)
    open: dict[str, dict[str, Any]] = field(default_factory=dict)  # entry id -> trade
    pending: dict[str, dict[str, Any]] = field(default_factory=dict)  # coid -> order info
    closed_entries: list[str] = field(default_factory=list)
    seen_fills: list[str] = field(default_factory=list)
    last_signal_bar: dict[str, str] = field(default_factory=dict)  # "model|pair" -> bar iso
    realized: float = 0.0
    version: int = STATE_VERSION

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PilotState":
        if d.get("version") != STATE_VERSION:
            raise ValueError("unsupported pilot state version")
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass(frozen=True)
class PilotConfig:
    symbols: tuple[str, ...] = DEFAULT_SYMBOLS
    capital: float = 10_000.0
    currency: str = "EUR"
    risk: RiskConfig = field(default_factory=RiskConfig)


def closed_model_bars(
    m15: pd.DataFrame, symbol: str, timeframe: str, now: datetime
) -> pd.DataFrame:
    """M15 bid/ask -> spread floor -> model timeframe (as in the tournament), CLOSED bars only."""
    hours = BAR_HOURS[timeframe]
    bars = resample(with_spread_floor(m15, symbol), f"{hours}h")
    closes = pd.DatetimeIndex(bars.index) + pd.Timedelta(hours=hours)
    return bars[closes <= pd.Timestamp(now)]


def mid_of(bars: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(
        {k: (bars[f"bid_{k}"] + bars[f"ask_{k}"]) / 2 for k in ("open", "high", "low", "close")},
        index=bars.index,
    )


class PaperPilot:
    def __init__(
        self,
        executor: IbkrPaperExecutor,
        store: FxStore,
        registry: dict[str, Any],
        bars: BarsFetcher,
        positions: PositionsFetcher,
        config: PilotConfig | None = None,
        armed: bool = False,
        stop_file: Path | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        calendar: FxCalendar | None = None,
        say: Callable[[str], None] = print,
    ) -> None:
        if registry.get("statistically_approved") is not False:
            raise ValueError("the pilot only runs explicitly experimental registries")
        self.ex, self.store, self.registry = executor, store, registry
        self.fetch_bars, self.fetch_positions = bars, positions
        self.config = config or PilotConfig()
        self.armed = armed
        self.stop_file = stop_file
        self.clock = clock
        self.calendar = calendar or FxCalendar()
        self.say = say
        self.models: dict[str, LinearModel] = {
            m["model_id"]: LinearModel.from_dict(m) for m in registry["models"]
        }
        saved = store.get_state("pilot")
        if saved is not None:
            self.state = PilotState.from_dict(saved)
        else:
            self.state = PilotState(active_ids=list(registry["active_model_ids"]))
        risk_saved = store.get_state("risk")
        self.risk = RiskManager(
            self.config.risk,
            RiskState.from_dict(risk_saved) if risk_saved else None,
            self.calendar,
            Environment.DEMO,
        )

    # --- persistence ----------------------------------------------------------------------

    def save(self) -> None:
        self.store.put_states({"pilot": self.state.to_dict(), "risk": self.risk.state.to_dict()})

    def journal(self, event: str, /, **payload: Any) -> None:
        self.store.append(f"pilot_{event}", {"label": LABEL, "armed": self.armed, **payload})

    def kill(self, reason: str) -> None:
        if not self.risk.state.kill_switch:
            self.risk.engage_kill_switch(reason, self.clock())
            self.journal("kill_switch", reason=reason)
            self.say(f"KILL SWITCH: {reason}")

    # --- account --------------------------------------------------------------------------

    def _positions(self) -> dict[str, Position]:
        out: dict[str, Position] = {}
        for eid, t in self.state.open.items():
            out[t["symbol"]] = Position(
                t["symbol"],
                t["units"],
                t["entry_price"],
                datetime.fromisoformat(t["opened_at"]),
                eid,
                t["stop"],
                t["target"],
            )
        return out

    def account(self, quotes: dict[str, Quote]) -> AccountSnapshot:
        conv = Converter(quotes)
        cur = self.config.currency
        unrealized = margin = 0.0
        for t in self.state.open.values():
            p = pair(t["symbol"])
            q = quotes.get(t["symbol"])
            with contextlib.suppress(LookupError):
                if q is not None:
                    unrealized += (
                        (q.mark_for(t["units"]) - t["entry_price"])
                        * t["units"]
                        * (conv.rate(p.quote, cur))
                    )
                margin += abs(t["units"]) * conv.rate(p.base, cur) / 30.0
        balance = self.config.capital + self.state.realized
        equity = balance + unrealized
        return AccountSnapshot(
            cur, balance, unrealized, equity, margin, equity - margin, self.clock()
        )

    # --- cycle ----------------------------------------------------------------------------

    async def cycle(self) -> None:
        """One pass: reconcile, protect/exit, then at most one new entry. Never raises for a
        routine refusal; raises PilotStopError when the pilot must stop."""
        now = self.clock()
        if self.stop_file is not None and self.stop_file.exists():
            self.armed = False
            raise PilotStopError(f"ficheiro {self.stop_file} presente: piloto desarmado")
        try:
            self.ex.verify_paper()
        except EnvironmentMismatchError as e:
            self.armed = False
            self.kill(str(e))
            self.save()
            raise PilotStopError(str(e)) from e
        try:
            await self._reconcile_pending()
            await self._check_open(now)
            if not self.calendar.is_open(now):
                self.say("mercado fechado: nenhuma decisão")
                return
            quotes = await self.ex.live_quotes(list(self.config.symbols))
            self.risk.observe(self.account(quotes).equity, now)
            await self._exits(now)
            await self._entry(now, quotes)
        except ReadOnlyApiError as e:
            self.armed = False
            self.journal("stop", reason=str(e))
            raise PilotStopError(str(e)) from e
        finally:
            self.save()

    # --- reconciliation -------------------------------------------------------------------

    async def _reconcile_pending(self) -> None:
        for coid, info in list(self.state.pending.items()):
            if info["kind"] == "exit" and info["entry_id"] not in self.state.open:
                self.state.pending.pop(coid, None)  # already closed (stop/target)
                continue
            st = await self.ex.find(coid)
            if st is None or st.state is OrderState.UNKNOWN:
                info["attempts"] = info.get("attempts", 0) + 1
                if info["attempts"] >= MAX_RECONCILE_ATTEMPTS:
                    self.kill(f"ordem {coid} sem confirmação do IBKR: rever na TWS e libertar")
                continue
            await self._apply_outcome(coid, info, st)

    async def _apply_outcome(self, coid: str, info: dict[str, Any], st: OrderStatus) -> None:
        if info["kind"] == "entry":
            if st.filled_units > 0 and st.state not in (
                OrderState.FILLED,
                OrderState.CANCELLED,
                OrderState.REJECTED,
            ):
                st = await self._settle_partial(coid, info, st)  # final total, or alarm
            if st.state is OrderState.FILLED:
                self.state.pending.pop(coid, None)
                self.risk.consume(coid)
                self._opened(coid, info, st)
                await self._verify_protection(coid)
            elif st.state in (OrderState.REJECTED, OrderState.CANCELLED) and st.filled_units == 0:
                self.state.pending.pop(coid, None)
                self.risk.release(coid)
                self.journal("order_rejected", coid=coid, reason=st.reason)
            elif st.filled_units > 0:  # final partial (remainder cancelled): stop size != position
                self.state.pending.pop(coid, None)
                self.risk.consume(coid)
                self._opened(coid, info, st)
                self.armed = False
                msg = (
                    f"entrada {coid} parcial ({st.filled_units:.0f} de {st.units:.0f}): "
                    "proteção não corresponde à exposição, rever na TWS"
                )
                self.kill(msg)
                raise PilotStopError(f"alarme: {msg}")
            # ACCEPTED without fills: still working, look again next cycle
        else:  # exit
            if st.client_order_id != coid:  # the stop/target filled while the exit was prepared
                self.state.pending.pop(coid, None)
                kind = "stop" if st.client_order_id.endswith(":sl") else "alvo"
                self._closed(info["entry_id"], st, kind)
            elif st.state is OrderState.FILLED:
                self.state.pending.pop(coid, None)
                self._closed(info["entry_id"], st, info.get("reason", "saída"))
            elif st.state in (OrderState.REJECTED, OrderState.CANCELLED):
                self.state.pending.pop(coid, None)
                self.kill(f"saída {coid} recusada pelo IBKR: posição sem proteção, rever na TWS")

    async def _settle_partial(
        self, coid: str, info: dict[str, Any], st: OrderStatus
    ) -> OrderStatus:
        """A parent still working with a partial fill may keep filling: cancel the remainder
        and wait for IBKR's final total. If that is not confirmed (or the pilot is not armed
        and may not cancel), the intention stays pending with the known exposure, and the
        pilot pauses with an alarm. Never a blind flatten."""
        info["filled_known"] = st.filled_units
        if self.armed:
            try:
                return await self.ex.cancel_entry_remainder(coid)
            except ExecutionError as e:
                detail = str(e)
        else:
            detail = "piloto não armado: o restante não pode ser cancelado"
        self.armed = False
        msg = (
            f"entrada {coid} parcial ({st.filled_units:.0f} de {st.units:.0f}) com total final "
            f"incerto ({detail}): rever na TWS"
        )
        self.kill(msg)
        self.save()
        raise PilotStopError(f"alarme: {msg}")

    async def _verify_protection(self, coid: str, quiet: bool = False) -> None:
        """Stops the pilot with an alarm when an open entry is not protected at IBKR."""
        ok, detail = await self.ex.protection_ok(coid)
        if ok:
            if not quiet:
                self.journal("protected", coid=coid, detail=detail)
            return
        self.armed = False
        self.kill(f"entrada {coid} SEM proteção confirmada: {detail}; rever na TWS")
        raise PilotStopError(f"alarme: posição {coid} sem stop/alvo confirmados ({detail})")

    def _opened(self, coid: str, info: dict[str, Any], st: OrderStatus) -> None:
        sign = 1 if info["side"] == Side.BUY.value else -1
        entry = float(st.avg_price or info["ref_price"])
        units = sign * float(st.filled_units)
        trade = {
            **info,
            "units": units,
            "entry_price": entry,
            "opened_at": self.clock().isoformat(),
            "risk_acct": abs(units) * abs(entry - info["stop"]) * info["quote_to_acct"],
            "entry_commission": sum(f.commission for f in st.fills),
        }
        self.state.open[coid] = trade
        self.journal("order_filled", coid=coid, symbol=info["symbol"], units=units, price=entry)
        self.say(f"entrada preenchida {info['symbol']} {units:+.0f} @ {entry}")

    def _closed(self, entry_id: str, st: OrderStatus, reason: str) -> None:
        fill_ids = [f.fill_id for f in st.fills]
        if entry_id in self.state.closed_entries or any(
            f in self.state.seen_fills for f in fill_ids
        ):
            return  # deduplicated: this close was already counted
        t = self.state.open.pop(entry_id, None)
        if t is None:
            return
        exit_px = float(st.avg_price or 0.0)
        commissions = t.get("entry_commission", 0.0) + sum(f.commission for f in st.fills)
        pnl = (exit_px - t["entry_price"]) * t["units"] * t["quote_to_acct"] - commissions
        net_r = pnl / t["risk_acct"] if t["risk_acct"] > 0 else 0.0
        self.state.realized += pnl
        self.state.closed_entries.append(entry_id)
        self.state.seen_fills.extend(fill_ids)
        model_id = t["model_id"]
        self.state.returns.setdefault(model_id, []).append(net_r)
        self.journal(
            "close",
            entry_id=entry_id,
            model_id=model_id,
            symbol=t["symbol"],
            reason=reason,
            exit_price=exit_px,
            pnl_estimate=round(pnl, 2),  # commissions converted at the latest LIVE mid
            net_r_estimate=round(net_r, 4),
            fills=fill_ids,
        )
        self.say(
            f"fecho {t['symbol']} ({reason}): {pnl:+.2f} {self.config.currency}, {net_r:+.2f} R"
        )
        self._retirement(model_id)

    def _retirement(self, model_id: str) -> None:
        from app.fx.agents.trained import retirement_decision

        d = retirement_decision(
            self.registry,
            model_id,
            self.state.returns[model_id],
            self.state.retired_ids,
            self.state.active_ids,  # the replacement must be a FREE candidate
        )
        if not d["retire"] or model_id in self.state.retired_ids:
            return
        self.state.retired_ids.append(model_id)
        ids = self.state.active_ids
        repl = d["replacement_id"]
        if model_id in ids:
            i = ids.index(model_id)
            if repl is not None and repl not in ids:
                ids[i] = repl
            else:
                ids.pop(i)
        self.journal("retire", model_id=model_id, replacement=repl, decision=d)
        self.say(f"candidato {model_id} retirado ({d['reason']}); substituto: {repl or 'NO TRADE'}")

    async def _check_open(self, now: datetime) -> None:
        """Stops/targets filled at IBKR, protection still working, positions changed outside."""
        for eid in list(self.state.open):
            st = await self.ex.protective_fill(eid)
            if st is None:
                continue
            if st.filled_units + 1e-9 < abs(self.state.open[eid]["units"]):
                self.kill(f"proteção de {eid} preenchida parcialmente: rever na TWS")
                continue
            self._closed(eid, st, "stop" if st.client_order_id.endswith(":sl") else "alvo")
        for eid in list(self.state.open):
            if f"{eid}:x" not in self.state.pending:  # an exit cancels protection on purpose
                await self._verify_protection(eid, quiet=True)
        broker = await self.fetch_positions()
        for t in list(self.state.open.values()):
            held = broker.get(t["symbol"], 0.0)
            age = now - datetime.fromisoformat(t["opened_at"])
            if age > timedelta(minutes=2) and abs(held - t["units"]) > 1e-6:
                self.kill(
                    f"posição {t['symbol']} na conta ({held:+.0f}) difere da do piloto "
                    f"({t['units']:+.0f}): alterada fora do piloto, rever na TWS"
                )

    # --- exits ----------------------------------------------------------------------------

    def _before_weekend(self, now: datetime) -> bool:
        day = self.calendar.trading_day(now)
        return day.weekday() == 4 and self.calendar.next_close(now) - now <= WEEKEND_MARGIN

    async def _exits(self, now: datetime) -> None:
        weekend = self._before_weekend(now)
        for eid, t in list(self.state.open.items()):
            due = datetime.fromisoformat(t["time_stop_at"])
            reason = "fim de semana" if weekend else ("tempo" if now >= due else None)
            if reason is None:
                continue
            coid = f"{eid}:x"
            if coid in self.state.pending:
                continue
            if not self.armed:
                self.journal("dry_run_exit", entry_id=eid, reason=reason)
                continue
            info = {"kind": "exit", "entry_id": eid, "symbol": t["symbol"], "reason": reason}
            self.state.pending[coid] = info
            self.journal("order_sent", coid=coid, **info)
            self.save()  # write-ahead
            try:
                st = await self.ex.close_position(t["symbol"], t["units"], eid, coid)
            except ReadOnlyApiError:
                self.state.pending.pop(coid, None)
                raise
            except ExecutionError as e:
                if not e.ambiguous:
                    self.state.pending.pop(coid, None)
                else:
                    self.journal("order_ambiguous", coid=coid)
                self.kill(f"saída {coid} incerta ou falhada ({e}): pausa, rever na TWS")
                continue
            await self._apply_outcome(coid, info, st)

    # --- entries --------------------------------------------------------------------------

    async def _signals(self, now: datetime) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        models = [self.models[i] for i in self.state.active_ids if i in self.models]
        for sym in self.config.symbols:
            proposals: list[dict[str, Any]] = []
            for m in models:
                key = f"{m.model_id}|{sym}"
                hours = BAR_HOURS[m.timeframe]
                last_close = pd.Timestamp(now).floor(f"{hours}h")
                bar_open = last_close - pd.Timedelta(hours=hours)
                if self.state.last_signal_bar.get(key) == bar_open.isoformat():
                    continue
                if now - last_close.to_pydatetime() > SIGNAL_MAX_DELAY:
                    continue
                if pd.Timestamp(now) < pd.Timestamp(m.training_end):
                    continue
                bars = closed_model_bars(
                    await self.fetch_bars(sym, m.timeframe), sym, m.timeframe, now
                )
                if len(bars) < 260 or bars.index[-1] != bar_open:
                    continue
                self.state.last_signal_bar[key] = bar_open.isoformat()
                last = bars.iloc[-1]
                spread = (last["ask_close"] - last["bid_close"]) / pair(sym).pip_size
                row = model_signals(mid_of(bars), m).iloc[-1]
                if int(row["dir"]) == 0 or spread > MAX_BAR_SPREAD_PIPS:
                    continue
                proposals.append({"model": m, "row": row, "bar_open": bar_open})
            dirs = {int(p["row"]["dir"]) for p in proposals}
            if len(dirs) > 1:
                self.journal(
                    "conflict", symbol=sym, models=[p["model"].model_id for p in proposals]
                )
                continue
            out += [{"symbol": sym, **p} for p in proposals]
        return sorted(out, key=lambda p: -float(p["row"]["score"]))

    async def _entry(self, now: datetime, quotes: dict[str, Quote]) -> None:
        if not self.state.active_ids:
            self.say("NO TRADE: nenhum candidato elegível no registo de treino")
            return
        if self.risk.state.kill_switch:
            self.say(f"kill switch ativo: {self.risk.state.kill_reason}")
            return
        candidates = await self._signals(now)
        if not candidates:
            return
        broker = await self.fetch_positions()
        # fetching history can take long: decide on a fresh clock and fresh LIVE quotes
        quotes = await self.ex.live_quotes(list(self.config.symbols))
        now = self.clock()
        managed = {t["symbol"] for t in self.state.open.values()}
        ambiguous = {i["symbol"]: c for c, i in self.state.pending.items()}
        for c in candidates:
            sym, m, row = c["symbol"], c["model"], c["row"]
            if sym in managed or abs(broker.get(sym, 0.0)) > 1e-9:
                self.journal("skip", symbol=sym, reason="par com posição (do piloto ou externa)")
                continue
            bar_close = c["bar_open"].to_pydatetime() + timedelta(hours=BAR_HOURS[m.timeframe])
            if now - bar_close > SIGNAL_MAX_DELAY:
                self.journal("skip", symbol=sym, reason="sinal antigo: vela fechou há demasiado")
                continue
            q = quotes.get(sym)
            if q is None:
                self.journal("skip", symbol=sym, reason="sem cotação LIVE recente")
                continue
            d = int(row["dir"])
            side = Side.BUY if d > 0 else Side.SELL
            ref = q.price_for(side)
            stop = ref - d * float(row["stop_dist"])
            target = ref + d * float(row["target_dist"])
            coid = f"P-{m.model_id}-{sym}-{c['bar_open']:%Y%m%d%H%M}"
            decision = self.risk.evaluate(
                TradeRequest(
                    sym,
                    Direction.LONG if d > 0 else Direction.SHORT,
                    stop,
                    target,
                    float(row["score"]),
                    coid,
                ),
                quotes=quotes,
                account=self.account(quotes),
                positions=self._positions(),
                now=now,
                currency=self.config.currency,
                ambiguous_pairs=ambiguous,
            )
            if not decision.approved:
                self.journal(
                    "refused", coid=coid, refusals=[r.to_dict() for r in decision.refusals]
                )
                self.say(f"{sym} recusado: {', '.join(r.code for r in decision.refusals)}")
                continue
            p = pair(sym)
            info = {
                "kind": "entry",
                "symbol": sym,
                "model_id": m.model_id,
                "side": side.value,
                "ref_price": ref,
                "stop": stop,
                "target": target,
                "score": float(row["score"]),
                "quote_to_acct": Converter(quotes).rate(p.quote, self.config.currency),
                "time_stop_at": (
                    c["bar_open"].to_pydatetime()
                    + timedelta(hours=BAR_HOURS[m.timeframe] * (1 + int(row["max_hold"])))
                ).isoformat(),
            }
            if not self.armed:
                self.journal("dry_run_entry", coid=coid, units=decision.units, **info)
                self.say(
                    f"[dry-run] entraria {side.value} {decision.units:.0f} {sym} ({m.model_id})"
                )
                return
            req = OrderRequest(coid, coid, sym, side, decision.units, now, False, stop, target)
            self.risk.reserve(coid, sym)
            self.state.pending[coid] = info
            self.journal("order_sent", coid=coid, units=decision.units, **info)
            self.save()  # write-ahead: a crash after this line is reconciled, never resent
            try:
                st = await self.ex.place_bracket(req)
            except ReadOnlyApiError:
                self.state.pending.pop(coid, None)
                self.risk.release(coid)
                raise
            except ExecutionError as e:
                if e.ambiguous:
                    self.journal("order_ambiguous", coid=coid)
                else:
                    self.state.pending.pop(coid, None)
                    self.risk.release(coid)
                    self.journal("order_rejected", coid=coid, reason=str(e))
                return
            await self._apply_outcome(coid, info, st)
            return  # at most one entry per cycle

    # --- status ---------------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        s = self.state
        return {
            "label": LABEL,
            "armed": self.armed,
            "active_ids": s.active_ids,
            "retired_ids": s.retired_ids,
            "open": s.open,
            "pending": list(s.pending),
            "realized": round(s.realized, 2),
            "closed_trades": {k: len(v) for k, v in s.returns.items()},
            "risk": self.risk.status(),
        }


# --- wiring to ib_async ---------------------------------------------------------------------


def ib_bars_fetcher(ib: Any) -> BarsFetcher:
    from app.fx.ibkr.adapter import forex_contract
    from app.fx.research.history import bars_to_frame

    async def fetch(symbol: str, timeframe: str) -> pd.DataFrame:
        duration = "90 D" if timeframe == "H4" else "30 D"
        contract = forex_contract(symbol)
        sides = {}
        for side in ("BID", "ASK"):
            rows = await ib.reqHistoricalDataAsync(
                contract, "", duration, "15 mins", side, False, formatDate=2
            )
            df = bars_to_frame(rows).set_index("ts").sort_index()
            sides[side] = df[~df.index.duplicated()].add_prefix(f"{side.lower()}_")
        return sides["BID"].join(sides["ASK"], how="inner")

    return fetch


def ib_positions_fetcher(ib: Any) -> PositionsFetcher:
    async def fetch() -> dict[str, float]:
        out: dict[str, float] = {}
        for p in await ib.reqPositionsAsync():
            c = p.contract
            if getattr(c, "secType", "") == "CASH":
                sym = f"{c.symbol}{c.currency}"
                out[sym] = out.get(sym, 0.0) + float(p.position)
        return out

    return fetch


async def run(args: argparse.Namespace) -> int:
    from ib_async import IB

    root = Path(args.dir)
    root.mkdir(parents=True, exist_ok=True)
    log = root / "pilot.log"

    def say(msg: str) -> None:
        line = f"{datetime.now(UTC):%Y-%m-%d %H:%M:%S}Z {msg}"
        print(line, flush=True)
        with contextlib.suppress(OSError), log.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    if args.port in LIVE_PORTS:
        say(f"porta {args.port} é a porta habitual de sessões REAIS: recusado")
        return 3
    armed = bool(args.arm)
    if armed and args.confirm != CONFIRM_WORD:
        say(f"para armar escreva --confirm {CONFIRM_WORD}")
        return 2
    registry = json.loads(Path(args.selection).read_text(encoding="utf-8"))
    active = load_models(Path(args.selection))  # validates the registry and the selection
    if args.self_check:
        import ib_async  # noqa: F401 - the executable must ship the API client

        say(LABEL)
        say(f"registo: {args.selection} (protocolo {str(registry.get('protocol_sha256'))[:12]})")
        say(
            "candidatos ativos: " + ", ".join(m.model_id for m in active)
            if active
            else "NENHUM candidato elegível: entradas bloqueadas (NO TRADE)"
        )
        say("auto-verificação OK (sem ligação ao IBKR)")
        return 0
    stop_file = root / "STOP"
    if armed and stop_file.exists():
        say(f"{stop_file} existe: apague-o para armar o piloto")
        return 2
    say(LABEL)
    say(f"modo: {'ARMADO (ordens na conta PAPER)' if armed else 'dry-run (nenhuma ordem)'}")
    ib = IB()
    await ib.connectAsync(
        "127.0.0.1", args.port, clientId=args.client_id, timeout=10, readonly=not armed
    )
    store = FxStore(root / "pilot.sqlite")
    try:
        ex = IbkrPaperExecutor(ib, currency="EUR")
        say(f"conta paper: {mask(ex.verify_paper())}")
        pilot = PaperPilot(
            ex,
            store,
            registry,
            ib_bars_fetcher(ib),
            ib_positions_fetcher(ib),
            armed=armed,
            stop_file=stop_file,
            say=say,
        )
        pilot.journal("start", selection=str(args.selection), active=pilot.state.active_ids)
        say(f"candidatos ativos: {pilot.state.active_ids or 'nenhum (NO TRADE)'}")
        while True:
            try:
                await pilot.cycle()
            except PilotStopError as e:
                say(f"piloto parado: {e}")
                return 0
            (root / "status.json").write_text(
                json.dumps(pilot.status(), indent=2, default=str), encoding="utf-8"
            )
            if args.once:
                return 0
            await asyncio.sleep(args.interval)
    finally:
        store.close()
        ib.disconnect()


def main(argv: list[str] | None = None) -> int:
    with contextlib.suppress(AttributeError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    ap = argparse.ArgumentParser(description=LABEL)
    ap.add_argument("--selection", default=str(default_selection()))
    ap.add_argument("--dir", default=str(default_dir()))
    ap.add_argument("--port", type=int, default=4002)
    ap.add_argument("--client-id", type=int, default=52)
    ap.add_argument("--interval", type=float, default=30.0)
    ap.add_argument("--once", action="store_true", help="one cycle, then exit")
    ap.add_argument("--arm", action="store_true", help="send orders to the PAPER account")
    ap.add_argument("--confirm", default="")
    ap.add_argument("--self-check", action="store_true", help="validate offline and exit")
    args = ap.parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("interrompido: piloto desarmado")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
