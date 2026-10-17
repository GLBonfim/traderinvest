"""Shared builders for the FX tests (synthetic data only; no network, no real accounts)."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.fx.config import FxConfig, SimulationSettings
from app.fx.models import Bar, DataOrigin, Quote, pair
from app.fx.orchestrator import Orchestrator
from app.fx.store import FxStore

# Wednesday 14 October 2026, 12:00 UTC: London and New York open, far from the rollover.
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


def quote(
    sym: str,
    mid: float,
    spread_pips: float = 1.0,
    ts: datetime = NOW,
    origin: DataOrigin = DataOrigin.SYNTHETIC,
) -> Quote:
    pip = pair(sym).pip_size
    half = spread_pips * pip / 2
    digits = 3 if pip >= 0.01 else 5
    return Quote(sym, round(mid - half, digits), round(mid + half, digits), ts, origin, "test")


def quotes(ts: datetime = NOW, **mids: float) -> dict[str, Quote]:
    base = {"EURUSD": 1.08, "GBPUSD": 1.27, "USDJPY": 150.0, "EURGBP": 0.85}
    base.update(mids)
    return {s: quote(s, m, ts=ts) for s, m in base.items()}


def bars(
    closes: Sequence[float], sym: str = "EURUSD", wiggle: float = 0.0002, start: datetime = NOW
) -> list[Bar]:
    out = []
    prev = closes[0]
    for i, c in enumerate(closes):
        hi, lo = max(prev, c) + wiggle, min(prev, c) - wiggle
        out.append(
            Bar(
                sym,
                start + timedelta(minutes=15 * i),
                prev,
                hi,
                lo,
                c,
                0.0001,
                DataOrigin.SYNTHETIC,
            )
        )
        prev = c
    return out


def small_config(**over: object) -> FxConfig:
    cfg = FxConfig(
        pairs=["EURUSD", "GBPUSD", "USDJPY", "EURGBP"],
        simulation=SimulationSettings(warm_up_minutes=1500, seed=11),
    )
    return cfg.model_copy(update=over)


def orchestrator(root: Path, cfg: FxConfig | None = None, now: datetime = NOW) -> Orchestrator:
    return Orchestrator(cfg or small_config(), FxStore(root / "fx.db"), now)
