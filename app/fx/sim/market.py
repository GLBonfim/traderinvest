"""Synthetic FX market (SIMULATION only). Prices are generated, NOT market data.

Model: each currency has a log value against a common numeraire; a pair's mid price is
value(base) / value(quote), so crosses are always consistent (EURJPY = EURUSD x USDJPY).
Each currency follows a regime-switching process (trend up, trend down, range, volatile) whose
regimes last a few hours on average, so trend, momentum and mean-reversion agents all meet
conditions that suit them. Activity and spreads follow the FX sessions and widen around the
17:00 New York rollover. The generator is seeded and its whole state is serialisable, so a
restarted simulation continues exactly where it stopped.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

import numpy as np

from app.fx.bars import BarBuilder
from app.fx.calendar import NEW_YORK, FxCalendar
from app.fx.models import PAIRS, Bar, DataOrigin, Quote, utc

SOURCE = "traderinvest-sim"

# Value of one unit of each currency in USD at the start (illustrative levels, not quotes).
START_VALUE: dict[str, float] = {
    "USD": 1.0,
    "EUR": 1.08,
    "GBP": 1.27,
    "JPY": 1 / 150.0,
    "AUD": 0.66,
    "CHF": 1 / 0.88,
    "CAD": 1 / 1.36,
    "NZD": 0.60,
}

# Typical spreads in pips (illustrative retail ECN-like levels).
BASE_SPREAD_PIPS: dict[str, float] = {
    "EURUSD": 0.8,
    "GBPUSD": 1.2,
    "USDJPY": 1.0,
    "AUDUSD": 1.0,
    "USDCHF": 1.4,
    "USDCAD": 1.6,
    "NZDUSD": 1.6,
    "EURGBP": 1.2,
    "EURJPY": 1.8,
}

ASIAN = frozenset({"JPY", "AUD", "NZD"})
STEP = timedelta(minutes=1)
ANNUAL_VOL = 0.05  # per currency; a pair's volatility is ~sqrt(2) times this
MINUTES_PER_YEAR = 260 * 1440


class Regime(StrEnum):
    TREND_UP = "trend_up"
    TREND_DOWN = "trend_down"
    RANGE = "range"
    VOLATILE = "volatile"


_REGIMES = tuple(Regime)


@dataclass
class _Ccy:
    x: float  # log value
    regime: Regime
    anchor: float  # range regime: the level it reverts to


class SyntheticMarket:
    def __init__(
        self,
        pairs: Sequence[str],
        start: datetime,
        seed: int = 7,
        calendar: FxCalendar | None = None,
        mean_regime_minutes: float = 360.0,
    ) -> None:
        self.pairs = [p for p in pairs if p in PAIRS]
        if not self.pairs:
            raise ValueError("no known pairs to simulate")
        self.calendar = calendar or FxCalendar()
        self.now = self.calendar.next_open(utc(start).replace(second=0, microsecond=0))
        self.seed = seed
        self.rng = np.random.default_rng(seed)
        self.switch_p = 1.0 / mean_regime_minutes
        self.sigma = ANNUAL_VOL / float(np.sqrt(MINUTES_PER_YEAR))
        self.ccys: dict[str, _Ccy] = {}
        for c in sorted({ccy for p in self.pairs for ccy in (PAIRS[p].base, PAIRS[p].quote)}):
            x = float(np.log(START_VALUE[c]))
            self.ccys[c] = _Ccy(x, _REGIMES[int(self.rng.integers(len(_REGIMES)))], x)
        self.quotes: dict[str, Quote] = {}
        self.builder = BarBuilder()

    @property
    def origin(self) -> DataOrigin:
        return DataOrigin.SYNTHETIC

    # --- generation ------------------------------------------------------------------------

    def _activity(self, ts: datetime) -> float:
        sessions = set(self.calendar.active_sessions(ts))
        if {"London", "New York"} <= sessions:
            return 1.25
        if sessions & {"London", "New York"}:
            return 1.0
        return 0.6

    def _evolve(self, ts: datetime) -> None:
        act = self._activity(ts)
        for c in self.ccys.values():
            if self.rng.random() < self.switch_p:
                c.regime = _REGIMES[int(self.rng.integers(len(_REGIMES)))]
                c.anchor = c.x
            s = self.sigma * act
            shock = float(self.rng.standard_normal())
            if c.regime is Regime.TREND_UP:
                c.x += 0.12 * s + s * shock
            elif c.regime is Regime.TREND_DOWN:
                c.x += -0.12 * s + s * shock
            elif c.regime is Regime.RANGE:
                c.x += 0.02 * (c.anchor - c.x) + 0.8 * s * shock
            else:
                c.x += 2.2 * s * shock

    def _spread_multiplier(self, symbol: str, ts: datetime) -> float:
        ny = ts.astimezone(NEW_YORK)
        minutes = ny.hour * 60 + ny.minute
        mult = 1.0
        if 16 * 60 + 55 <= minutes < 17 * 60 + 15:  # rollover: liquidity drains
            mult *= 5.0
        p = PAIRS[symbol]
        sessions = set(self.calendar.active_sessions(ts))
        if not sessions & {"London", "New York"} and not {p.base, p.quote} & ASIAN:
            mult *= 1.4
        if Regime.VOLATILE in (self.ccys[p.base].regime, self.ccys[p.quote].regime):
            mult *= 1.5
        return mult

    def _quote(self, symbol: str, ts: datetime) -> Quote:
        p = PAIRS[symbol]
        mid = float(np.exp(self.ccys[p.base].x - self.ccys[p.quote].x))
        jitter = 1.0 + 0.15 * abs(float(self.rng.standard_normal()))
        spread = BASE_SPREAD_PIPS.get(symbol, 2.0) * p.pip_size
        spread *= self._spread_multiplier(symbol, ts) * jitter
        digits = 3 if p.pip_size >= 0.01 else 5
        bid = round(mid - spread / 2, digits)
        ask = round(mid + spread / 2, digits)
        if ask <= bid:
            ask = round(bid + 10 ** (-digits), digits)
        return Quote(symbol, bid, ask, ts, DataOrigin.SYNTHETIC, SOURCE)

    def step(self) -> tuple[dict[str, Quote], list[tuple[str, str, Bar]]]:
        """Advances one simulated minute (skipping closed market time).
        Returns the new quotes and the bars they closed as (pair, timeframe, bar)."""
        nxt = self.calendar.next_open(self.now + STEP)
        self.now = nxt
        self._evolve(nxt)
        closed: list[tuple[str, str, Bar]] = []
        for sym in self.pairs:
            q = self._quote(sym, nxt)
            self.quotes[sym] = q
            closed.extend((sym, tf, bar) for tf, bar in self.builder.on_quote(q))
        return dict(self.quotes), closed

    def warm_up(self, minutes: int) -> None:
        """Generates history (bars) before trading starts."""
        for _ in range(minutes):
            self.step()

    # --- MarketDataProvider ------------------------------------------------------------------

    def latest(self, pair: str) -> Quote | None:
        return self.quotes.get(pair)

    def bars(self, pair: str, timeframe: str, count: int) -> list[Bar]:
        return self.builder.bars(pair, timeframe, count)

    def regimes(self) -> dict[str, str]:
        """The generator's hidden regimes (diagnostics and tests only; agents never see it)."""
        return {c: s.regime.value for c, s in self.ccys.items()}

    # --- persistence -------------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "pairs": self.pairs,
            "now": self.now.isoformat(),
            "seed": self.seed,
            "rng": self.rng.bit_generator.state,
            "ccys": {c: [s.x, s.regime.value, s.anchor] for c, s in self.ccys.items()},
            "quotes": {s: [q.bid, q.ask, q.ts.isoformat()] for s, q in self.quotes.items()},
            "bars": self.builder.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any], calendar: FxCalendar | None = None) -> "SyntheticMarket":
        if d.get("version") != 1:
            raise ValueError("unsupported synthetic market state version")
        m = cls(d["pairs"], datetime.fromisoformat(d["now"]), int(d["seed"]), calendar)
        m.now = datetime.fromisoformat(d["now"])
        m.rng.bit_generator.state = d["rng"]
        for c, (x, regime, anchor) in d["ccys"].items():
            m.ccys[c] = _Ccy(float(x), Regime(regime), float(anchor))
        m.quotes = {
            s: Quote(s, float(b), float(a), datetime.fromisoformat(t), DataOrigin.SYNTHETIC, SOURCE)
            for s, (b, a, t) in d["quotes"].items()
        }
        m.builder.load(d["bars"])
        return m
