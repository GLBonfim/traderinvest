"""Parsing of the frozen raw files into pandas objects (no network, no mutation)."""

from pathlib import Path

import pandas as pd

PRICE_COLUMNS = ("open", "high", "low", "close", "adj_close", "volume", "dividends", "splits")


def read_price_csv(path: Path, data_end: str | None = None) -> pd.DataFrame:
    """Stored Yahoo CSV -> frame indexed by naive session date, optionally cut at `data_end`."""
    frame = pd.read_csv(path, index_col="date", dtype=dict.fromkeys(PRICE_COLUMNS, "float64"))
    frame.index = pd.DatetimeIndex(pd.to_datetime(frame.index, format="%Y-%m-%d"), name="date")
    if data_end is not None:
        frame = frame.loc[frame.index <= pd.Timestamp(data_end)]
    return frame


def read_ecb_csv(path: Path, data_end: str | None = None) -> pd.Series:
    """ECB SDMX csvdata -> float series indexed by naive date (TIME_PERIOD), sorted."""
    frame = pd.read_csv(path, usecols=["TIME_PERIOD", "OBS_VALUE"])
    series = pd.Series(
        frame["OBS_VALUE"].astype("float64").to_numpy(),
        index=pd.DatetimeIndex(pd.to_datetime(frame["TIME_PERIOD"], format="%Y-%m-%d")),
        name="value",
    ).sort_index()
    if data_end is not None:
        series = series.loc[series.index <= pd.Timestamp(data_end)]
    return series


def total_return_factors(close: pd.Series, dividends: pd.Series) -> pd.Series:
    """Daily gross total-return factor ``(close_t + dividend_t) / close_{t-1}``.

    Dividends are booked on their ex-date, gross, reinvested at that day's close. The first
    row has no factor (NaN).
    """
    return (close + dividends.fillna(0.0)) / close.shift(1)
