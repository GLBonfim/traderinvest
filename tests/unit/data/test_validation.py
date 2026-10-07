from datetime import UTC, date, datetime

import numpy as np
import pandas as pd
import pytest

from app.data.calendar import TradingCalendar
from app.data.normalization import normalize_daily
from app.data.validation import ValidationResult, validate_daily
from tests.factories import AFTER_JULY_2024, Row, provider_bars, regular_rows, row

START, END = date(2024, 7, 1), date(2024, 7, 9)


@pytest.fixture(scope="module")
def xnys() -> TradingCalendar:
    return TradingCalendar("XNYS")


def run(
    xnys: TradingCalendar,
    rows: dict[str, Row] | None = None,
    frame: pd.DataFrame | None = None,
    as_of: datetime = AFTER_JULY_2024,
) -> ValidationResult:
    if frame is None:
        frame = normalize_daily(provider_bars(rows or regular_rows()), xnys).frame
    return validate_daily(frame, xnys.sessions(START, END), as_of=as_of, requested_end=END)


def checks(result: ValidationResult) -> list[tuple[str, str]]:
    return sorted((i.check_name, i.action_taken) for i in result.issues)


def base(xnys: TradingCalendar) -> pd.DataFrame:
    return normalize_daily(provider_bars(regular_rows()), xnys).frame.copy()


def test_clean_data_produces_no_issues(xnys: TradingCalendar) -> None:
    result = run(xnys)
    assert result.issues == []
    assert len(result.valid) == 6
    assert result.valid["is_closed"].all()


def test_empty_data_is_reported(xnys: TradingCalendar) -> None:
    result = run(xnys, frame=base(xnys).iloc[:0])
    assert checks(result) == [("no_data", "recorded_only")]


@pytest.mark.parametrize(
    ("column", "value", "check"),
    [
        ("close", np.nan, "null_value"),
        ("volume", np.nan, "null_value"),
        ("low", 0.0, "non_positive_price"),
        ("open", -5.0, "non_positive_price"),
        ("adj_close", -1.0, "non_positive_adj_close"),
        ("high", 50.0, "ohlc_inconsistent"),  # high below open/close
        ("low", 500.0, "ohlc_inconsistent"),  # low above open/close
        ("volume", -1.0, "negative_volume"),
    ],
)
def test_invalid_bars_are_excluded_and_reported(
    xnys: TradingCalendar, column: str, value: float, check: str
) -> None:
    frame = base(xnys)
    frame.iloc[2, frame.columns.get_loc(column)] = value
    result = run(xnys, frame=frame)
    assert (check, "excluded") in checks(result)
    assert result.excluded_count == 1
    assert len(result.valid) == 5
    bad_ts = frame.index[2]
    assert bad_ts not in result.valid.index
    # The excluded bar must not ALSO be reported as a missing session.
    assert "missing_session" not in {c for c, _ in checks(result)}
    issue = next(i for i in result.issues if i.check_name == check)
    assert issue.bar_ts == bad_ts.to_pydatetime()
    assert issue.severity == "error"


def test_identical_duplicates_collapse_to_one(xnys: TradingCalendar) -> None:
    frame = base(xnys)
    frame = pd.concat([frame, frame.iloc[[1]]])
    result = run(xnys, frame=frame)
    assert checks(result) == [("duplicate_bar", "deduplicated_identical")]
    assert len(result.valid) == 6


def test_conflicting_duplicates_are_all_excluded(xnys: TradingCalendar) -> None:
    frame = base(xnys)
    dup = frame.iloc[[1]].copy()
    dup["close"] += 0.5
    result = run(xnys, frame=pd.concat([frame, dup]))
    assert ("conflicting_duplicate_bar", "excluded") in checks(result)
    assert len(result.valid) == 5
    assert result.excluded_count == 2


def test_future_bars_are_excluded(xnys: TradingCalendar) -> None:
    as_of = datetime(2024, 7, 5, 15, 0, tzinfo=UTC)  # during the 07-05 session
    result = run(xnys, as_of=as_of)
    assert checks(result).count(("future_bar", "excluded")) == 2  # 07-08, 07-09
    assert result.valid.index.max() <= as_of


