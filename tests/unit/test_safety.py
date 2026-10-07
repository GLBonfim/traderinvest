import pytest

from app.core.config import Settings, TradingMode
from app.core.safety import (
    RealMoneyExecutionBlockedError,
    assert_safe_trading_mode,
    refuse_real_money_order,
)


def test_real_money_orders_are_always_refused() -> None:
    with pytest.raises(RealMoneyExecutionBlockedError):
        refuse_real_money_order()


@pytest.mark.parametrize("mode", list(TradingMode))
def test_all_configurable_modes_are_safe(mode: TradingMode) -> None:
    settings = Settings(_env_file=None, postgres_password="x", trading_mode=mode)  # type: ignore[call-arg]
    assert_safe_trading_mode(settings)


def test_unsafe_mode_injected_past_validation_is_blocked() -> None:
    settings = Settings(_env_file=None, postgres_password="x")  # type: ignore[call-arg]
    object.__setattr__(settings, "trading_mode", "live")
    with pytest.raises(RealMoneyExecutionBlockedError):
        assert_safe_trading_mode(settings)
