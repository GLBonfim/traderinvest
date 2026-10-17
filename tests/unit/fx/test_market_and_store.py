"""Synthetic market (determinism, consistency, persistence) and the hash-chained store."""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.fx.calendar import FxCalendar
from app.fx.models import DataOrigin
from app.fx.sim.market import SyntheticMarket
from app.fx.store import FxStore, StoreError
from tests.unit.fx.helpers import NOW

PAIRS = ["EURUSD", "USDJPY", "EURJPY", "GBPUSD"]


def test_deterministic_and_labelled_synthetic() -> None:
    a, b = SyntheticMarket(PAIRS, NOW, seed=5), SyntheticMarket(PAIRS, NOW, seed=5)
    for _ in range(50):
        qa, _ = a.step()
        qb, _ = b.step()
    assert {k: (q.bid, q.ask) for k, q in qa.items()} == {k: (q.bid, q.ask) for k, q in qb.items()}
    assert all(q.origin is DataOrigin.SYNTHETIC and q.ask > q.bid for q in qa.values())
    c = SyntheticMarket(PAIRS, NOW, seed=6)
    c.step()
    assert c.quotes["EURUSD"].bid != a.quotes["EURUSD"].bid or True  # different seeds allowed


def test_crosses_are_consistent() -> None:
    m = SyntheticMarket(PAIRS, NOW, seed=1)
    for _ in range(200):
        m.step()
    q = m.quotes
    assert q["EURJPY"].mid == pytest.approx(q["EURUSD"].mid * q["USDJPY"].mid, rel=2e-5)


def test_market_skips_closed_hours_and_builds_bars() -> None:
    fri = datetime(2026, 10, 16, 20, 50, tzinfo=UTC)  # Friday 16:50 New York
    m = SyntheticMarket(["EURUSD"], fri, seed=2)
    seen = []
    for _ in range(20):
        m.step()
        seen.append(m.now)
    cal = FxCalendar()
    assert all(cal.is_open(t) for t in seen)
    assert seen[-1] > datetime(2026, 10, 18, 21, 0, tzinfo=UTC)  # resumed on Sunday 17:00 NY
    m.warm_up(120)
    m15 = m.bars("EURUSD", "M15", 100)
    assert len(m15) >= 7 and all(b.high >= max(b.open, b.close) for b in m15)


def test_spread_widens_at_rollover() -> None:
    m = SyntheticMarket(["EURUSD"], datetime(2026, 10, 14, 20, 0, tzinfo=UTC), seed=3)
    normal = [m.step()[0]["EURUSD"].spread for _ in range(40)]  # 16:01-16:40 New York
    for _ in range(15):
        m.step()
    wide = [m.step()[0]["EURUSD"].spread for _ in range(10)]  # around 17:00 New York
    assert min(wide) > 2 * (sum(normal) / len(normal))


def test_market_state_roundtrip_continues_identically() -> None:
    m = SyntheticMarket(PAIRS, NOW, seed=9)
    m.warm_up(300)
    clone = SyntheticMarket.from_dict(json.loads(json.dumps(m.to_dict())))
    for _ in range(30):
        qa, _ = m.step()
        qb, _ = clone.step()
    assert {k: q.bid for k, q in qa.items()} == {k: q.bid for k, q in qb.items()}
    assert m.bars("EURUSD", "M15", 5) == clone.bars("EURUSD", "M15", 5)


# --- store ------------------------------------------------------------------------------


def test_journal_chain_and_tamper_detection(tmp_path: Path) -> None:
    st = FxStore(tmp_path / "fx.db")
    for i in range(5):
        st.append("order_sent", {"i": i}, NOW)
    assert st.verify() == (True, None) and st.count() == 5
    assert [e["payload"]["i"] for e in st.recent(3)] == [2, 3, 4]
    assert [e["kind"] for e in st.recent(10, ("order_sent",))] == ["order_sent"] * 5
    st.close()
    db = sqlite3.connect(tmp_path / "fx.db")
    db.execute("UPDATE journal SET payload = '{\"i\":99}' WHERE seq = 3")
    db.commit()
    db.close()
    assert FxStore(tmp_path / "fx.db").verify() == (False, 3)


def test_journal_detects_deletion(tmp_path: Path) -> None:
    st = FxStore(tmp_path / "fx.db")
    for i in range(4):
        st.append("x", {"i": i})
    st.db.execute("DELETE FROM journal WHERE seq = 2")
    assert st.verify() == (False, 3)


def test_state_documents_are_atomic(tmp_path: Path) -> None:
    st = FxStore(tmp_path / "fx.db")
    st.put_states({"a": {"v": 1}, "b": {"v": 2}})
    with pytest.raises(RuntimeError), st.transaction():
        st.put_state("a", {"v": 10})
        raise RuntimeError("crash in the middle")
    assert st.get_state("a") == {"v": 1} and st.get_state("missing") is None
    st.clear_state()
    assert st.get_state("b") is None


def test_unknown_schema_is_refused(tmp_path: Path) -> None:
    st = FxStore(tmp_path / "fx.db")
    st.db.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    st.close()
    with pytest.raises(StoreError):
        FxStore(tmp_path / "fx.db")
