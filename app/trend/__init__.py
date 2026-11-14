"""Multi-asset trend research (design v2, ADR-0040).

Offline only: no broker, no orders. One fixed rule (200-session SMA long/cash, volatility-scaled
weights) evaluated through a causal replay and a one-time sealed test.
"""
