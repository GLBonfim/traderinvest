"""Dialogs: typed confirmations for destructive actions, first-run disclaimer, settings, about."""

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.desktop import APP_NAME, APP_VERSION
from app.desktop.strings import t
from app.desktop.theme import P


def confirm_word(parent: QWidget, title: str, body: str, word: str) -> bool:
    """The action proceeds only if the user types `word` exactly."""
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    lay = QVBoxLayout(dlg)
    lbl = QLabel(body + f"\n\nEscreva {word} para confirmar:")
    lbl.setWordWrap(True)
    lay.addWidget(lbl)
    edit = QLineEdit()
    lay.addWidget(edit)
    bb = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
    )
    ok = bb.button(QDialogButtonBox.StandardButton.Ok)
    ok.setEnabled(False)
    edit.textChanged.connect(lambda s: ok.setEnabled(s.strip() == word))
    bb.accepted.connect(dlg.accept)
    bb.rejected.connect(dlg.reject)
    lay.addWidget(bb)
    dlg.resize(460, 220)
    return dlg.exec() == QDialog.DialogCode.Accepted and edit.text().strip() == word


def disclaimer(parent: QWidget | None) -> bool:
    dlg = QDialog(parent)
    dlg.setWindowTitle(t("disclaimer_title"))
    lay = QVBoxLayout(dlg)
    head = QLabel("▲ " + t("disclaimer_title").upper())
    head.setStyleSheet(f"color: {P.amber}; font-size: 14pt; font-weight: bold;")
    lay.addWidget(head)
    body = QLabel(t("disclaimer_body"))
    body.setWordWrap(True)
    lay.addWidget(body)
    check = QCheckBox("Li e compreendi o aviso")
    lay.addWidget(check)
    bb = QDialogButtonBox()
    accept = bb.addButton(t("disclaimer_accept"), QDialogButtonBox.ButtonRole.AcceptRole)
    bb.addButton(t("disclaimer_reject"), QDialogButtonBox.ButtonRole.RejectRole)
    accept.setEnabled(False)
    check.toggled.connect(accept.setEnabled)
    bb.accepted.connect(dlg.accept)
    bb.rejected.connect(dlg.reject)
    lay.addWidget(bb)
    dlg.resize(560, 380)
    return dlg.exec() == QDialog.DialogCode.Accepted


def about(parent: QWidget) -> None:
    dlg = QDialog(parent)
    dlg.setWindowTitle(f"Sobre o {APP_NAME}")
    lay = QVBoxLayout(dlg)
    text = QLabel(
        f"<h2 style='color:{P.green}'>{APP_NAME}</h2>"
        f"<p>Versão {APP_VERSION} · ambiente SIMULAÇÃO</p>"
        "<p>Plataforma pessoal de Forex com cinco agentes (Tendência, Momentum, Reversão à "
        "Média, Filtro de Regime e Coordenador de Carteira), um orquestrador e um gestor de "
        "risco central.</p>"
        "<p><b>Sem execução com dinheiro real nesta versão.</b> Os dados de SIMULAÇÃO são "
        "sintéticos. Não é aconselhamento financeiro.</p>"
        "<p>Interface construída com Qt for Python (PySide6), licença LGPLv3. "
        "Ver THIRD_PARTY_NOTICES no pacote.</p>"
    )
    text.setWordWrap(True)
    text.setTextFormat(Qt.TextFormat.RichText)
    lay.addWidget(text)
    bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    bb.rejected.connect(dlg.reject)
    lay.addWidget(bb)
    dlg.resize(480, 320)
    dlg.exec()