def test_forming_bar_is_kept_but_not_closed(xnys: TradingCalendar) -> None:
    as_of = datetime(2024, 7, 5, 15, 0, tzinfo=UTC)
    result = run(xnys, as_of=as_of)
    closed = result.valid["is_closed"]
    assert not closed.iloc[-1]  # 07-05 session still open at as_of
    assert closed.iloc[:-1].all()


def test_early_close_bar_is_closed_after_1pm_et(xnys: TradingCalendar) -> None:
    as_of = datetime(2024, 7, 3, 17, 30, tzinfo=UTC)  # 13:30 ET, after the early close
    result = run(xnys, as_of=as_of)
    assert result.valid.loc[result.valid["session_date"] == date(2024, 7, 3), "is_closed"].item()


def test_missing_sessions_are_reported(xnys: TradingCalendar) -> None:
    rows = regular_rows()
    del rows["2024-07-03"], rows["2024-07-09"]  # one interior, one trailing
    result = run(xnys, rows=rows)
    missing = sorted(i.details["session_date"] for i in result.issues if i.check_name == "missing_session")
    assert missing == ["2024-07-03", "2024-07-09"]
    assert all(i.action_taken == "recorded_only" for i in result.issues)


def test_trailing_session_not_yet_closed_is_not_missing(xnys: TradingCalendar) -> None:
    rows = regular_rows()
    del rows["2024-07-09"]
    result = run(xnys, rows=rows, as_of=datetime(2024, 7, 9, 15, 0, tzinfo=UTC))
    assert "missing_session" not in {c for c, _ in checks(result)}


def test_large_moves_are_flagged_not_removed(xnys: TradingCalendar) -> None:
    rows = regular_rows()
    rows["2024-07-05"] = (80.0, 81.0, 79.0, 80.0, 78.4, 1_000_000)  # -22% gap and return
    result = run(xnys, rows=rows)
    assert ("return_outlier", "kept_flagged") in checks(result)
    assert ("gap_outlier", "kept_flagged") in checks(result)
    assert len(result.valid) == 6


def test_zero_volume_and_missing_adj_close_are_flagged(xnys: TradingCalendar) -> None:
    rows = regular_rows()
    rows["2024-07-02"] = row(101, volume=0)
    rows["2024-07-08"] = (104.0, 105.0, 103.0, 104.0, float("nan"), 1_000_000)
    result = run(xnys, rows=rows)
    assert checks(result) == [("missing_adj_close", "kept_flagged"), ("zero_volume", "kept_flagged")]
    assert len(result.valid) == 6


def test_split_is_flagged(xnys: TradingCalendar) -> None:
    frame = normalize_daily(provider_bars(regular_rows(), splits={"2024-07-08": 2.0}), xnys).frame
    result = run(xnys, frame=frame)
    issue = next(i for i in result.issues if i.check_name == "split_detected")
    assert issue.details["split_ratio"] == 2.0


def test_adjustment_factor_going_backwards_is_flagged(xnys: TradingCalendar) -> None:
    rows = regular_rows()
    rows["2024-07-08"] = row(104, factor=0.90)  # factor falls from 0.98 to 0.90
    result = run(xnys, rows=rows)
    assert ("adj_factor_decrease", "kept_flagged") in checks(result)


def test_naive_as_of_is_rejected(xnys: TradingCalendar) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        run(xnys, as_of=datetime(2024, 8, 1))  # noqa: DTZ001


def test_validation_is_deterministic(xnys: TradingCalendar) -> None:
    rows = regular_rows()
    rows["2024-07-05"] = (80.0, 81.0, 79.0, 80.0, 78.4, 0)
    a, b = run(xnys, rows=rows), run(xnys, rows=rows)
    assert a.issues == b.issues
    pd.testing.assert_frame_equal(a.valid, b.valid)
