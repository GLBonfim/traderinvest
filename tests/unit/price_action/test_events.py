"""Breakout/breakdown, retest, rejection, sweep and outcome semantics on synthetic scenarios.

BASE ends with a confirmed resistance zone at exactly 110 (pivot bar 4, confirmed bar 6) and
the last close at 103. Events are filtered to that zone.
"""

import dataclasses

import pandas as pd
import pytest

from app.price_action.engine import EVENT_COLUMNS, PriceActionAnalysis, PriceActionEngine
from tests.unit.candles.helpers import Candle, bars
from tests.unit.price_action.helpers import BASE, FAST, mirror

BREAKOUT: Candle = (103, 112, 102.5, 111)  # closes 0.909% above 110


def run(extra: list[Candle], cfg=FAST, base=BASE) -> PriceActionAnalysis:  # type: ignore[no-untyped-def]
    return PriceActionEngine(cfg).analyze(bars([*base, *extra]))


def zone_events(
    a: PriceActionAnalysis, level: float = 110.0, kind: str | None = None
) -> pd.DataFrame:
    e = a.events[(a.events["reference_low"] <= level) & (a.events["reference_high"] >= level)]
    return e if kind is None else e[e["event_type"] == kind]


# ── breakout / breakdown ──


def test_close_above_resistance_is_breakout() -> None:
    a = run([BREAKOUT])
    e = zone_events(a, kind="breakout")
    assert len(e) == 1
    row = e.iloc[0]
    assert row["ts"] == a.state.index[8] and row["available_at"] == row["ts"]
    assert row["direction"] == "up" and row["confirmation_type"] == "close"
    assert row["reference_level"] == 110.0
    assert row["distance_pct"] == pytest.approx(111 / 110 - 1)


def test_wick_only_break_is_not_breakout_but_sweep() -> None:
    a = run([(103, 112, 102.5, 109.5)])
    assert zone_events(a, kind="breakout").empty
    sweep = zone_events(a, kind="sweep")
    assert len(sweep) == 1 and sweep.iloc[0]["direction"] == "up"
    assert sweep.iloc[0]["distance_pct"] == pytest.approx(112 / 110 - 1)


@pytest.mark.parametrize(("close", "expected"), [(110.11, 1), (110.109, 0)])
def test_breakout_distance_boundary(close: float, expected: int) -> None:
    # breakout_min_distance_pct = 0.1%: 110.11 / 110 - 1 = 0.001 exactly -> included
    a = run([(103, 111, 102.5, close)])
    assert len(zone_events(a, kind="breakout")) == expected


def test_no_breakout_when_previous_close_already_above() -> None:
    a = run([BREAKOUT, (111, 113, 110.5, 112)])
    assert len(zone_events(a, kind="breakout")) == 1  # only the crossing bar


def test_breakdown_is_mirror_of_breakout() -> None:
    a = run(mirror([BREAKOUT]), base=mirror(BASE))  # support zone at 90, close at 89
    e = zone_events(a, level=90.0, kind="breakdown")
    assert len(e) == 1
    assert e.iloc[0]["direction"] == "down"
    assert e.iloc[0]["distance_pct"] == pytest.approx(89 / 90 - 1)


def test_zone_not_used_before_its_confirmation() -> None:
    early = BASE[:6]  # the 110 pivot is not confirmed until bar 6
    a = PriceActionEngine(FAST).analyze(bars([*early, BREAKOUT]))
    assert zone_events(a, kind="breakout").empty


# ── retest ──


def test_valid_retest_after_breakout() -> None:
    a = run([BREAKOUT, (111, 112, 110.1, 111.5)])
    r = zone_events(a, kind="retest")
    assert len(r) == 1
    brk = zone_events(a, kind="breakout").iloc[0]
    assert r.iloc[0]["related_event_id"] == brk["event_id"]
    assert r.iloc[0]["ts"] == a.state.index[9]


def test_no_retest_when_price_stays_away() -> None:
    assert zone_events(run([BREAKOUT, (111, 112, 110.5, 111.5)]), kind="retest").empty


@pytest.mark.parametrize(("low", "expected"), [(110.22, 1), (110.2201, 0)])
def test_retest_tolerance_boundary(low: float, expected: int) -> None:
    # retest_tolerance_pct = 0.2%: 110.22 / 110 - 1 = 0.002 exactly -> included
    assert len(zone_events(run([BREAKOUT, (111, 112, low, 111.5)]), kind="retest")) == expected


