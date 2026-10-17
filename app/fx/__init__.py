"""Forex trading domain (ADR-0036): five agents, an orchestrator, a central risk manager and
execution adapters behind explicit ports, for SIMULATION and DEMO environments.

Real-money execution is not possible from this package: `Environment.REAL` exists only as an
identifier, and every executable path calls `require_executable`, which refuses it through the
platform-wide guard (`app.core.safety`, ADR-0006).
"""
