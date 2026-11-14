from datetime import datetime, time
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from app.trend.costs import cost_model, ter_adjustment_pct
from app.trend.data import (
    DataError,
    cash_rate_series,
    eur_level,
    interest,
    is_period_end,
    scenario_levels,
    ter_factors,
)
from app.trend.universe import load_universe


def test_ter_table_of_section_8_2() -> None:
    assert ter_adjustment_pct("product_own", 0.2, None, "zero") == 0.2
    assert ter_adjustment_pct("product_own", 0.2, None, "realistic") == 0.0
    assert ter_adjustment_pct("product_own", 0.2, None, "double") == -0.2
    assert ter_adjustment_pct("proxy", 0.07, 0.0945, "zero") == pytest.approx(0.0945)
    assert ter_adjustment_pct("proxy", 0.07, 0.0945, "realistic") == pytest.approx(0.0245)
    assert ter_adjustment_pct("proxy", 0.07, 0.0945, "double") == pytest.approx(-0.0455)
    assert ter_adjustment_pct("index", 0.1, None, "double") == pytest.approx(-0.2)
    with pytest.raises(ValueError):
        ter_adjustment_pct("proxy", 0.07, None, "zero")


def test_ter_accrues_per_calendar_day() -> None:
    idx = pd.DatetimeIndex(["2024-01-05", "2024-01-08", "2024-01-09"])  # Fri, Mon, Tue
    f = ter_factors(idx, -0.365)
    assert f.tolist() == pytest.approx([1.0, 1 - 0.00365 * 3 / 365, 1 - 0.00365 / 365])


def test_scenario_levels_respect_the_ter_difference() -> None:
    idx = pd.bdate_range("2024-01-01", periods=30)
    frame = pd.DataFrame({"close": np.full(30, 100.0), "dividends": np.zeros(30)}, index=idx)
    levels = scenario_levels(frame, None, "proxy", 0.07, 0.40, idx[0])
    days = (idx[-1] - idx[0]).days
    assert levels["realistic"].iloc[0] == 1.0  # normalised at the reference session
    assert levels["realistic"].iloc[-1] > 1.0  # adds back 0.40, deducts 0.07
    assert levels["zero"].iloc[-1] > levels["realistic"].iloc[-1] > levels["double"].iloc[-1]
    approx = 1 + (0.40 - 0.07) / 100 * days / 365
    assert levels["realistic"].iloc[-1] == pytest.approx(approx, rel=1e-6)


def test_cost_model_scenarios() -> None:
    u = load_universe()
    tiers = {a.id: a.cost_class for a in u.all_series()}
    real = cost_model(u.raw, tiers, "realistic")
    assert real.commission(1000.0) == 3.0  # minimum
    assert real.commission(10_000.0) == pytest.approx(5.0)  # 0.05 %
    assert real.side_cost("US_EQ") == pytest.approx(0.0005)  # class A: 0.03 % + 0.02 %
    double = cost_model(u.raw, tiers, "double")
    assert double.commission(1000.0) == 6.0
    assert double.side_cost("GLOBAL_RE") == pytest.approx(0.003)  # class C doubled
    zero = cost_model(u.raw, tiers, "zero")
    assert zero.commission(1000.0) == 0.0 and zero.side_cost("US_EQ") == 0.0


def test_stale_ecb_rate_fails_closed() -> None:
    fx = pd.Series([1.1, 1.2], index=pd.DatetimeIndex(["2024-01-02", "2024-01-03"]))
    ok = pd.Series([120.0, 120.0], index=pd.DatetimeIndex(["2024-01-03", "2024-01-08"]))
    assert eur_level(ok, fx).tolist() == pytest.approx([100.0, 100.0])  # 3 weekdays stale
    late = pd.Series([1.0], index=pd.DatetimeIndex(["2024-01-10"]))  # 5 weekdays
    with pytest.raises(DataError, match="stale"):
        eur_level(late, fx)
    early = pd.Series([1.0], index=pd.DatetimeIndex(["2024-01-01"]))
    with pytest.raises(DataError):
        eur_level(early, fx)


def test_eonia_to_estr_splice_and_haircut() -> None:
    eonia = pd.Series([-0.45, -0.46], index=pd.DatetimeIndex(["2019-09-30", "2019-10-01"]))
    estr = pd.Series([-0.55, -0.54], index=pd.DatetimeIndex(["2019-09-30", "2019-10-01"]))
    raw = {"eonia": {"adjustment_pp": -0.085}, "haircut_pp": -0.25}
    rate = cash_rate_series(eonia, estr, raw)
    assert rate.loc["2019-09-30"] == pytest.approx((-0.45 - 0.085 - 0.25) / 100)
    assert rate.loc["2019-10-01"] == pytest.approx((-0.54 - 0.25) / 100)
    assert len(rate) == 2


def test_interest_uses_previous_day_rate_act_360() -> None:
    rate = pd.Series([0.036, 0.072], index=pd.DatetimeIndex(["2024-01-05", "2024-01-08"]))
    got = interest(1000.0, rate, pd.Timestamp("2024-01-05"), pd.Timestamp("2024-01-08"))
    assert got == pytest.approx(1000 * 0.036 * 3 / 360)
    with pytest.raises(DataError):
        interest(1000.0, rate, pd.Timestamp("2024-01-04"), pd.Timestamp("2024-01-05"))


def test_period_end_uses_the_scheduled_calendar_and_recovers_a_missed_month() -> None:
    scheduled = pd.bdate_range("2024-01-01", "2024-03-29")
    assert is_period_end(pd.Timestamp("2024-01-31"), scheduled, "M", pd.Timestamp("2024-01-30"))
    assert not is_period_end(pd.Timestamp("2024-01-30"), scheduled, "M", None)
    # 2024-02-29 scheduled but missing from the master calendar: the first March day takes it
    assert is_period_end(pd.Timestamp("2024-03-01"), scheduled, "M", pd.Timestamp("2024-02-28"))
    assert not is_period_end(pd.Timestamp("2024-03-04"), scheduled, "M", pd.Timestamp("2024-03-01"))
    assert is_period_end(
        pd.Timestamp("2024-12-31"),
        pd.bdate_range("2024-01-01", "2025-01-31"),
        "Y",
        pd.Timestamp("2024-12-30"),
    )


def test_decision_time_follows_every_input() -> None:
    """Owner condition (b): at 23:00 Lisbon the US close (16:00 New York), the Xetra close
    (17:30 Frankfurt) and the ECB rate (~16:00 Frankfurt) are already published, every day."""
    lisbon, ny, fra = (
        ZoneInfo("Europe/Lisbon"),
        ZoneInfo("America/New_York"),
        ZoneInfo("Europe/Berlin"),
    )
    for day in pd.date_range("2008-01-01", "2026-12-31", freq="D"):
        d = day.date()
        decision = datetime.combine(d, time(23, 0), lisbon)
        for t, zone in ((time(16, 0), ny), (time(17, 30), fra), (time(16, 0), fra)):
            assert datetime.combine(d, t, zone) < decision, (d, t, zone)
