"""Main window: header (environment, clocks, controls), the operations room and the panels.

Window-close policy (ADR-0037): closing the window asks for confirmation, then stops the engine
cleanly (state saved, positions kept in the simulated account with their stops, recovered on
the next start). F11 toggles full screen; the window is freely resizable.
"""

from typing import Any

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.desktop import APP_ID, ORG_NAME
from app.desktop.dialogs import SettingsDialog, about, confirm_word
from app.desktop.engine_client import EngineClient, EngineSupervisor
from app.desktop.icon import app_icon
from app.desktop.opsroom import OpsRoom
from app.desktop.panels import (
    AgentsPanel,
    LogPanel,
    MarketPanel,
    OrdersPanel,
    PortfolioPanel,
    RiskPanel,
    money,
    pct,
)
from app.desktop.strings import t
from app.desktop.theme import P, mono_font
from app.engine.protocol import CONFIRM_FLATTEN, CONFIRM_KILL_RELEASE, CONFIRM_RESET
from app.fx.config import data_dir

SPEEDS = [("1x  (1 min/s)", 1.0), ("2x", 2.0), ("5x", 5.0), ("10x", 10.0), ("20x", 20.0)]


class MainWindow(QMainWindow):
    def __init__(self, autostart: bool = True, remember_geometry: bool = True) -> None:
        super().__init__()
        self.setWindowTitle(t("window_title"))
        self.setWindowIcon(app_icon())
        self.resize(1600, 920)
        self.setMinimumSize(1100, 680)
        self.snap: dict[str, Any] = {}
        self.client = EngineClient(self)
        self.supervisor = EngineSupervisor(self.client, self)
        self._quitting = False
        self._remember_geometry = remember_geometry
        self._build()
        self._wire()
        if remember_geometry:
            self._restore_geometry()
        if autostart:
            QTimer.singleShot(50, self.supervisor.start)

    # --- layout -----------------------------------------------------------------------------

    def _build(self) -> None:
        root = QWidget()
        v = QVBoxLayout(root)
        v.setContentsMargins(8, 6, 8, 4)
        v.setSpacing(6)
        v.addWidget(self._header())
        split = QSplitter(Qt.Orientation.Horizontal)
        left = QSplitter(Qt.Orientation.Vertical)
        self.room = OpsRoom()
        left.addWidget(self.room)
        self.log = LogPanel()
        left.addWidget(self.log)
        left.setStretchFactor(0, 4)
        left.setStretchFactor(1, 1)
        split.addWidget(left)
        self.tabs = QTabWidget()
        self.market = MarketPanel()
        self.portfolio = PortfolioPanel()
        self.risk = RiskPanel()
        self.agents = AgentsPanel()
        self.orders = OrdersPanel()
        for w, key in (
            (self.market, "tab_market"),
            (self.portfolio, "tab_portfolio"),
            (self.risk, "tab_risk"),
            (self.agents, "tab_agents"),
            (self.orders, "tab_orders"),
        ):
            self.tabs.addTab(w, t(key))
        split.addWidget(self.tabs)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        self.tabs.setMinimumWidth(520)
        self.split = split
        QTimer.singleShot(
            0, lambda: split.setSizes([int(self.width() * 0.6), int(self.width() * 0.4)])
        )
        v.addWidget(split, 1)
        self.setCentralWidget(root)
        self.status_engine = QLabel(t("engine_starting"))
        self.status_beat = QLabel("")
        self.status_journal = QLabel("")
        for lbl in (self.status_engine, self.status_beat, self.status_journal):
            self.statusBar().addPermanentWidget(lbl)
        self._menu()

    def _header(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("Panel")
        rows = QVBoxLayout(bar)
        rows.setContentsMargins(10, 6, 10, 6)
        rows.setSpacing(4)
        h = QHBoxLayout()
        h2 = QHBoxLayout()
        rows.addLayout(h)
        rows.addLayout(h2)
        title = QLabel("◢◤ TRADERINVEST FX")
        title.setFont(mono_font(14, bold=True))
        title.setStyleSheet(f"color: {P.green};")
        h.addWidget(title)
        self.badge = QLabel(t("env_unknown"))  # set from the validated snapshot only
        self.badge.setObjectName("Badge")
        h.addWidget(self.badge)
        self.clock = QLabel("—")
        self.clock.setFont(mono_font(10, bold=True))
        h2.addWidget(self.clock)
        h2.addSpacing(16)
        self.sessions = QLabel("")
        self.sessions.setObjectName("Muted")
        h2.addWidget(self.sessions)
        h2.addStretch(1)
        h.addStretch(1)
        self.kpi = QLabel("")
        self.kpi.setFont(mono_font(11, bold=True))
        h.addWidget(self.kpi)
        h.addSpacing(12)
        self.speed = QComboBox()
        for label, _ in SPEEDS:
            self.speed.addItem(label)
        self.speed.setToolTip("Velocidade da simulação (minutos simulados por segundo)")
        h.addWidget(QLabel(t("speed")))
        h.addWidget(self.speed)
        self.pause_btn = QPushButton(t("pause"))
        h.addWidget(self.pause_btn)
        self.kill_btn = QPushButton(t("kill"))
        self.kill_btn.setObjectName("Kill")
        self.kill_btn.setToolTip(
            "Bloqueia imediatamente todas as novas entradas. As saídas continuam permitidas."
        )
        h.addWidget(self.kill_btn)
        return bar

    def _menu(self) -> None:
        mb = self.menuBar()
        m = mb.addMenu("&Ficheiro")
        settings = QAction("Configuração…", self)
        settings.triggered.connect(self._settings)
        m.addAction(settings)
        verify = QAction("Verificar integridade do registo", self)
        verify.triggered.connect(lambda: self.client.send("journal.verify"))
        m.addAction(verify)
        reset = QAction("Reiniciar conta simulada…", self)
        reset.triggered.connect(lambda: self._reset(None))
        m.addAction(reset)
        m.addSeparator()
        quit_ = QAction("Sair", self)
        quit_.setShortcut(QKeySequence.StandardKey.Quit)
        quit_.triggered.connect(self.close)
        m.addAction(quit_)
        view = mb.addMenu("&Ver")
        full = QAction("Ecrã inteiro", self)
        full.setShortcut(QKeySequence("F11"))
        full.triggered.connect(self._toggle_fullscreen)
        view.addAction(full)
        self.addAction(full)
        helpm = mb.addMenu("&Ajuda")
        ab = QAction("Sobre…", self)
        ab.triggered.connect(lambda: about(self))
        helpm.addAction(ab)

    def _wire(self) -> None:
        c, s = self.client, self.supervisor
        c.snapshot.connect(self._on_snapshot)
        c.engine_event.connect(self._on_event)
        c.ack.connect(self._on_ack)
        c.connected.connect(lambda _m: self._set_engine(t("engine_connected"), P.green, "●"))
        c.disconnected.connect(self._on_disconnected)
        c.failed.connect(lambda e: self._set_engine(f"{t('engine_error')}: {e}", P.red, "✖"))
        s.status.connect(self._on_supervisor)
        s.log_line.connect(lambda line: self.log.text.appendPlainText(f"[motor] {line}"))
        self.pause_btn.clicked.connect(self._toggle_pause)
        self.kill_btn.clicked.connect(self._kill)
        self.speed.currentIndexChanged.connect(
            lambda i: self.client.send("set_speed", value=SPEEDS[i][1])
        )
        self.risk.kill_requested.connect(self._kill)
        self.risk.release_requested.connect(self._release)
        self.risk.flatten_requested.connect(self._flatten)
        self.room.station_clicked.connect(self._on_station)
        self.market.pair_selected.connect(self._on_pair)
        self._beat = QTimer(self)
        self._beat.timeout.connect(self._tick_status)
        self._beat.start(1000)
        self._last_snapshot_ms = 0

    # --- engine events ----------------------------------------------------------------------

    def _set_engine(self, text: str, color: str, glyph: str) -> None:
        self.status_engine.setText(f"{glyph} {text}")
        self.status_engine.setStyleSheet(f"color: {color};")

    def _on_supervisor(self, code: str, detail: str) -> None:
        if code == "starting":
            self._set_engine(t("engine_starting"), P.amber, "▲")
        elif code == "ready":
            self._set_engine(f"{t('engine_connected')} (porta local {detail})", P.green, "●")
        elif code == "stopped" and not self._quitting:
            self._set_engine(f"{t('engine_stopped')} (código {detail})", P.red, "■")
            self.room.set_offline("motor parado")
        elif code == "waiting":
            self._set_engine(detail, P.amber, "▲")
        elif code == "error":
            self._set_engine(f"{t('engine_error')}: {detail}", P.red, "✖")
            self.room.set_offline(detail)
            self._set_badge({})
            if detail.startswith("startup:"):
                code_, _, reason = detail.removeprefix("startup:").partition(" ")
                QMessageBox.critical(
                    self,
                    t("startup_refused_title"),
                    t("startup_refused_body").format(
                        code=code_, reason=reason, folder=str(data_dir())
                    ),
                )
            if "already" in detail:
                QMessageBox.warning(
                    self,
                    t("engine_error"),
                    "Já existe outro motor a usar a mesma pasta de dados. Feche a "
                    "outra instância do TraderInvest FX.",
                )

    def _on_disconnected(self) -> None:
        if not self._quitting:
            self._set_engine(t("engine_disconnected"), P.red, "■")
            self.room.set_offline("desligado")
            self._set_badge({})

    def _set_badge(self, snap: dict[str, Any] | None) -> None:
        """The environment shown is the engine's validated one (E2), never a fixed label."""
        if not snap or not snap.get("environment_validated"):
            self.badge.setText(t("env_unknown") if snap is None else t("env_offline"))
            return
        env = snap.get("environment")
        if env == "simulation":
            text = t("env_sim")
        elif env == "demo":
            text = t("env_demo")
        else:
            text = str(snap.get("environment_label") or env).upper()
        self.badge.setText(text)
        self.badge.setToolTip(str(snap.get("adapter") or ""))

    def _on_snapshot(self, snap: dict[str, Any]) -> None:
        self.snap = snap
        self._set_badge(snap)
        self._last_snapshot_ms = 0
        self.room.apply_snapshot(snap)
        idx = self.tabs.currentIndex()
        panels: list[Any] = [self.market, self.portfolio, self.risk, self.agents, self.orders]
        if idx < len(panels):
            panels[idx].update_from(snap)  # only the visible panel: keeps the UI light
        a, r = snap["account"], snap["risk"]
        ccy = a["currency"]
        self.kpi.setText(
            f"Capital {money(a['equity'], ccy)}  ·  Hoje {pct(r.get('daily_pnl_pct'))}"
            f"  ·  Entradas {r['entries_used'] + r['entries_reserved']}/"
            f"{r['entries_max']}"
        )
        open_txt = "mercado aberto" if snap.get("market_open") else "▲ " + t("market_closed")
        self.clock.setText(
            f"{t('sim_clock')}: {snap['now'][:16].replace('T', ' ')} UTC  ·  "
            f"{t('trading_day')} {snap['trading_day']}  ·  {open_txt}"
        )
        sess = snap.get("sessions") or []
        self.sessions.setText(f"{t('sessions')}: {', '.join(sess) if sess else '—'}")
        self.pause_btn.setText(t("resume") if snap.get("paused") else t("pause"))
        self.pause_btn.setToolTip(
            t("paused_safety") if snap.get("pause_reason") == "safety" else ""
        )
        killed = bool(r.get("kill_switch"))
        self.kill_btn.setText("■ KILL SWITCH ATIVO" if killed else t("kill"))
        self.kill_btn.setEnabled(not killed)
        speed = float(snap.get("speed", 2.0))
        i = min(range(len(SPEEDS)), key=lambda k: abs(SPEEDS[k][1] - speed))
        if self.speed.currentIndex() != i:
            self.speed.blockSignals(True)
            self.speed.setCurrentIndex(i)
            self.speed.blockSignals(False)
        if not self.log.seen:
            for ev in snap.get("events", []):
                self.log.add(ev)

    def _on_event(self, ev: dict[str, Any]) -> None:
        self.room.on_event(ev)
        self.log.add(ev)

    def _on_ack(self, msg: dict[str, Any]) -> None:
        name = msg.get("name")
        if not msg.get("ok"):
            QMessageBox.warning(self, "Comando recusado", f"{name}: {msg.get('error')}")
        elif name == "journal.verify":
            res = msg.get("result", {})
            if res.get("ok"):
                QMessageBox.information(
                    self,
                    "Integridade do registo",
                    f"● Cadeia íntegra: {res.get('events')} eventos verificados.",
                )
            else:
                QMessageBox.critical(
                    self,
                    "Integridade do registo",
                    f"✖ Cadeia quebrada no evento {res.get('first_bad_seq')}.",
                )

    def _tick_status(self) -> None:
        self._last_snapshot_ms += 1000
        if self.client.online:
            age = self._last_snapshot_ms / 1000
            self.status_beat.setText(
                f"● dados há {age:.0f}s" if age < 3 else f"▲ sem dados há {age:.0f}s"
            )
        else:
            self.status_beat.setText("")
        if self.snap:
            self.status_journal.setText(
                f"origem dos dados: {self.snap.get('data_origin')} · "
                f"ciclos {self.snap.get('cycles')}"
            )

    # --- actions ----------------------------------------------------------------------------

    def _on_station(self, key: str) -> None:
        if key in ("trend", "momentum", "mean_reversion", "regime", "coordinator"):
            self.tabs.setCurrentWidget(self.agents)
            self.agents.select(key)
        elif key == "risk":
            self.tabs.setCurrentWidget(self.risk)
        elif key in ("execution", "broker"):
            self.tabs.setCurrentWidget(self.orders)
        if self.snap:
            self._on_snapshot(self.snap)

    def _on_pair(self, sym: str) -> None:
        self.room.chart_pair = sym

    def _toggle_pause(self) -> None:
        self.client.send("resume" if self.snap.get("paused") else "pause")

    def _kill(self) -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Kill switch")
        box.setText(
            "■ Ativar o KILL SWITCH?\n\nTodas as novas entradas ficam bloqueadas até "
            "libertação manual. As saídas continuam permitidas."
        )
        only = box.addButton("Ativar (manter posições)", QMessageBox.ButtonRole.AcceptRole)
        flat = box.addButton(
            "Ativar e fechar todas as posições", QMessageBox.ButtonRole.DestructiveRole
        )
        box.addButton("Cancelar", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is only:
            self.client.send("kill_switch.engage", reason="manual (interface)")
        elif box.clickedButton() is flat:
            self.client.send("kill_switch.engage", reason="manual (interface)", flatten=True)

    def _release(self) -> None:
        if confirm_word(
            self,
            "Libertar kill switch",
            "As novas entradas voltarão a ser permitidas (sujeitas às restantes regras de risco).",
            CONFIRM_KILL_RELEASE,
        ):
            self.client.send("kill_switch.release", confirmation=CONFIRM_KILL_RELEASE)

    def _flatten(self) -> None:
        if confirm_word(
            self,
            "Fechar todas as posições",
            "Todas as posições abertas serão fechadas a mercado.",
            CONFIRM_FLATTEN,
        ):
            self.client.send("flatten_all", confirmation=CONFIRM_FLATTEN)

    def _settings(self) -> None:
        if not self.snap:
            return
        dlg = SettingsDialog(self, self.snap["config"])
        if dlg.exec():
            self.client.send("config.update", patch=dlg.risk_patch())
            change = dlg.account_change()
            if change:
                self._reset(change)

    def _reset(self, change: dict[str, Any] | None) -> None:
        if confirm_word(
            self,
            "Reiniciar conta simulada",
            "A conta simulada recomeça do zero (posições, ordens e limites diários). "
            "O registo histórico é mantido.",
            CONFIRM_RESET,
        ):
            self.log.seen.clear()
            self.client.send("account.reset", confirmation=CONFIRM_RESET, **(change or {}))

    def _toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    # --- geometry and close policy ----------------------------------------------------------

    def _restore_geometry(self) -> None:
        s = QSettings(ORG_NAME, APP_ID)
        geo = s.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)

    def closeEvent(self, ev: QCloseEvent) -> None:  # noqa: N802 - Qt override
        if not self._quitting:
            answer = QMessageBox.question(self, t("close_title"), t("close_body"))
            if answer != QMessageBox.StandardButton.Yes:
                ev.ignore()
                return
        self._quitting = True
        if self._remember_geometry:
            QSettings(ORG_NAME, APP_ID).setValue("geometry", self.saveGeometry())
        self._set_engine("A parar o motor…", P.amber, "▲")
        self.supervisor.stop()
        self.client.close()
        ev.accept()