class SettingsDialog(QDialog):
    """Risk and simulation settings. Account currency and capital change only via a reset."""

    def __init__(self, parent: QWidget, config: dict[str, Any]) -> None:
        super().__init__(parent)
        self.setWindowTitle("Configuração")
        r = config["risk"]
        lay = QVBoxLayout(self)
        risk = QGroupBox("Gestão de risco (aplica-se já; bloqueios ativos mantêm-se)")
        f = QFormLayout(risk)
        self.entries = QSpinBox()
        self.entries.setRange(0, 100)
        self.entries.setValue(int(r["max_entries_per_day"]))
        self.loss = self._dbl(0.1, 50, r["daily_loss_limit_pct"], " %")
        self.target = self._dbl(0.1, 500, r["daily_profit_target_pct"], " %")
        self.per_trade = self._dbl(0.05, 5, r["risk_per_trade_pct"], " %", 2)
        self.leverage = self._dbl(1, 30, r["max_leverage"], " x")
        self.positions = QSpinBox()
        self.positions.setRange(1, 20)
        self.positions.setValue(int(r["max_open_positions"]))
        self.spread = self._dbl(0.1, 50, r["max_spread_pips"], " pips")
        self.score = self._dbl(0, 1, r["min_score"], "", 2)
        self.max_dd = self._dbl(1, 90, r.get("max_drawdown_pct", 20.0), " %")
        self.flatten_loss = QCheckBox("Fechar posições ao atingir a perda diária")
        self.flatten_loss.setChecked(bool(r["flatten_on_daily_loss"]))
        self.flatten_target = QCheckBox("Fechar posições ao atingir o objetivo diário")
        self.flatten_target.setChecked(bool(r["flatten_on_profit_target"]))
        f.addRow("Máx. entradas por dia", self.entries)
        f.addRow("Limite de perda diária", self.loss)
        f.addRow("Objetivo de lucro diário", self.target)
        f.addRow("Risco por operação", self.per_trade)
        f.addRow("Alavancagem máxima", self.leverage)
        f.addRow("Máx. posições abertas", self.positions)
        f.addRow("Spread máximo", self.spread)
        f.addRow("Pontuação mínima (não é probabilidade)", self.score)
        f.addRow("Drawdown máximo desde o pico (kill switch)", self.max_dd)
        f.addRow(self.flatten_loss)
        f.addRow(self.flatten_target)
        lay.addWidget(risk)
        demonstration = QGroupBox("Demonstração com preços sintéticos")
        demo_layout = QVBoxLayout(demonstration)
        self.synthetic_demo = QCheckBox("Demonstrar agentes NÃO APROVADOS na simulação")
        self._simulation = config.get("environment") == "simulation"
        self.synthetic_demo.setChecked(
            self._simulation
            and bool(config.get("simulation", {}).get("allow_unapproved_agents", False))
        )
        self.synthetic_demo.setEnabled(self._simulation)
        self.demo_notice = QLabel(
            "Os agentes não passaram a validação estatística. Esta opção permite que abram "
            "posições apenas com preços sintéticos em SIMULAÇÃO. Demonstra a interface e as "
            "regras de risco; não comprova desempenho em mercados reais."
        )
        self.demo_notice.setWordWrap(True)
        self.demo_notice.setStyleSheet(f"color: {P.amber};")
        demo_layout.addWidget(self.synthetic_demo)
        demo_layout.addWidget(self.demo_notice)
        lay.addWidget(demonstration)
        acct = QGroupBox("Conta simulada (alterar exige reiniciar a conta)")
        g = QFormLayout(acct)
        self.currency = QComboBox()
        self.currency.addItems(["EUR", "USD", "GBP", "CHF", "JPY", "AUD", "CAD"])
        self.currency.setCurrentText(str(config["account_currency"]))
        self.capital = self._dbl(100, 1e9, config["initial_capital"], "", 2)
        self.capital.setSingleStep(1000)
        g.addRow("Moeda base", self.currency)
        g.addRow("Capital inicial", self.capital)
        lay.addWidget(acct)
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._orig = config

    @staticmethod
    def _dbl(lo: float, hi: float, v: float, suffix: str, dec: int = 1) -> QDoubleSpinBox:
        s = QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setDecimals(dec)
        s.setValue(float(v))
        s.setSuffix(suffix)
        return s

    def risk_patch(self) -> dict[str, Any]:
        patch: dict[str, Any] = {
            "risk": {
                "max_entries_per_day": self.entries.value(),
                "daily_loss_limit_pct": self.loss.value(),
                "daily_profit_target_pct": self.target.value(),
                "risk_per_trade_pct": self.per_trade.value(),
                "max_leverage": self.leverage.value(),
                "max_open_positions": self.positions.value(),
                "max_spread_pips": self.spread.value(),
                "min_score": self.score.value(),
                "max_drawdown_pct": self.max_dd.value(),
                "flatten_on_daily_loss": self.flatten_loss.isChecked(),
                "flatten_on_profit_target": self.flatten_target.isChecked(),
            }
        }

        if self._simulation:
            patch["simulation"] = {"allow_unapproved_agents": self.synthetic_demo.isChecked()}
        return patch

    def account_change(self) -> dict[str, Any] | None:
        cur, cap = self.currency.currentText(), self.capital.value()
        if cur != self._orig["account_currency"] or abs(cap - self._orig["initial_capital"]) > 1e-9:
            return {"account_currency": cur, "initial_capital": cap}
        return None
