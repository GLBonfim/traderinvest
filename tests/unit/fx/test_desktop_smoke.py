"""Desktop UI smoke tests (offscreen Qt): widgets build, render real snapshots and events, and
the room never conveys state by colour alone."""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from app.desktop.icon import app_icon, icon_image
from app.desktop.main_window import MainWindow
from app.desktop.opsroom import OpsRoom
from app.desktop.panels import money, pct, price
from app.desktop.theme import STATUS
from tests.unit.fx.helpers import orchestrator


@pytest.fixture(scope="module")
def app() -> Iterator[QApplication]:
    inst = QApplication.instance() or QApplication([])
    yield inst  # type: ignore[misc]


@pytest.fixture(scope="module")
def snapshot(tmp_path_factory: pytest.TempPathFactory) -> dict:  # type: ignore[type-arg]
    o = orchestrator(Path(tmp_path_factory.mktemp("fx")))
    for _ in range(120):
        o.tick()
    return o.snapshot()


def test_status_glyphs_differ_in_shape_and_have_labels() -> None:
    glyphs = [g for g, _, _ in STATUS.values()]
    labels = [lbl for _, _, lbl in STATUS.values()]
    assert len(set(glyphs)) == len(glyphs) and all(labels)


def test_formatting() -> None:
    assert money(10234.5, "EUR") == "10 234,50 EUR"
    assert pct(-1.234) == "-1,23%"
    assert price(1.234567, "EURUSD") == "1.23457" and price(150.1234, "USDJPY") == "150.123"


def test_icon(app: QApplication) -> None:
    assert not app_icon().isNull()
    assert icon_image(64).width() == 64


def test_room_renders_snapshots_and_events(app: QApplication, snapshot: dict) -> None:  # type: ignore[type-arg]
    room = OpsRoom()
    room.resize(1000, 600)
    room.apply_snapshot(snapshot)
    for kind, payload in (
        ("cycle", {"signals": 2, "selected": ["EURUSD"]}),
        ("risk_decision", {"approved": True}),
        ("risk_decision", {"approved": False}),
        ("order_sent", {}),
        ("order_result", {"state": "filled", "side": "buy", "pair": "EURUSD"}),
        (
            "position_closed",
            {"reason": "stop_loss", "fill": {"pair": "EURUSD", "realized_pnl": -12.5}},
        ),
        ("kill_switch", {"engaged": True}),
    ):
        room.on_event({"kind": kind, "payload": payload})
    for _ in range(60):
        room._advance()
    img = room.grab().toImage()
    assert img.width() == 1000
    room.set_offline("teste")
    assert all(s.status == "error" for s in room.stations.values())
    snapshot["risk"]["kill_switch"] = True
    room.apply_snapshot(snapshot)
    assert room.alert == "kill" and room.stations["risk"].detail == "KILL SWITCH ATIVO"
    snapshot["risk"]["kill_switch"] = False


def test_main_window_panels_render_without_engine(app: QApplication, snapshot: dict) -> None:  # type: ignore[type-arg]
    w = MainWindow(autostart=False)
    for i in range(w.tabs.count()):
        w.tabs.setCurrentIndex(i)
        w._on_snapshot(snapshot)
    w._on_event(
        {
            "seq": 1,
            "ts": snapshot["now"],
            "kind": "order_sent",
            "payload": {"pair": "EURUSD", "side": "buy", "units": 1000},
        }
    )
    assert "Ordem enviada" in w.log.text.toPlainText()
    assert "Capital" in w.kpi.text()
    w._quitting = True
    w.close()


def test_environment_badge_comes_from_the_validated_snapshot(
    app: QApplication,
    snapshot: dict,  # type: ignore[type-arg]
) -> None:
    from app.desktop.strings import t

    w = MainWindow(autostart=False)
    assert w.badge.text() == t("env_unknown")  # nothing fixed before the engine answers
    w._on_snapshot(snapshot)
    assert snapshot["environment_validated"] and w.badge.text() == t("env_sim")
    w._set_badge({**snapshot, "environment": "demo"})
    assert w.badge.text() == t("env_demo")
    w._on_disconnected()
    assert w.badge.text() == t("env_offline")
    w._quitting = True
    w.close()
