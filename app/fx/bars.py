"""Builds mid-price OHLC bars from quotes, aligned to UTC epoch buckets."""

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.fx.models import Bar, DataOrigin, Quote

TIMEFRAMES: dict[str, int] = {"M1": 60, "M5": 300, "M15": 900, "H1": 3600}


def bucket_start(ts: datetime, seconds: int) -> datetime:
    epoch = int(ts.timestamp())
    return datetime.fromtimestamp(epoch - epoch % seconds, UTC)


@dataclass
class _Open:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    spread_sum: float
    n: int
    origin: DataOrigin

    def bar(self, pair: str) -> Bar:
        return Bar(
            pair,
            self.ts,
            self.open,
            self.high,
            self.low,
            self.close,
            self.spread_sum / self.n,
            self.origin,
        )


class BarBuilder:
    def __init__(self, timeframes: Iterable[str] = ("M1", "M15", "H1"), keep: int = 600) -> None:
        self.timeframes = tuple(timeframes)
        self.keep = keep
        self._open: dict[tuple[str, str], _Open] = {}
        self._closed: dict[tuple[str, str], deque[Bar]] = {}

    def on_quote(self, q: Quote) -> list[tuple[str, Bar]]:
        """Feeds a quote; returns the (timeframe, bar) pairs closed by it."""
        closed: list[tuple[str, Bar]] = []
        for tf in self.timeframes:
            key = (q.pair, tf)
            start = bucket_start(q.ts, TIMEFRAMES[tf])
            cur = self._open.get(key)
            if cur is not None and start > cur.ts:
                bar = cur.bar(q.pair)
                self._closed.setdefault(key, deque(maxlen=self.keep)).append(bar)
                closed.append((tf, bar))
                cur = None
            m = q.mid
            if cur is None:
                self._open[key] = _Open(start, m, m, m, m, q.spread, 1, q.origin)
            else:
                cur.high, cur.low, cur.close = max(cur.high, m), min(cur.low, m), m
                cur.spread_sum += q.spread
                cur.n += 1
        return closed

    def bars(self, pair: str, timeframe: str, count: int) -> list[Bar]:
        d = self._closed.get((pair, timeframe))
        return list(d)[-count:] if d else []

    def to_dict(self) -> dict[str, Any]:
        return {
            "closed": self._closed_dict(),
            "open": {
                f"{p}|{tf}": [
                    o.ts.isoformat(),
                    o.open,
                    o.high,
                    o.low,
                    o.close,
                    o.spread_sum,
                    o.n,
                    o.origin.value,
                ]
                for (p, tf), o in self._open.items()
            },
        }

    def _closed_dict(self) -> dict[str, Any]:
        return {
            f"{p}|{tf}": [
                [b.ts.isoformat(), b.open, b.high, b.low, b.close, b.spread, b.origin.value]
                for b in bars
            ]
            for (p, tf), bars in self._closed.items()
        }

    def load(self, data: dict[str, Any]) -> None:
        for key, o in data.get("open", {}).items():
            p, tf = key.split("|")
            if tf in self.timeframes:
                self._open[(p, tf)] = _Open(
                    datetime.fromisoformat(o[0]),
                    float(o[1]),
                    float(o[2]),
                    float(o[3]),
                    float(o[4]),
                    float(o[5]),
                    int(o[6]),
                    DataOrigin(o[7]),
                )
        for key, rows in data.get("closed", {}).items():
            p, tf = key.split("|")
            if tf not in self.timeframes:
                continue
            d: deque[Bar] = deque(maxlen=self.keep)
            for r in rows:
                d.append(
                    Bar(
                        p,
                        datetime.fromisoformat(r[0]),
                        float(r[1]),
                        float(r[2]),
                        float(r[3]),
                        float(r[4]),
                        float(r[5]),
                        DataOrigin(r[6]),
                    )
                )
            self._closed[(p, tf)] = d
