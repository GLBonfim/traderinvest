"""Synthetic sessions long enough for the 272-bar regime warm-up (not market data)."""

from datetime import date
from functools import lru_cache

import numpy as np
import pandas as pd

from app.ml.config import MLConfig, SplitSpec
from app.validation.config import ValidationConfig
from tests.unit.strategies.helpers import session_walk

N = 1000  # 2015-01-02 .. 2018-12
SPLIT = SplitSpec(
    train_end=date(2017, 3, 31),
    validation_start=date(2017, 4, 1),
    validation_end=date(2017, 12, 31),
    test_start=date(2018, 1, 1),
)
CFG = MLConfig(split=SPLIT)
VCFG = ValidationConfig(n_resamples=200)  # explicit, smaller count for unit tests only


@lru_cache(maxsize=4)
def _walk(n: int, seed: int) -> pd.DataFrame:
    b = session_walk(n, seed)
    return b.assign(adj_close=b["close"] * np.linspace(0.85, 1.0, n))


def walk(n: int = N, seed: int = 3) -> pd.DataFrame:
    return _walk(n, seed).copy()
