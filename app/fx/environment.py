"""Execution environments, identified explicitly and never interchangeable (ADR-0036).

- SIMULATION: local simulator and synthetic or replayed data; no external service.
- DEMO: a broker's practice / paper account (no real money), with its own configuration and
  credentials.
- REAL: a real-money account. It exists ONLY as an identifier so the architecture can name it;
  it cannot be executed: `require_executable(REAL)` raises through `refuse_real_money_order`
  (ADR-0006). Enabling it requires a new ADR and the owner's explicit approval.

An adapter never falls back from one environment to another (no demo<->real fallback).

`ENABLED` lists the environments this build can run (an adapter exists for them): only
SIMULATION. `validate_environment` is the single check, done by the engine before any
processing, that the configured environment, the adapter's declared environment and the
enabled set agree.
"""

from enum import StrEnum
from typing import NoReturn

from app.core.safety import refuse_real_money_order
from app.fx.errors import EnvironmentMismatchError


class Environment(StrEnum):
    SIMULATION = "simulation"
    DEMO = "demo"
    REAL = "real"


EXECUTABLE: frozenset[Environment] = frozenset({Environment.SIMULATION, Environment.DEMO})

LABELS: dict[Environment, str] = {
    Environment.SIMULATION: "SIMULAÇÃO",
    Environment.DEMO: "DEMO (conta de prática)",
    Environment.REAL: "REAL (bloqueado)",
}


def _refuse() -> NoReturn:
    refuse_real_money_order()


def require_executable(env: Environment) -> Environment:
    """Returns `env` if orders may be routed in it; REAL is always refused."""
    if env is Environment.REAL:
        _refuse()
    if env not in EXECUTABLE:
        raise ValueError(f"environment {env!r} is not executable")
    return env


def parse_environment(text: str) -> Environment:
    try:
        return Environment(text.strip().lower())
    except ValueError as exc:
        raise ValueError(f"unknown environment {text!r}") from exc


# Environments with an implemented adapter in this build. DEMO is a valid identifier but has no
# adapter yet; REAL is blocked by `require_executable` whatever this set says.
ENABLED: frozenset[Environment] = frozenset({Environment.SIMULATION})


def validate_environment(configured: Environment, adapter_environment: Environment) -> Environment:
    """The one source of truth for the running environment. Raises
    `RealMoneyExecutionBlockedError` for REAL and `EnvironmentMismatchError` when the
    configuration, the adapter and the enabled environments disagree."""
    require_executable(configured)
    require_executable(adapter_environment)
    if configured not in ENABLED:
        raise EnvironmentMismatchError(
            f"o ambiente configurado '{configured.value}' não está ativo nesta versão "
            f"(ativos: {', '.join(sorted(e.value for e in ENABLED))})"
        )
    if adapter_environment is not configured:
        raise EnvironmentMismatchError(
            f"o adaptador implementa '{adapter_environment.value}' mas a configuração "
            f"pede '{configured.value}'"
        )
    return configured
