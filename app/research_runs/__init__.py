"""Persistent registry of offline research runs (statistical validation, ML).

Infrastructure only: it records, publishes and verifies runs; it never computes anything.
See `registry.py` for the on-disk layout, the run states and the integrity guarantees.
"""
