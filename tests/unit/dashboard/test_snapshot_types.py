"""Display-only session types of the dashboard's "Account snapshots" table (D1, ADR-0035).

Trades come only from fill events of the same session; a window-start reference snapshot is
shown as indicative (no record marks it); anything the data cannot separate is
indeterminate. Nothing is persisted. Synthetic data and `tmp_path` only.
"""

import copy
from pathlib import Path
from typing import Any

import pytest

from app.dashboard.services import paper as svc
from app.paper.config import PaperConfig
from app.paper.store import PaperStore
from tests.unit.paper.test_window_start import COSTS, F, K, L, day, inputs, ramp
from tests.unit.risk.test_adr0034_baseline import ATR3

TRADE, NO_TRADE = svc.SNAPSHOT_TRADE, svc.SNAPSHOT_NO_TRADE
REFERENCE, INDETERMINATE = svc.SNAPSHOT_REFERENCE, svc.SNAPSHOT_INDETERMINATE
CAPITAL = COSTS.initial_capital


def store(root: Path) -> PaperStore:
    return PaperStore(
        PaperConfig(risk=ATR3, backtest=COSTS),
        strategy_id="x",
        strategy_version="1.0.0",
        root=root,
    )


def types(s: PaperStore) -> list[str]:
    """Session type of every committed snapshot, oldest first."""
    got = svc.classify_snapshots(s.events(), s.trader.state.session_seq, CAPITAL)
    return [got[e["seq"]][0] for e in s.events() if e["event_type"] == "snapshot"]


def snapshot_sessions(s: PaperStore) -> list[str]:
    return [e["session"] for e in s.events() if e["event_type"] == "snapshot"]


# ── reference snapshot (window start) ──


def test_window_start_reference_is_indicative_then_trade_then_no_trade(tmp_path: Path) -> None:
    bars = ramp()
    s = store(tmp_path)
    s.process_many(inputs(bars, [L] * 12), start=day(bars, K))
    got = types(s)
    assert got == [REFERENCE, TRADE] + [NO_TRADE] * (len(bars) - K - 1)
    assert snapshot_sessions(s)[0] == bars.index[K - 1].isoformat()
    assert "indicative" in REFERENCE


def test_reference_alone_before_the_window_session_exists(tmp_path: Path) -> None:
    bars = ramp()
    s = store(tmp_path)
    s.process_many(inputs(bars, [L] * 12)[:K], start=day(bars, K))
    assert types(s) == [REFERENCE]


def test_reference_basis_states_the_limitation(tmp_path: Path) -> None:
    bars = ramp()
    s = store(tmp_path)
    s.process_many(inputs(bars, [L] * 12), start=day(bars, K))
    first = next(e for e in s.events() if e["event_type"] == "snapshot")
    _, basis = svc.classify_snapshots(s.events(), s.trader.state.session_seq, CAPITAL)[first["seq"]]
    assert basis.startswith("indicative, not recorded")
    assert "no record field marks a reference" in basis


# ── normal sessions without a trade ──


def test_normal_account_first_and_later_sessions_without_a_fill(tmp_path: Path) -> None:
    bars = ramp()
    s = store(tmp_path)
    s.process_many(inputs(bars, [F, F, F] + [L] * 9))
    got = types(s)
    assert got[:4] == [NO_TRADE] * 4  # flat sessions, the 4th schedules the entry
    assert got[4] == TRADE  # the entry fill at the open of session 4
    assert got[5:] == [NO_TRADE] * (len(bars) - 5)
    rows = svc.classify_snapshots(s.events(), s.trader.state.session_seq, CAPITAL)
    first = rows[min(rows)]
    assert "indicative" in first[1]  # a counted session is itself only an indication


# ── sessions with a fill ──


def test_trade_only_from_a_fill_in_the_same_session(tmp_path: Path) -> None:
    bars = ramp()
    s = store(tmp_path)
    s.process_many(inputs(bars, [F, F, L, L, L, F, F, L, L, F, F, F]))
    fills = {e["session"] for e in s.events() if e["event_type"] == "fill"}
    got = dict(zip(snapshot_sessions(s), types(s), strict=True))
    assert fills and {k for k, v in got.items() if v == TRADE} == fills


