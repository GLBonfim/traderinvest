"""Instrument registry: what is traded, where it is listed, which calendar applies, and how each
provider names it. Strategies reference instruments, never provider tickers."""

from collections.abc import Mapping
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import Instrument, ProviderSymbol


@dataclass(frozen=True)
class InstrumentSpec:
    underlying: str
    symbol: str
    name: str
    asset_class: str
    exchange: str  # listing venue MIC
    calendar: str  # trading-calendar MIC used for sessions
    currency: str
    timezone: str
    provider_symbols: Mapping[str, str] = field(default_factory=dict)


# NYSE Arca follows the NYSE holiday calendar and 09:30-16:00 ET regular session -> XNYS.
SPY = InstrumentSpec(
    underlying="S&P 500",
    symbol="SPY",
    name="SPDR S&P 500 ETF Trust",
    asset_class="etf",
    exchange="ARCX",
    calendar="XNYS",
    currency="USD",
    timezone="America/New_York",
    provider_symbols={"yfinance": "SPY"},
)

REGISTRY: dict[str, InstrumentSpec] = {SPY.symbol: SPY}


def get_spec(symbol: str) -> InstrumentSpec:
    try:
        return REGISTRY[symbol.upper()]
    except KeyError:
        raise KeyError(f"Unknown instrument {symbol!r}; registered: {sorted(REGISTRY)}") from None


def ensure_instrument(session: Session, spec: InstrumentSpec) -> Instrument:
    """Idempotently create the instrument and its provider symbols."""
    inst = session.scalar(
        select(Instrument).where(
            Instrument.symbol == spec.symbol, Instrument.exchange == spec.exchange
        )
    )
    if inst is None:
        inst = Instrument(
            underlying=spec.underlying,
            symbol=spec.symbol,
            name=spec.name,
            asset_class=spec.asset_class,
            exchange=spec.exchange,
            currency=spec.currency,
            timezone=spec.timezone,
        )
        session.add(inst)
        session.flush()

    existing = {
        ps.provider: ps
        for ps in session.scalars(
            select(ProviderSymbol).where(ProviderSymbol.instrument_id == inst.id)
        )
    }
    for provider, ticker in spec.provider_symbols.items():
        if provider not in existing:
            session.add(
                ProviderSymbol(instrument_id=inst.id, provider=provider, provider_symbol=ticker)
            )
    session.flush()
    return inst
