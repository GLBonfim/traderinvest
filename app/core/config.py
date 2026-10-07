"""Application settings, loaded exclusively from environment variables / .env."""

from enum import StrEnum
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


class TradingMode(StrEnum):
    """Execution modes the platform can run in.

    There is deliberately no LIVE member: real-money execution cannot be
    expressed in configuration. Adding it requires a code change, an ADR and
    explicit owner approval (see docs/decisions.md, ADR-0006).
    """

    DISABLED = "disabled"
    PAPER = "paper"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = "json"

    trading_mode: TradingMode = TradingMode.DISABLED

    postgres_host: str = "localhost"
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    postgres_db: str = "quant"
    postgres_user: str = "quant"
    postgres_password: SecretStr
    db_connect_timeout_s: int = Field(default=3, ge=1, le=60)

    def database_url(self, database: str | None = None) -> URL:
        """SQLAlchemy URL. Returned as a URL object so the password is masked in repr/str."""
        return URL.create(
            drivername="postgresql+psycopg",
            username=self.postgres_user,
            password=self.postgres_password.get_secret_value(),
            host=self.postgres_host,
            port=self.postgres_port,
            database=database or self.postgres_db,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()  # required fields (POSTGRES_PASSWORD) come from the environment
