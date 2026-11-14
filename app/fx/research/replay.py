"""System-level historical replay: the REAL orchestrator, coordinator, risk manager and
simulated venue on bid/ask M5/M15/H1 history (SIMULATION, HISTORICAL data).
M5 is an isolated research path; no registry promotion or broker integration is performed.

- every bar is replayed as four quote ticks in the usual OHLC path (open, low then high for a
  rising bar / high then low for a falling one, close), so venue-held stops and targets trigger
  inside the bar;
- agents are the v2 signal functions evaluated once over the whole history (causal) and looked
  up bar by bar (`PrecomputedAgent`): same code as production, without recomputing indicators
  24,000 times; production agents use a 600-bar window (negligible EMA seeding difference);
- legacy replay commission remains 40 per million; explicit overrides may add a USD
  per-order minimum. Overnight financing is not modelled by the simulated venue;
- entries fill at the signal close and the four-tick OHLC path is assumed, not observed.
  Replay results cannot establish paper/live qualification or exact M5 research parity.

Everything the product does is exercised: one entry per cycle, conflicts, regime vetoes, risk
sizing in EUR, leverage/margin/exposure checks, 5 entries per FX day, -5 % / +15 % daily locks,
weekend flattening, rollover window, spread limit.
"""

import math
import tempfile
from collections.abc import Iterator, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, overload

import numpy as np
import pandas as pd

from app.fx.accounting import Converter
from app.fx.agents.base import Agent, MarketView, from_signal, hold, make
from app.fx.agents.coordinator import PortfolioCoordinator
from app.fx.calendar import FxCalendar
from app.fx.config import FxConfig, SimulationSettings
from app.fx.environment import Environment
from app.fx.models import Bar, DataOrigin, Direction, Quote, Side
from app.fx.orchestrator import Orchestrator
from app.fx.orders import ExecutionError, Fill, OrderRequest
from app.fx.ports import Capabilities
from app.fx.proposals import AgentProposal, AgentRole, ProposalAction
from app.fx.sim.broker import SimBroker, SimBrokerConfig
from app.fx.sim.market import SyntheticMarket
from app.fx.store import FxStore

SOURCE = "ibkr-history"


class PairArrays:
    """One pair's bid/ask/mid OHLC as numpy arrays (fast, typed)."""

    def __init__(self, symbol: str, df: pd.DataFrame) -> None:
        self.symbol = symbol
        self.index = pd.DatetimeIndex(df.index)
        self.ts = [t.to_pydatetime() for t in self.index]
        self.px: dict[str, np.ndarray] = {
            f"{side}_{k}": df[f"{side}_{k}"].to_numpy(float)
            for side in ("bid", "ask")
            for k in ("open", "high", "low", "close")
        }
        for k in ("open", "high", "low", "close"):
            self.px[f"mid_{k}"] = (self.px[f"bid_{k}"] + self.px[f"ask_{k}"]) / 2
        self.spread = self.px["ask_close"] - self.px["bid_close"]

    def bar(self, i: int) -> Bar:
        p = self.px
        return Bar(
            self.symbol,
            self.ts[i],
            float(p["mid_open"][i]),
            float(p["mid_high"][i]),
            float(p["mid_low"][i]),
            float(p["mid_close"][i]),
            float(self.spread[i]),
            DataOrigin.HISTORICAL,
        )

    def price(self, i: int, side: str, sub: int) -> float:
        p = self.px
        o, c = p[f"{side}_open"][i], p[f"{side}_close"][i]
        lo, hi = p[f"{side}_low"][i], p[f"{side}_high"][i]
        rising = p["mid_close"][i] >= p["mid_open"][i]
        path = (o, lo, hi, c) if rising else (o, hi, lo, c)
        return float(path[sub])


class LazyBars(Sequence[Bar]):
    """Closed mid bars [start, end) of one pair, materialised only when indexed."""

    def __init__(self, arrays: PairArrays, start: int, end: int) -> None:
        self.arrays, self.start, self.end = arrays, start, end

    def __len__(self) -> int:
        return self.end - self.start

    @overload
    def __getitem__(self, i: int) -> Bar: ...
    @overload
    def __getitem__(self, i: slice) -> Sequence[Bar]: ...
    def __getitem__(self, i: int | slice) -> Bar | Sequence[Bar]:
        if isinstance(i, slice):
            a, b, _ = i.indices(len(self))
            return LazyBars(self.arrays, self.start + a, self.start + b)
        if i < 0:
            i += len(self)
        if not 0 <= i < len(self):
            raise IndexError(i)
        return self.arrays.bar(self.start + i)

    def __iter__(self) -> Iterator[Bar]:
        for i in range(len(self)):
            yield self[i]