@pytest.mark.parametrize(("bars_away", "expected"), [(2, 1), (3, 0)])
def test_retest_window_boundary(bars_away: int, expected: int) -> None:
    cfg = dataclasses.replace(FAST, retest_window_bars=3, outcome_window_bars=10)
    away: list[Candle] = [(111, 113, 111.5, 112)] * bars_away
    a = run([BREAKOUT, *away, (112, 112.5, 110.1, 111.8)], cfg=cfg)  # touch at age bars_away+1
    assert len(zone_events(a, kind="retest")) == expected


def test_breakout_then_immediate_reversal_is_not_a_retest() -> None:
    a = run([BREAKOUT, (111, 111.2, 108, 108.5)])
    assert zone_events(a, kind="retest").empty
    out = a.outcomes[a.outcomes["event_type"] == "breakout"]
    assert out.iloc[0]["outcome"] == "failed"


# ── rejection ──


def test_valid_rejection_from_resistance() -> None:
    a = run([(103, 112, 102.9, 104)])  # into the zone, closes far below, upper wick 88%
    r = zone_events(a, kind="rejection")
    assert len(r) == 1 and r.iloc[0]["direction"] == "down"
    assert r.iloc[0]["wick_ratio"] == pytest.approx(8 / 9.1)


def test_rejection_insufficient_wick() -> None:
    assert zone_events(run([(103, 112, 100, 109)]), kind="rejection").empty  # wick 25%


def test_rejection_insufficient_excursion() -> None:
    assert zone_events(run([(103, 109.9, 102.9, 104)]), kind="rejection").empty  # never reaches 110


def test_rejection_exact_thresholds() -> None:
    cfg = dataclasses.replace(FAST, rejection_min_distance_pct=0.05)
    # upper wick (110 - 104.76...) / range: build wick ratio 0.5 and distance exactly 5%
    close = 110 / 1.05
    candle: Candle = (close, 110.0, close - (110.0 - close), close)  # wick = range/2
    a = run([candle], cfg=cfg)
    assert len(zone_events(a, kind="rejection")) == 1
    tighter = dataclasses.replace(cfg, rejection_min_distance_pct=0.0501)
    assert zone_events(run([candle], cfg=tighter), kind="rejection").empty


def test_support_rejection_is_mirror() -> None:
    a = run(mirror([(103, 112, 102.9, 104)]), base=mirror(BASE))
    r = zone_events(a, level=90.0, kind="rejection")
    assert len(r) == 1 and r.iloc[0]["direction"] == "up"


# ── outcomes: separate from observations, never known at event time ──


def test_failed_breakout_outcome_is_timestamped_later() -> None:
    later: list[Candle] = [(111, 112, 110.5, 111.5), (111.5, 112, 110.4, 111), (111, 111, 108, 109)]
    a = run([BREAKOUT, *later])
    brk = zone_events(a, kind="breakout").iloc[0]
    out = a.outcomes.set_index("event_id").loc[brk["event_id"]]
    assert out["outcome"] == "failed"
    assert out["outcome_at"] == a.state.index[11]
    assert out["bars_to_outcome"] == 3
    assert out["outcome_at"] > brk["ts"]


def test_outcome_is_pending_before_it_is_known() -> None:
    later: list[Candle] = [(111, 112, 110.5, 111.5), (111.5, 112, 110.4, 111), (111, 111, 108, 109)]
    full = run([BREAKOUT, *later])
    for cut in (9, 10, 11):  # analyses ending at bars 8, 9, 10
        partial = PriceActionEngine(FAST).analyze(full_bars(BREAKOUT, later).iloc[:cut])
        brk = zone_events(partial, kind="breakout").iloc[0]
        out = partial.outcomes.set_index("event_id").loc[brk["event_id"]]
        assert out["outcome"] == "pending" and pd.isna(out["outcome_at"])
        # The breakout observation itself is identical in every analysis.
        pd.testing.assert_series_equal(brk, zone_events(full, kind="breakout").iloc[0])


def full_bars(first: Candle, rest: list[Candle]) -> pd.DataFrame:
    return bars([*BASE, first, *rest])


def test_held_outcome_after_window() -> None:
    cfg = dataclasses.replace(FAST, outcome_window_bars=3)
    stay: list[Candle] = [(111, 113, 110.5, 112)] * 3
    a = run([BREAKOUT, *stay], cfg=cfg)
    brk = zone_events(a, kind="breakout").iloc[0]
    out = a.outcomes.set_index("event_id").loc[brk["event_id"]]
    assert (out["outcome"], out["bars_to_outcome"]) == ("held", 3)


def test_events_table_has_no_outcome_or_trading_fields() -> None:
    forbidden = ("outcome", "failed", "signal", "buy", "sell", "entry", "stop", "target", "pnl")
    assert not [c for c in EVENT_COLUMNS if any(f in c for f in forbidden)]
