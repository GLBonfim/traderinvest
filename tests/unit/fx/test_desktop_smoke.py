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
from app.desktop.paper_panel import PaperLabPanel
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


def test_paper_ui_does_not_call_stale_agents_active(app: QApplication, tmp_path: Path) -> None:
    panel = PaperLabPanel(root=tmp_path)
    panel.apply_snapshot(
        {
            "fresh": False,
            "age": 600,
            "journal_ok": True,
            "status": {
                "armed": True,
                "agents": [{"id": "P-one", "role": "reversao", "active": True, "closed_trades": 0}],
            },
        }
    )
    assert panel.state.text().startswith("PAUSADO")
    assert panel.agents.item(0, 1).text() == "Sem confirmação atual"
    panel.apply_snapshot(
        {"fresh": True, "age": 5, "journal_ok": None, "status": {"armed": True, "agents": []}}
    )
    assert panel.state.text().startswith("PAUSADO")
    assert "Entradas de agentes: —" in panel.summary.text()
    panel.deleteLater()


def test_default_paper_screen_does_not_start_simulation_or_another_lab(app: QApplication) -> None:
    from PySide6.QtCore import QProcess

    w = MainWindow(remember_geometry=False)
    assert w.modes.currentIndex() == 0
    w._start_engine_for_mode()
    assert w.supervisor.proc.state() == QProcess.ProcessState.NotRunning
    w._quitting = True
    w.close()


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
            "seq": 10**9,  # a new event (not one already shown from the snapshot)
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


@pytest.mark.parametrize(
    ("approved", "demo", "expected", "forbidden"),
    [
        ([], True, "NÃO APROVADO", "■ APROVADO"),
        ([], False, "EM OBSERVAÇÃO", "■ APROVADO"),
        (["trend"], False, "■ APROVADO", "NÃO APROVADO"),
    ],
)
def test_agents_panel_never_shows_a_demo_agent_as_approved(
    app: QApplication,
    snapshot: dict,  # type: ignore[type-arg]
    approved: list[str],
    demo: bool,
    expected: str,
    forbidden: str,
) -> None:
    from app.desktop.panels import AgentsPanel

    panel = AgentsPanel()
    panel.select("trend")
    panel.update_from(
        {
            **snapshot,
            "approved_agents": approved,
            "executing_agents": ["mean_reversion", "momentum", "trend"],
            "unapproved_demo": demo,
        }
    )
    text = panel.status.text()
    assert expected in text and forbidden not in text
    assert panel.status.wordWrap()


def test_simple_overview_keeps_results_and_separates_tests(
    app: QApplication, tmp_path: Path
) -> None:
    panel = PaperLabPanel(tmp_path)
    snapshot = {
        "fresh": True,
        "journal_ok": True,
        "age": 3,
        "agent_results": {"one": 5.93, "two": 4.65},
        "counts": {"pilot_close": 2},
        "closed_trades": [
            {
                "symbol": "EURGBP",
                "entry": 0.8426,
                "exit": 0.841,
                "pnl": 5.93,
                "reason": "alvo",
                "time": "2026-10-20T11:19:00+00:00",
                "model_id": "one",
            }
        ],
        "tests": [{"ref": "TEST", "pnl": -7.65, "state": "Fechado"}],
        "status": {
            "armed": True,
            "open": {},
            "agents": [{"id": "one", "role": "reversao", "active": True, "closed_trades": 1}],
        },
    }
    panel.apply_snapshot(snapshot)
    assert panel.kpis["pnl"].text() == "+10.58 €"
    assert panel.closed.rowCount() == 1 and panel.tests.rowCount() == 1
    assert panel.agents.item(0, 4).text() == "+5.93"
    assert panel.tabs.tabText(0) == "Visão geral"
    assert "Nenhuma operação aberta" in panel.next_step.text()
    assert snapshot["agent_results"] == {"one": 5.93, "two": 4.65}
    panel.apply_snapshot({**snapshot, "fresh": False})
    assert panel.state.text().startswith("PAUSADO") and "último registro" in panel.next_step.text()
    panel.apply_snapshot({**snapshot, "journal_ok": None})
    assert panel.kpis["pnl"].text() == "—" and panel.closed.rowCount() == 0
    panel.timer.stop()
    panel.deleteLater()


def test_budget_controls_only_queue_request_without_changing_journal(
    app: QApplication, tmp_path: Path
) -> None:
    import json

    (tmp_path / "status.json").write_text("{}")
    (tmp_path / "pilot.sqlite").write_bytes(b"not a database")
    panel = PaperLabPanel(tmp_path)
    panel.capital_input.setValue(500)
    panel.fixed_input.setChecked(True)
    panel.order_input.setValue(1000)
    panel.save_budget()
    request = json.loads((tmp_path / "budget-request.json").read_text())
    assert request["capital_eur"] == 500 and request["order_eur"] == 1000
    assert request["fixed_order"] is True
    assert len(request["request_id"]) == 32
    assert (tmp_path / "pilot.sqlite").read_bytes() == b"not a database"
    assert "Ainda não aplicado" in panel.budget_message.text()
    panel.timer.stop()
    panel.deleteLater()