class HistoricalMarket(SyntheticMarket):
    """Market feed replaying bid/ask bars (implements what the orchestrator uses)."""

    def __init__(
        self,
        data: dict[str, pd.DataFrame],
        start: datetime,
        end: datetime,
        calendar: FxCalendar,
        timeframe: str = "M15",
        bar_minutes: int = 15,
    ) -> None:
        self.calendar = calendar
        self.timeframe = timeframe
        step = timedelta(minutes=bar_minutes)
        self.subtick = (timedelta(0), step / 3, 2 * step / 3, step)
        self.pairs = list(data)
        self.arrays = {s: PairArrays(s, df) for s, df in data.items()}
        lo, hi = pd.Timestamp(start), pd.Timestamp(end)
        stamps: set[pd.Timestamp] = set()
        for a in self.arrays.values():
            stamps.update(a.index[(a.index >= lo) & (a.index < hi)])
        self.timeline = sorted(stamps)
        self.pos = {s: {ts: i for i, ts in enumerate(a.index)} for s, a in self.arrays.items()}
        self.closed_upto = {s: int(a.index.searchsorted(lo)) for s, a in self.arrays.items()}
        self.cursor, self.sub = 0, 0
        self.quotes: dict[str, Quote] = {}
        self.now = start

    @property
    def origin(self) -> DataOrigin:
        return DataOrigin.HISTORICAL

    @property
    def done(self) -> bool:
        return self.cursor >= len(self.timeline)

    def step(self) -> tuple[dict[str, Quote], list[tuple[str, str, Bar]]]:
        ts = self.timeline[self.cursor]
        sub = self.sub
        now = ts.to_pydatetime() + self.subtick[sub]
        closed: list[tuple[str, str, Bar]] = []
        for s, a in self.arrays.items():
            i = self.pos[s].get(ts)
            if i is None:
                continue
            bid, ask = a.price(i, "bid", sub), a.price(i, "ask", sub)
            self.quotes[s] = Quote(s, bid, max(ask, bid), now, DataOrigin.HISTORICAL, SOURCE)
            if sub == 3:
                self.closed_upto[s] = i + 1
                closed.append((s, self.timeframe, a.bar(i)))
        self.now = now
        self.sub = (sub + 1) % 4
        if self.sub == 0:
            self.cursor += 1
        return dict(self.quotes), closed

    def bars(self, pair: str, timeframe: str, count: int) -> list[Bar]:
        end = self.closed_upto.get(pair, 0)
        lazy = LazyBars(self.arrays[pair], max(0, end - count), end)
        return lazy  # type: ignore[return-value]  # lazy: only indexed and measured

    def latest(self, pair: str) -> Quote | None:
        return self.quotes.get(pair)

    def warm_up(self, minutes: int) -> None:
        return None

    def to_dict(self) -> dict[str, Any]:
        return {"version": "replay", "cursor": self.cursor, "sub": self.sub}


