import pytest
from pydantic import ValidationError

from app.core.config import Settings, TradingMode


def make(**env: str) -> Settings:
    """Build settings from explicit values only (ignores the developer's .env)."""
    return Settings(_env_file=None, **env)  # type: ignore[call-arg]


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("TRADING_MODE", "APP_ENV", "LOG_LEVEL", "LOG_FORMAT", "POSTGRES_PASSWORD"):
        monkeypatch.delenv(var, raising=False)


def test_trading_is_disabled_by_default() -> None:
    assert make(postgres_password="x").trading_mode is TradingMode.DISABLED


def test_live_trading_mode_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make(postgres_password="x", trading_mode="live")


@pytest.mark.parametrize("value", ["LIVE", "real", "production", "enabled", ""])
def test_unknown_trading_modes_are_rejected(value: str) -> None:
    with pytest.raises(ValidationError):
        make(postgres_password="x", trading_mode=value)


def test_trading_mode_enum_has_no_live_member() -> None:
    assert {m.value for m in TradingMode} == {"disabled", "paper"}


def test_settings_are_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_PASSWORD", "from-env")
    monkeypatch.setenv("POSTGRES_PORT", "6543")
    monkeypatch.setenv("TRADING_MODE", "paper")
    s = make()
    assert s.postgres_password.get_secret_value() == "from-env"
    assert s.postgres_port == 6543
    assert s.trading_mode is TradingMode.PAPER


def test_password_is_required() -> None:
    with pytest.raises(ValidationError):
        make()


def test_password_never_appears_in_repr_or_url_string() -> None:
    s = make(postgres_password="super-secret-value")
    assert "super-secret-value" not in repr(s)
    assert "super-secret-value" not in str(s.database_url())
    assert "super-secret-value" not in repr(s.database_url())


def test_database_url_uses_psycopg_driver() -> None:
    url = make(postgres_password="x", postgres_db="quant").database_url()
    assert url.drivername == "postgresql+psycopg"
    assert url.database == "quant"
    assert make(postgres_password="x").database_url("other").database == "other"
