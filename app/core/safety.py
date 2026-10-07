"""Execution safety guard.

Real-money execution is blocked by design. Any code path that could transmit an
order to a real-money venue MUST call `refuse_real_money_order()`, which always raises.
Changing this module requires an ADR and explicit owner approval.
"""

from typing import NoReturn

from app.core.config import Settings, TradingMode

SAFE_TRADING_MODES: frozenset[TradingMode] = frozenset({TradingMode.DISABLED, TradingMode.PAPER})


class RealMoneyExecutionBlockedError(RuntimeError):
    """Raised whenever code attempts to route an order to a real-money venue."""


def assert_safe_trading_mode(settings: Settings) -> None:
    """Defense in depth: config validation already rejects unknown modes."""
    if settings.trading_mode not in SAFE_TRADING_MODES:
        raise RealMoneyExecutionBlockedError(
            f"Trading mode {settings.trading_mode!r} is not permitted."
        )


def refuse_real_money_order() -> NoReturn:
    """Unconditionally refuses real-money order routing."""
    raise RealMoneyExecutionBlockedError(
        "Real-money execution is disabled and blocked in this build."
    )
