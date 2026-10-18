"""FX platform configuration: a JSON file in the user's data directory, validated by pydantic.

Defaults are PROVISIONAL choices (ADR-0036) the owner can change: EUR account, 10 000 capital,
nine major pairs, M15 decisions, the master prompt's daily limits (5 entries, -5 %, +15 %).
Credentials are never stored here.
"""

import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.fx.environment import Environment
from app.fx.ibkr.settings import IbkrSettings
from app.fx.models import PAIRS
from app.fx.risk import RiskConfig

APP_DIR_NAME = "TraderInvestFX"
ACCOUNT_CURRENCIES = ("EUR", "USD", "GBP", "CHF", "JPY", "AUD", "CAD")


def data_dir() -> Path:
    """`TRADERINVEST_FX_HOME`, else %LOCALAPPDATA%/TraderInvestFX (~/.traderinvest-fx)."""
    env = os.environ.get("TRADERINVEST_FX_HOME")
    if env:
        return Path(env)
    local = os.environ.get("LOCALAPPDATA")
    return Path(local) / APP_DIR_NAME if local else Path.home() / ".traderinvest-fx"


class RiskSettings(BaseModel):
    max_entries_per_day: int = Field(5, ge=0, le=100)
    daily_loss_limit_pct: float = Field(5.0, gt=0, le=50)
    daily_profit_target_pct: float = Field(15.0, gt=0, le=500)
    risk_per_trade_pct: float = Field(0.5, gt=0, le=5)
    max_leverage: float = Field(30.0, gt=0, le=30)  # ESMA retail cap for majors
    max_open_positions: int = Field(4, ge=1, le=20)
    max_spread_pips: float = Field(3.0, gt=0, le=50)
    min_score: float = Field(0.55, ge=0, le=1)
    min_risk_reward: float = Field(1.2, ge=0, le=10)
    flatten_on_daily_loss: bool = True
    flatten_on_profit_target: bool = False

    @model_validator(mode="after")
    def _per_trade_within_daily(self) -> "RiskSettings":
        if self.risk_per_trade_pct > self.daily_loss_limit_pct:
            raise ValueError("risk_per_trade_pct cannot exceed daily_loss_limit_pct")
        return self

    def to_risk_config(self) -> RiskConfig:
        return RiskConfig(**self.model_dump())


class SimulationSettings(BaseModel):
    seed: int = 7
    steps_per_second: float = Field(2.0, gt=0, le=50)  # simulated minutes per real second
    warm_up_minutes: int = Field(60 * 24 * 4, ge=0, le=60 * 24 * 30)
    commission_per_million: float = Field(30.0, ge=0, le=500)
    slippage_pips: float = Field(0.1, ge=0, le=10)
    # SIMULATION only (synthetic prices): let agents that did NOT pass the out-of-sample
    # validation open positions, to demonstrate the interface. Never applies to DEMO.
    allow_unapproved_agents: bool = False


class FxConfig(BaseModel):
    environment: Environment = Environment.SIMULATION
    account_currency: str = "EUR"
    initial_capital: float = Field(10_000.0, gt=0, le=1e9)
    pairs: list[str] = Field(default_factory=lambda: list(PAIRS))
    timeframe: str = "M15"
    history_bars: int = Field(600, ge=280, le=600)  # EMA(200) needs a long window
    max_new_entries_per_cycle: int = Field(1, ge=1, le=10)
    language: str = "pt"
    risk: RiskSettings = Field(default_factory=RiskSettings)
    simulation: SimulationSettings = Field(default_factory=SimulationSettings)
    ibkr: IbkrSettings = Field(default_factory=IbkrSettings)  # read-only paper connection

    @field_validator("account_currency")
    @classmethod
    def _ccy(cls, v: str) -> str:
        v = v.upper()
        if v not in ACCOUNT_CURRENCIES:
            raise ValueError(f"account currency must be one of {ACCOUNT_CURRENCIES}")
        return v

    @field_validator("pairs")
    @classmethod
    def _pairs(cls, v: list[str]) -> list[str]:
        out = [p.replace("/", "").upper() for p in v]
        unknown = [p for p in out if p not in PAIRS]
        if unknown or not out:
            raise ValueError(f"unknown or empty pairs: {unknown}")
        return list(dict.fromkeys(out))

    @field_validator("timeframe")
    @classmethod
    def _tf(cls, v: str) -> str:
        if v not in ("M15", "H1"):
            raise ValueError("timeframe must be M15 or H1")
        return v

    @field_validator("environment")
    @classmethod
    def _env(cls, v: Environment) -> Environment:
        if v is Environment.REAL:
            raise ValueError("the REAL environment is disabled in this build (ADR-0006)")
        return v

    @model_validator(mode="after")
    def _convertible(self) -> "FxConfig":
        ccys = {c for p in self.pairs for c in (PAIRS[p].base, PAIRS[p].quote)}
        if self.account_currency not in ccys:
            raise ValueError("the account currency must appear in at least one configured pair")
        return self


def config_path(root: Path | None = None) -> Path:
    return (root or data_dir()) / "config.json"


def load_config(root: Path | None = None) -> FxConfig:
    path = config_path(root)
    if not path.exists():
        return FxConfig()
    return FxConfig.model_validate(json.loads(path.read_text(encoding="utf-8")))


def save_config(cfg: FxConfig, root: Path | None = None) -> Path:
    path = config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(cfg.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def merge(cfg: FxConfig, patch: dict[str, Any]) -> FxConfig:
    """A validated copy of `cfg` with `patch` applied (nested dicts merged)."""
    base = cfg.model_dump(mode="json")
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k] = {**base[k], **v}
        else:
            base[k] = v
    return FxConfig.model_validate(base)
