"""Execution environments, identified explicitly and never interchangeable (ADR-0036).

- SIMULATION: local simulator and synthetic or replayed data; no external service.
- DEMO: a broker's practice / paper account (no real money), with its own configuration and
  credentials.
- REAL: a real-money account. It exists ONLY as an identifier so the architecture can name it;
  it cannot be executed: `require_executable(REAL)` raises through `refuse_real_money_order`
  (ADR-0006). Enabling it requires a new ADR and the owner's explicit approval.

An adapter never falls back from one environment to another (no demo<->real fallback).
"""

from enum import StrEnum
from typing import NoReturn

from app.core.safety import refuse_real_money_order


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