class ReplayBroker(SimBroker):
    def __init__(
        self,
        config: SimBrokerConfig,
        max_order_units: float | None = None,
        commission_min_usd: float = 0.0,
    ) -> None:
        super().__init__(config)
        self.max_order_units = max_order_units
        self.commission_min_usd = commission_min_usd

    def _check_margin(self, request: OrderRequest, units: float) -> None:
        if (
            not request.reduce_only
            and self.max_order_units is not None
            and units > self.max_order_units
        ):
            raise ExecutionError("replay max_order_units exceeded", ambiguous=False)
        super()._check_margin(request, units)

    def _execute(
        self,
        coid: str,
        sym: str,
        side: Side,
        units: float,
        reason: str,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> Fill:
        if self.commission_min_usd == 0:
            return super()._execute(coid, sym, side, units, reason, stop_loss, take_profit)
        conv = Converter(self.quotes)
        try:
            minimum = self.commission_min_usd * conv.rate("USD", self.config.currency)
            notional = self.book.notional(sym, units, conv)
        except LookupError as exc:
            raise ExecutionError(
                "replay commission USD conversion unavailable", ambiguous=False
            ) from exc
        original = self.config
        # Synchronous execution: apply the minimum through the existing single
        # commission debit/fill path, including stops and risk-reducing closes.
        self.config = replace(
            original,
            commission_per_million=max(
                original.commission_per_million,
                minimum / notional * 1_000_000,
            ),
        )
        try:
            return super()._execute(coid, sym, side, units, reason, stop_loss, take_profit)
        finally:
            self.config = original

    @property
    def capabilities(self) -> Capabilities:
        return replace(
            super().capabilities,
            data_origin=DataOrigin.HISTORICAL,
            description="Corretor simulado (replay histórico IBKR)",
        )


class PrecomputedAgent:
    """A v2 agent whose signal rows were computed once over the whole (causal) history."""

    def __init__(
        self,
        role: AgentRole,
        signals: dict[str, pd.DataFrame],
        feats: dict[str, pd.DataFrame],
        max_hold: int,
    ) -> None:
        self.role, self.version, self.min_bars = role, "2.0.0", 1
        self.signals, self.feats, self.max_hold = signals, feats, max_hold

    def analyze(self, view: MarketView) -> AgentProposal:
        ts = pd.Timestamp(view.bars[-1].ts)
        sig = self.signals[view.pair].loc[ts]
        feat = self.feats[view.pair].loc[ts]
        assert isinstance(sig, pd.Series) and isinstance(feat, pd.Series)  # noqa: S101 - unique index
        return from_signal(
            self,
            view,
            sig,
            feat,
            "sinal de compra",
            "sinal de venda",
            self.max_hold,
        )


class PrecomputedRegime:
    def __init__(self, regimes: dict[str, pd.DataFrame]) -> None:
        self.role, self.version, self.min_bars = AgentRole.REGIME, "2.0.0", 1
        self.regimes = regimes

    def analyze(self, view: MarketView) -> AgentProposal:
        row = self.regimes[view.pair].loc[pd.Timestamp(view.bars[-1].ts)]
        ev: dict[str, Any] = {"regime": str(row["regime"])}
        if bool(row["veto"]):
            return make(self, view, ProposalAction.VETO, Direction.FLAT, 1.0, "veto", ev)
        return hold(self, view, f"regime {row['regime']}", ev)


def run_replay(
    data: dict[str, pd.DataFrame],
    start: datetime,
    end: datetime,
    signals: dict[AgentRole, dict[str, pd.DataFrame]],
    max_holds: dict[AgentRole, int],
    feats: dict[str, pd.DataFrame],
    regimes: dict[str, pd.DataFrame],
    approved: frozenset[str],
    work_dir: Path | None = None,
    timeframe: str = "M15",
    *,
    config_override: FxConfig | None = None,
    max_order_units: float | None = None,
    commission_min_usd: float = 0.0,
) -> dict[str, Any]:
    if timeframe not in ("M5", "M15", "H1"):
        raise ValueError("unsupported replay timeframe")
    if max_order_units is not None and (not math.isfinite(max_order_units) or max_order_units <= 0):
        raise ValueError("replay max_order_units must be finite and positive")
    if not math.isfinite(commission_min_usd) or commission_min_usd < 0:
        raise ValueError("replay commission minimum must be finite and nonnegative")
    bar_minutes = {"M5": 5, "M15": 15, "H1": 60}[timeframe]
    cfg = FxConfig(
        pairs=list(data),
        timeframe=timeframe,
        simulation=SimulationSettings(
            commission_per_million=40.0, slippage_pips=0.0, warm_up_minutes=0
        ),
    )
    if config_override is not None:
        cfg = FxConfig.model_validate(config_override.model_dump())
        if cfg.environment is not Environment.SIMULATION:
            raise ValueError("historical replay requires SIMULATION")
        if cfg.timeframe != timeframe or set(cfg.pairs) != set(data):
            raise ValueError("replay configuration timeframe/pairs mismatch")
    work = work_dir or Path(tempfile.mkdtemp())
    if (work / "replay.db").exists():
        raise ValueError("existing replay journal: choose an isolated directory")
    agents: list[Agent] = [
        PrecomputedAgent(role, signals[role], feats, max_holds[role]) for role in signals
    ]
    o = Orchestrator(
        cfg,
        FxStore(work / "replay.db"),
        start,
        broker_factory=lambda c: ReplayBroker(c, max_order_units, commission_min_usd),
        market_factory=lambda c, now, cal: HistoricalMarket(
            data, start, end, cal, timeframe, bar_minutes
        ),
        agents=agents,
        regime_agent=PrecomputedRegime(regimes),
    )
    o.coordinator = PortfolioCoordinator(
        cfg.risk.min_score, cfg.max_new_entries_per_cycle, approved
    )
    market = o.market
    assert isinstance(market, HistoricalMarket)  # noqa: S101 - constructed just above
    daily: dict[str, float] = {}
    while not market.done:
        o.tick()
        daily[o.risk.state.trading_day or ""] = o.broker.account().equity
    o.save()
    closed = pd.DataFrame(o.broker.closed_positions)
    kinds = pd.Series([e["kind"] for e in o.store.recent(10**7)])
    locks = [e["payload"] for e in o.store.recent(10**6, ("risk_lock",))]
    eq = pd.Series(daily).sort_index()
    eq = eq[eq.index != ""]
    curve = np.concatenate([[cfg.initial_capital], eq.to_numpy(float)])  # from the start
    rets = pd.Series(curve).pct_change().dropna()
    peak = np.maximum.accumulate(curve)
    entries = [i for i in o.intents.values() if i.is_entry and i.filled_units > 0]
    per_day = pd.Series(
        [o.calendar.trading_day(datetime.fromisoformat(i.created_at)).isoformat() for i in entries]
    ).value_counts()
    fills_by_id = {
        f.fill_id: f.to_dict() for status in o.broker.orders.values() for f in status.fills
    }
    for event in o.store.recent(10**7, ("position_closed",)):
        fill = event["payload"]["fill"]
        fills_by_id[fill["fill_id"]] = fill
    pd.DataFrame(list(fills_by_id.values())).to_csv(work / "fills.csv", index=False)
    closed.to_csv(work / "closed_positions.csv", index=False)
    eq.rename("equity").to_csv(work / "equity_daily.csv", index_label="trading_day")
    result = {
        "research_only": True,
        "qualified_for_paper": False,
        "statistically_approved": False,
        "execution_enabled": False,
        "timeframe": timeframe,
        "effective_config": cfg.model_dump(mode="json"),
        "max_order_units": max_order_units,
        "commission_min_usd": commission_min_usd,
        "limitations": [
            "assumed_intrabar_ohlc_path",
            "entry_at_signal_close_not_next_open",
            "financing_not_modelled",
            "no_broker_execution_or_reconciliation",
        ],
        "closed_positions_pnl": "gross_before_commissions",
        "largest_entry_units": max((i.filled_units for i in entries), default=0.0),
        "artifacts": {
            "journal": str(work / "replay.db"),
            "closed_positions": str(work / "closed_positions.csv"),
            "equity_daily": str(work / "equity_daily.csv"),
            "fills": str(work / "fills.csv"),
        },
        "start": start.isoformat(),
        "end": end.isoformat(),
        "approved": sorted(approved),
        "final_equity": float(o.broker.account().equity),
        "return_pct": float(100 * (eq.iloc[-1] / cfg.initial_capital - 1)) if len(eq) else 0.0,
        "max_drawdown_pct": float(100 * ((peak - curve) / peak).max()),
        "daily_sharpe": float(rets.mean() / rets.std() * np.sqrt(252)) if rets.std() > 0 else 0.0,
        "trading_days": len(eq),
        "positions_closed": len(closed),
        "close_reasons": closed["reason"].value_counts().to_dict() if len(closed) else {},
        "realized_total": float(o.broker.book.realized),
        "commissions_total": float(o.broker.book.commissions),
        "entries_filled": len(entries),
        "max_entries_one_day": int(per_day.max()) if len(per_day) else 0,
        "risk_locks": locks,
        "refused_intents": int(sum(1 for i in o.intents.values() if i.state.value == "refused")),
        "journal_events": {k: int(v) for k, v in kinds.value_counts().items()},
        "journal_chain_ok": o.store.verify()[0],
        "equity_daily": {k: round(float(v), 2) for k, v in eq.items()},
    }
    o.store.close()
    return result