def test_position_change_without_a_fill_event_is_not_a_trade(tmp_path: Path) -> None:
    bars = ramp()
    s = store(tmp_path)
    s.process_many(inputs(bars, [F, L] + [L] * 10))
    events = [e for e in s.events() if e["event_type"] != "fill"]  # positions still change
    got = svc.classify_snapshots(events, s.trader.state.session_seq, CAPITAL)
    assert TRADE not in {v[0] for v in got.values()}


# ── ambiguous cases ──


def test_same_ledger_is_classified_by_the_account_counts_only(tmp_path: Path) -> None:
    """A window-start account and a new account whose first session is start-1 have the same
    ledger (hashes included); only `sessions_processed` differs, hence 'indicative'."""
    bars = ramp()
    ins = inputs(bars, [L] * 12)
    a, b = store(tmp_path / "a"), store(tmp_path / "b")
    a.process_many(ins, start=day(bars, K))
    b.process_many(ins[K - 1 :])
    assert a.events() == b.events()
    assert types(a)[0] == REFERENCE and types(b)[0] == NO_TRADE


@pytest.mark.parametrize("offset", [-2, 1, 5])  # extra snapshots: -1, 2, 6
def test_inconsistent_counts_make_the_first_snapshot_indeterminate(
    tmp_path: Path, offset: int
) -> None:
    bars = ramp()
    s = store(tmp_path)
    s.process_many(inputs(bars, [L] * 12), start=day(bars, K))
    events = s.events()
    n = sum(e["event_type"] == "snapshot" for e in events)
    got = svc.classify_snapshots(events, n - 1 - offset, CAPITAL)
    kinds = [got[e["seq"]][0] for e in events if e["event_type"] == "snapshot"]
    assert kinds[0] == INDETERMINATE
    assert kinds[1:] == [TRADE] + [NO_TRADE] * (n - 2)  # unaffected by the counts


@pytest.mark.parametrize("field", ["position_quantity", "equity"])
def test_one_extra_snapshot_not_flat_at_initial_capital_is_indeterminate(
    tmp_path: Path, field: str
) -> None:
    bars = ramp()
    s = store(tmp_path)
    s.process_many(inputs(bars, [L] * 12), start=day(bars, K))
    events: list[dict[str, Any]] = copy.deepcopy(s.events())
    first = next(e for e in events if e["event_type"] == "snapshot")
    first["record"][field] = first["record"][field] + 1.0
    got = svc.classify_snapshots(events, s.trader.state.session_seq, CAPITAL)
    assert got[first["seq"]][0] == INDETERMINATE


def test_no_snapshots_classifies_nothing() -> None:
    assert svc.classify_snapshots([], 0, CAPITAL) == {}


# ── the table ──


def test_snapshot_table_shows_types_over_the_whole_ledger_and_reads_only(tmp_path: Path) -> None:
    bars = ramp()
    s = store(tmp_path)
    s.process_many(inputs(bars, [L] * 12), start=day(bars, K))
    before = (s.ledger_path.read_bytes(), s.state_path.read_bytes())
    full = svc.ledger_tables(s)
    snap = full["snapshot"]
    assert list(snap.columns[:3]) == ["seq", "event_id", "session_type"]
    assert snap.columns[-1] == "session_type_basis"
    assert snap["session_type"].tolist() == list(reversed(types(s)))  # newest first
    for other in ("decision", "order", "fill", "reconciliation"):
        assert "session_type" not in full[other].columns
    recent = svc.ledger_tables(s, last_n_sessions=3)["snapshot"]
    assert recent["session_type"].tolist() == [NO_TRADE] * 3  # reference not in view
    assert (s.ledger_path.read_bytes(), s.state_path.read_bytes()) == before
    assert "indicative" in svc.SNAPSHOT_TYPES_NOTE and "fill event" in svc.SNAPSHOT_TYPES_NOTE
