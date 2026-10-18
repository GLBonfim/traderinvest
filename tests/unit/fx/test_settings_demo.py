"""The synthetic demonstration is explicit, reversible, and confined to simulation."""

import os
from collections.abc import Iterator

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication, QWidget

from app.desktop.dialogs import SettingsDialog
from app.fx.config import FxConfig, merge


@pytest.fixture(scope="module")
def app() -> Iterator[QApplication]:
    instance = QApplication.instance() or QApplication([])
    yield instance  # type: ignore[misc]


def test_demo_defaults_off_and_preserves_other_settings(app: QApplication) -> None:
    cfg = FxConfig()
    parent = QWidget()
    dialog = SettingsDialog(parent, cfg.model_dump(mode="json"))
    assert dialog.synthetic_demo.isEnabled() and not dialog.synthetic_demo.isChecked()
    assert "NÃO APROVADOS" in dialog.synthetic_demo.text()
    assert "preços sintéticos" in dialog.demo_notice.text()
    assert dialog.demo_notice.wordWrap()
    assert merge(cfg, dialog.risk_patch()) == cfg
    assert dialog.account_change() is None


def test_demo_can_be_enabled_and_disabled_without_reset(app: QApplication) -> None:
    cfg = FxConfig()
    parent = QWidget()
    dialog = SettingsDialog(parent, cfg.model_dump(mode="json"))
    dialog.synthetic_demo.setChecked(True)
    updated = merge(cfg, dialog.risk_patch())
    assert updated.simulation.allow_unapproved_agents is True
    assert updated.risk == cfg.risk and updated.initial_capital == cfg.initial_capital
    assert dialog.account_change() is None
    reopened = SettingsDialog(parent, updated.model_dump(mode="json"))
    assert reopened.synthetic_demo.isChecked()
    reopened.synthetic_demo.setChecked(False)
    assert merge(updated, reopened.risk_patch()).simulation.allow_unapproved_agents is False


@pytest.mark.parametrize("environment", ["demo", "real"])
def test_demo_control_is_disabled_outside_simulation(
    app: QApplication,
    environment: str,
) -> None:
    config = FxConfig().model_dump(mode="json")
    config["environment"] = environment
    config["simulation"]["allow_unapproved_agents"] = True
    parent = QWidget()
    dialog = SettingsDialog(parent, config)
    assert not dialog.synthetic_demo.isEnabled() and not dialog.synthetic_demo.isChecked()
    # Even programmatic changes cannot make this dialog emit the override outside simulation.
    dialog.synthetic_demo.setChecked(True)
    assert "simulation" not in dialog.risk_patch()
