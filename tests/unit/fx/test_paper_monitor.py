"""Monitor never trades, does not mutate sources, and separates demo tests from agents."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.desktop.paper_monitor import pair_reason, read_monitor
from app.fx.store import FxStore

NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)


def setup(root: Path) -> FxStore:
    root.mkdir()
    (root / "status.json").write_text(
        json.dumps(
            {
                "observed_at": NOW.isoformat(),
                "state": "observing",
                "armed": True,
                "open": {},
                "pending": [],
                "agents": [],
                "risk": {"entries_used": 2, "entries_max": 5},
            }
        ),
        encoding="utf-8",
    )
    return FxStore(root / "pilot.sqlite")


def test_two_manual_tests_are_not_agent_performance_and_source_unchanged(tmp_path: Path) -> None:
    root = tmp_path / "lab"
    store = setup(root)
    for number in (1, 2):
        ref = f"USER-DEMO-{number}"
        store.append("user_demo_test_intent", {"ref": ref})
        store.append(
            "user_demo_test_filled",
            {
                "client_order_id": ref,
                "pair": "EURUSD",
                "avg_price": 1.12,
                "side": "sell",
                "filled_units": 1000,
            },
        )
        store.append(
            "user_demo_test_closed",
            {
                "ref": ref,
                "net_eur_estimate": -3.8,
                "close": {
                    "client_order_id": ref + ":close",
                    "pair": "EURUSD",
                    "avg_price": 1.1202,
                    "side": "buy",
                    "filled_units": 1000,
                },
            },
        )
    before = hashlib.sha256((root / "pilot.sqlite-wal").read_bytes()).hexdigest()
    snap = read_monitor(root, NOW, tmp_path / "cache")
    assert snap["fresh"] and snap["journal_ok"]
    assert len(snap["tests"]) == 2 and len(snap["orders"]) == 4
    assert all(o["origin"] == "Teste de integração" for o in snap["orders"])
    assert snap["counts"].get("pilot_order_filled", 0) == 0
    assert hashlib.sha256((root / "pilot.sqlite-wal").read_bytes()).hexdigest() == before
    store.close()


def test_agent_closed_quantity_is_recovered_without_inventing_zero(tmp_path: Path) -> None:
    root = tmp_path / "lab"
    store = setup(root)
    store.append(
        "pilot_order_filled", {"coid": "P-agent", "symbol": "EURUSD", "units": -1000, "price": 1.12}
    )
    store.append("pilot_close", {"entry_id": "P-agent", "symbol": "EURUSD", "exit_price": 1.121})
    snap = read_monitor(root, NOW, tmp_path / "cache")
    close = next(o for o in snap["orders"] if o["state"] == "Fechada")
    assert close["units"] == 1000 and close["side"] == "buy"
    store.close()


def test_stale_or_future_status_is_not_live(tmp_path: Path) -> None:
    root = tmp_path / "lab"
    store = setup(root)
    assert not read_monitor(root, NOW + timedelta(minutes=4), tmp_path / "cache")["fresh"]
    assert not read_monitor(root, NOW - timedelta(minutes=4), tmp_path / "cache")["fresh"]
    store.close()


def test_missing_or_corrupt_journal_does_not_report_verified(tmp_path: Path) -> None:
    root = tmp_path / "lab"
    store = setup(root)
    store.append("event", {"a": 1})
    store.db.execute("UPDATE journal SET payload='{}'")
    snap = read_monitor(root, NOW, tmp_path / "cache")
    assert not snap["journal_ok"] and snap["orders"] == []
    assert "journal_error" in snap
    store.close()
    assert not read_monitor(tmp_path / "absent", NOW, tmp_path / "cache")["fresh"]


def test_decision_reasons_do_not_misrepresent_a_pattern_as_a_fill() -> None:
    assert "vetou" in pair_reason({"tradable": False}, True)
    assert "Sem padrão" in pair_reason({"tradable": True, "patterns": []}, True)
    assert "BUY bloqueadas" in pair_reason({"tradable": True, "patterns": [{"direction": 1}]}, True)
    assert "depende" in pair_reason({"tradable": True, "patterns": [{"direction": -1}]}, True)
