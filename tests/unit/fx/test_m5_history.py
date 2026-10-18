from datetime import UTC, datetime

import pandas as pd
import pytest

from app.fx.research.m5_history import ends, validate


def frame(freq: str = "5min") -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts": pd.date_range("2025-01-01", periods=20, freq=freq, tz="UTC"),
            "open": 1.1,
            "high": 1.2,
            "low": 1.0,
            "close": 1.15,
        }
    )


def test_native_m5_only() -> None:
    assert len(validate(frame())) == 20
    with pytest.raises(ValueError, match="genuine"):
        validate(frame("15min"))
    with pytest.raises(ValueError, match="aligned"):
        validate(frame("1min"))


def test_reject_bad_ohlc() -> None:
    bad = frame()
    bad.loc[0, "high"] = 0.9
    with pytest.raises(ValueError, match="OHLC"):
        validate(bad)


def test_reject_nonfinite_quotes() -> None:
    bad = frame()
    bad.loc[0, "high"] = float("inf")
    with pytest.raises(ValueError, match="invalid price"):
        validate(bad)


def test_chunks_cover_training_and_validation_calendar() -> None:
    assert len(ends()) == len(set(ends())) == 198
    assert min(ends()) == datetime(2023, 1, 8, tzinfo=UTC)
    assert max(ends()) == datetime(2026, 10, 17, tzinfo=UTC)
