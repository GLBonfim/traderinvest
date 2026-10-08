"""Dashboard services: structured, deterministic views over the domain engines.

Nothing here re-implements a financial calculation: every number comes from an existing engine
(candles, price action, indicators, regimes, strategies, backtest, validation, ML, risk, paper)
or is a direct selection/formatting of its output. No Streamlit import is allowed here.
"""
