"""Side panels of the main window. Each panel renders the engine snapshot; none trades."""

from collections.abc import Sequence
from typing import Any

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPaintEvent, QPen, QPolygonF
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.desktop.strings import t
from app.desktop.theme import AGENT_COLORS, P, mono_font, qc
from app.fx.lifecycle import LABELS as STATE_LABELS
from app.fx.lifecycle import OrderState

AGENT_INFO: dict[str, tuple[str, str]] = {
    "trend": (
        "Agente de Tendência (Trend Following)",
        "EMA 20/50 alinhadas com inclinação da EMA lenta. Stop 2 ATR, alvo 3 ATR.",
    ),
    "momentum": (
        "Agente de Momentum",
        "RSI 14 entre 55–75 (ou 25–45), ROC 10 e histograma MACD a acelerar. "
        "Stop 1,5 ATR, alvo 2,5 ATR.",
    ),
    "mean_reversion": (
        "Agente de Reversão à Média",
        "Saídas das Bandas de Bollinger 20/2 confirmadas pelo RSI. Alvo: média; stop 1,5 ATR.",
    ),
    "regime": (
        "Filtro de Volatilidade / Regime",
        "Classifica cada par (tendência, lateral, misto) pelo Efficiency Ratio e veta "
        "entradas com volatilidade extrema, mercado parado ou spread largo.",
    ),
    "coordinator": (
        "Coordenador de Carteira",
        "Pondera os agentes conforme o regime, resolve conflitos, diversifica por "
        "moeda e envia no máximo um pedido por ciclo ao gestor de risco.",
    ),
}

ACTION_LABELS = {"open": "ABRIR", "close": "FECHAR", "hold": "MANTER", "veto": "VETO"}
DIR_LABELS = {"long": "▲ COMPRA", "short": "▼ VENDA", "flat": "— NEUTRO"}
REGIME_LABELS = {
    "trending": "Tendência",
    "ranging": "Lateral",
    "mixed": "Misto",
    "volatile": "▲ Volátil",
    "quiet": "▲ Parado",
}


def money(v: float | None, ccy: str = "") -> str:
    if v is None:
        return "—"
    s = f"{v:,.2f}".replace(",", " ").replace(".", ",")
    return f"{s} {ccy}".strip()


def pct(v: float | None) -> str:
    return "—" if v is None else f"{v:+.2f}%".replace(".", ",")


def price(v: float | None, sym: str = "") -> str:
    if v is None:
        return "—"
    return f"{v:.3f}" if sym.endswith("JPY") else f"{v:.5f}"


def hhmm(iso_ts: str | None) -> str:
    return "—" if not iso_ts else iso_ts[5:16].replace("T", " ")


def panel() -> QFrame:
    f = QFrame()
    f.setObjectName("Panel")
    return f


def table(headers: Sequence[str]) -> QTableWidget:
    tb = QTableWidget(0, len(headers))
    tb.setHorizontalHeaderLabels(list(headers))
    tb.verticalHeader().setVisible(False)
    tb.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    tb.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    tb.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    tb.setAlternatingRowColors(True)
    tb.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    tb.horizontalHeader().setStretchLastSection(True)
    tb.setFont(mono_font(9))
    return tb


def fill(tb: QTableWidget, rows: list[list[tuple[str, str | None]]]) -> None:
    """Rows of (text, colour or None). Keeps the selection when the row count is unchanged."""
    sel = tb.currentRow()
    tb.setUpdatesEnabled(False)
    tb.setRowCount(len(rows))
    for r, row in enumerate(rows):
        for c, (text, color) in enumerate(row):
            item = tb.item(r, c)
            if item is None:
                item = QTableWidgetItem()
                tb.setItem(r, c, item)
            if item.text() != text:
                item.setText(text)
            item.setForeground(QColor(color or P.text))
    if 0 <= sel < len(rows):
        tb.selectRow(sel)
    tb.setUpdatesEnabled(True)


def kv_grid(keys: Sequence[tuple[str, str]]) -> tuple[QGridLayout, dict[str, QLabel]]:
    grid = QGridLayout()
    labels: dict[str, QLabel] = {}
    for i, (key, title) in enumerate(keys):
        k = QLabel(title)
        k.setObjectName("Muted")
        v = QLabel("—")
        v.setFont(mono_font(11, bold=True))
        grid.addWidget(k, i // 2, (i % 2) * 2)
        grid.addWidget(v, i // 2, (i % 2) * 2 + 1)
        labels[key] = v
    return grid, labels


class EquityChart(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.points: list[float] = []
        self.ref: float | None = None
        self.ccy = ""
        self.setMinimumHeight(130)

    def set_data(self, curve: list[list[Any]], ref: float | None, ccy: str) -> None:
        self.points = [float(v) for _, v in curve]
        self.ref, self.ccy = ref, ccy
        self.update()

    def paintEvent(self, _ev: QPaintEvent) -> None:  # noqa: N802 - Qt override
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(46, 8, -8, -16)
        p.fillRect(self.rect(), qc(P.panel))
        p.setPen(QPen(qc(P.border), 1))
        p.drawRect(r)
        pts = self.points
        p.setFont(mono_font(8))
        if len(pts) < 2:
            p.setPen(qc(P.muted))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, "curva de capital: a recolher dados…")
            return
        vals = [*pts, self.ref] if self.ref is not None else pts
        lo, hi = min(vals), max(vals)
        span = (hi - lo) or 1.0

        def y(v: float) -> float:
            return r.bottom() - (v - lo) / span * r.height()

        if self.ref is not None:
            p.setPen(QPen(qc(P.amber, 160), 1, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(r.left(), y(self.ref)), QPointF(r.right(), y(self.ref)))
        poly = QPolygonF(
            [QPointF(r.left() + r.width() * i / (len(pts) - 1), y(v)) for i, v in enumerate(pts)]
        )
        up = pts[-1] >= pts[0]
        p.setPen(QPen(qc(P.green if up else P.red), 2))
        p.drawPolyline(poly)
        p.setPen(qc(P.muted))
        p.drawText(QPointF(2, r.top() + 10), f"{hi:,.0f}".replace(",", " "))
        p.drawText(QPointF(2, r.bottom()), f"{lo:,.0f}".replace(",", " "))
        p.drawText(QPointF(r.left(), self.height() - 3), "— capital próprio   - - início do dia FX")


class EntryCells(QWidget):
    """Daily entries: filled square = used, outlined = reserved, dim = available (+ text)."""

    def __init__(self) -> None:
        super().__init__()
        self.used, self.reserved, self.max = 0, 0, 5
        self.setMinimumHeight(34)

    def set(self, used: int, reserved: int, mx: int) -> None:
        self.used, self.reserved, self.max = used, reserved, mx
        self.update()

    def paintEvent(self, _ev: QPaintEvent) -> None:  # noqa: N802 - Qt override
        p = QPainter(self)
        size = 22
        for i in range(self.max):
            r = QRectF(4 + i * (size + 6), 6, size, size)
            if i < self.used:
                p.fillRect(r, qc(P.amber))
                p.setPen(qc(P.bg))
                p.drawText(r, Qt.AlignmentFlag.AlignCenter, "✓")
            elif i < self.used + self.reserved:
                p.setPen(QPen(qc(P.cyan), 2))
                p.drawRect(r)
                p.drawText(r, Qt.AlignmentFlag.AlignCenter, "…")
            else:
                p.setPen(QPen(qc(P.green, 120), 1))
                p.drawRect(r)
        p.setPen(qc(P.text))
        p.setFont(mono_font(10, bold=True))
        free = max(0, self.max - self.used - self.reserved)
        p.drawText(
            QPointF(12 + self.max * (size + 6), 23),
            f"{self.used} usadas · {self.reserved} reservadas · {free} livres",
        )


class PnlGauge(QWidget):
    """Daily P&L between the loss limit (left) and the profit target (right)."""

    def __init__(self) -> None:
        super().__init__()
        self.value, self.loss, self.target = 0.0, 5.0, 15.0
        self.setMinimumHeight(46)

    def set(self, value: float, loss: float, target: float) -> None:
        self.value, self.loss, self.target = value, loss, target
        self.update()

    def paintEvent(self, _ev: QPaintEvent) -> None:  # noqa: N802 - Qt override
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(8, 14, -8, -14)
        total = self.loss + self.target
        zero_x = r.left() + r.width() * self.loss / total
        p.fillRect(QRectF(r.left(), r.top(), zero_x - r.left(), r.height()), qc(P.red, 40))
        p.fillRect(QRectF(zero_x, r.top(), r.right() - zero_x, r.height()), qc(P.green, 30))
        v = max(-self.loss, min(self.target, self.value))
        x = r.left() + r.width() * (v + self.loss) / total
        col = P.green if v >= 0 else P.red
        p.fillRect(QRectF(min(x, zero_x), r.top() + 3, abs(x - zero_x), r.height() - 6), qc(col))
        p.setPen(QPen(qc(P.text), 2))
        p.drawLine(QPointF(zero_x, r.top() - 3), QPointF(zero_x, r.bottom() + 3))
        p.setFont(mono_font(8))
        p.setPen(qc(P.red))
        p.drawText(QPointF(r.left(), r.top() - 2), f"■ −{self.loss:g}% bloqueio")
        p.setPen(qc(P.green))
        lbl = f"objetivo +{self.target:g}% ■"
        p.drawText(QPointF(r.right() - p.fontMetrics().horizontalAdvance(lbl), r.top() - 2), lbl)
        p.setPen(qc(P.text))
        p.drawText(QPointF(zero_x - 20, r.bottom() + 12), f"hoje {pct(self.value)}")


class MarketPanel(QWidget):
    pair_selected = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        lay = QVBoxLayout(self)
        note = QLabel("▲ Preços SINTÉTICOS gerados localmente — não são cotações de mercado.")
        note.setStyleSheet(f"color: {P.amber}; font-weight: bold;")
        note.setWordWrap(True)
        lay.addWidget(note)
        self.table = table(
            ["Par", "Bid", "Ask", "Spread", "Tend.", "Regime", "Coordenador", "Origem"]
        )
        self.table.cellClicked.connect(self._clicked)
        lay.addWidget(self.table)
        self._first: dict[str, float] = {}
        self._pairs: list[str] = []

    def _clicked(self, row: int, _col: int) -> None:
        if 0 <= row < len(self._pairs):
            self.pair_selected.emit(self._pairs[row])

    def update_from(self, snap: dict[str, Any]) -> None:
        rows = []
        self._pairs = []
        for sym, q in snap.get("quotes", {}).items():
            self._pairs.append(sym)
            first = self._first.setdefault(sym, q["mid"])
            d = q["mid"] - first
            trend = ("▲", P.green) if d > 0 else ("▼", P.red) if d < 0 else ("—", P.muted)
            props = snap.get("proposals", {}).get(sym, {})
            reg = props.get("regime", {}).get("evidence", {}).get("regime")
            dec = snap.get("decisions", {}).get(sym, {})
            act = dec.get("action")
            act_txt = (
                f"{ACTION_LABELS.get(act, '—')} {DIR_LABELS.get(dec.get('direction'), '')}"
                if act and act != "hold"
                else "MANTER"
            )
            rows.append(
                [
                    (f"{sym[:3]}/{sym[3:]}", P.cyan),
                    (price(q["bid"], sym), None),
                    (price(q["ask"], sym), None),
                    (f"{q['spread_pips']:.1f}", None),
                    trend,
                    (
                        REGIME_LABELS.get(str(reg), "—"),
                        P.amber if reg in ("volatile", "quiet") else None,
                    ),
                    (act_txt, P.magenta if act == "open" else P.amber if act == "veto" else None),
                    ("SINTÉTICO" if q["origin"] == "synthetic" else q["origin"].upper(), P.amber),
                ]
            )
        fill(self.table, rows)


class PortfolioPanel(QWidget):
    def __init__(self) -> None:
        super().__init__()
        lay = QVBoxLayout(self)
        box = panel()
        grid, self.kv = kv_grid(
            [
                ("initial", "Capital inicial"),
                ("balance", "Saldo"),
                ("equity", "Capital próprio"),
                ("unrealized", "P&L não realizado"),
                ("realized", "P&L realizado (total)"),
                ("commissions", "Comissões (total)"),
                ("margin", "Margem usada"),
                ("free", "Margem livre"),
                ("level", "Nível de margem"),
                ("exposure", "Maior exposição"),
            ]
        )
        box.setLayout(grid)
        lay.addWidget(box)
        self.chart = EquityChart()
        lay.addWidget(self.chart)
        lay.addWidget(QLabel("Posições abertas"))
        self.positions = table(
            ["Par", "Lado", "Unidades", "Preço médio", "Marca", "Stop", "Alvo", "Agente", "Aberta"]
        )
        lay.addWidget(self.positions, 2)
        lay.addWidget(QLabel("Posições fechadas (recentes)"))
        self.closed = table(["Fecho", "Par", "Unidades", "Entrada", "Saída", "Motivo", "P&L"])
        lay.addWidget(self.closed, 2)

    def update_from(self, snap: dict[str, Any]) -> None:
        a = snap["account"]
        ccy = a["currency"]
        unreal = a["unrealized"]
        self.kv["initial"].setText(money(snap.get("initial_capital"), ccy))
        self.kv["balance"].setText(money(a["balance"], ccy))
        self.kv["equity"].setText(money(a["equity"], ccy))
        self.kv["unrealized"].setText(money(unreal, ccy))
        self.kv["unrealized"].setStyleSheet(f"color: {P.green if unreal >= 0 else P.red};")
        self.kv["realized"].setText(money(snap.get("realized_total"), ccy))
        self.kv["commissions"].setText(money(snap.get("commissions_total"), ccy))
        self.kv["margin"].setText(money(a["margin_used"], ccy))
        self.kv["free"].setText(money(a["free_margin"], ccy))
        lvl = a.get("margin_level_pct")
        self.kv["level"].setText("—" if lvl is None else f"{lvl:,.0f}%".replace(",", " "))
        exp = snap.get("exposure", {})
        if exp:
            k, v = max(exp.items(), key=lambda kv: abs(kv[1]))
            self.kv["exposure"].setText(f"{k} {money(v, ccy)}")
        else:
            self.kv["exposure"].setText("—")
        self.chart.set_data(snap.get("equity_curve", []), snap["risk"].get("day_start_equity"), ccy)
        rows = []
        for p in snap.get("positions", []):
            long = p["units"] > 0
            rows.append(
                [
                    (f"{p['pair'][:3]}/{p['pair'][3:]}", P.cyan),
                    ("▲ COMPRA" if long else "▼ VENDA", P.green if long else P.red),
                    (f"{abs(p['units']):,.0f}".replace(",", " "), None),
                    (price(p["avg_price"], p["pair"]), None),
                    (price(p["mark"], p["pair"]), None),
                    (price(p.get("stop_loss"), p["pair"]), P.red),
                    (price(p.get("take_profit"), p["pair"]), P.green),
                    (str(p.get("owner") or "—"), AGENT_COLORS.get(str(p.get("owner")))),
                    (hhmm(p["opened_at"]), P.muted),
                ]
            )
        fill(self.positions, rows)
        crow = []
        for c in reversed(snap.get("closed_positions", [])):
            pnl = float(c.get("realized", 0.0))
            reason = {
                "stop_loss": "■ Stop",
                "take_profit": "● Alvo",
                "stop_out": "▲ Margem",
                "order": "◆ Ordem",
            }.get(c.get("reason", ""), c.get("reason", ""))
            crow.append(
                [
                    (hhmm(c.get("closed_at")), P.muted),
                    (c["pair"], P.cyan),
                    (f"{c['units']:+,.0f}".replace(",", " "), None),
                    (price(c["avg_price"], c["pair"]), None),
                    (price(c.get("exit_price"), c["pair"]), None),
                    (reason, None),
                    (money(pnl, ccy), P.green if pnl >= 0 else P.red),
                ]
            )
        fill(self.closed, crow)


class RiskPanel(QWidget):
    kill_requested = Signal()
    release_requested = Signal()
    flatten_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        lay = QVBoxLayout(self)
        self.state = QLabel("—")
        self.state.setObjectName("Big")
        self.state.setWordWrap(True)
        lay.addWidget(self.state)
        lay.addWidget(QLabel("Entradas do dia FX (contam aberturas e aumentos; saídas não contam)"))
        self.cells = EntryCells()
        lay.addWidget(self.cells)
        lay.addWidget(QLabel("P&L diário (realizado + não realizado, líquido de custos)"))
        self.gauge = PnlGauge()
        lay.addWidget(self.gauge)
        box = panel()
        grid, self.kv = kv_grid(
            [
                ("day", "Dia FX"),
                ("start", "Capital no início do dia"),
                ("pnl", "P&L do dia"),
                ("range", "Mín / máx do dia"),
                ("lock", "Bloqueio diário"),
                ("kill", "Kill switch"),
            ]
        )
        box.setLayout(grid)
        lay.addWidget(box)
        buttons = QHBoxLayout()
        self.kill = QPushButton(t("kill"))
        self.kill.setObjectName("Kill")
        self.kill.clicked.connect(self.kill_requested.emit)
        self.release = QPushButton(t("kill_release"))
        self.release.setObjectName("Release")
        self.release.clicked.connect(self.release_requested.emit)
        self.flatten = QPushButton(t("flatten"))
        self.flatten.clicked.connect(self.flatten_requested.emit)
        for b in (self.kill, self.release, self.flatten):
            buttons.addWidget(b)
        lay.addLayout(buttons)
        self.rules = QLabel()
        self.rules.setObjectName("Muted")
        self.rules.setWordWrap(True)
        lay.addWidget(self.rules)
        lay.addStretch(1)

    def update_from(self, snap: dict[str, Any]) -> None:
        r = snap["risk"]
        ccy = snap["account"]["currency"]
        if r.get("kill_switch"):
            self.state.setText(f"■ KILL SWITCH ATIVO — {r.get('kill_reason') or ''}")
            self.state.setStyleSheet(f"color: {P.red};")
        elif r.get("lock"):
            self.state.setText(f"■ {r.get('lock_label')}")
            self.state.setStyleSheet(f"color: {P.red};")
        elif r.get("entries_available", 0) == 0:
            self.state.setText("▲ Sem entradas disponíveis hoje (saídas permitidas)")
            self.state.setStyleSheet(f"color: {P.amber};")
        else:
            self.state.setText("● Risco dentro dos limites — novas entradas permitidas")
            self.state.setStyleSheet(f"color: {P.green};")
        self.cells.set(int(r["entries_used"]), int(r["entries_reserved"]), int(r["entries_max"]))
        self.gauge.set(
            float(r.get("daily_pnl_pct") or 0.0),
            float(r["loss_limit_pct"]),
            float(r["profit_target_pct"]),
        )
        self.kv["day"].setText(str(r.get("trading_day")))
        self.kv["start"].setText(money(r.get("day_start_equity"), ccy))
        self.kv["pnl"].setText(f"{money(r.get('daily_pnl'), ccy)}  ({pct(r.get('daily_pnl_pct'))})")
        self.kv["range"].setText(
            f"{pct(r.get('day_low_pnl_pct'))} / {pct(r.get('day_high_pnl_pct'))}"
        )
        self.kv["lock"].setText("■ " + str(r.get("lock_label")) if r.get("lock") else "○ nenhum")
        self.kv["kill"].setText("■ ATIVO" if r.get("kill_switch") else "○ inativo")
        self.kill.setEnabled(not r.get("kill_switch"))
        self.release.setEnabled(bool(r.get("kill_switch")))
        self.flatten.setEnabled(bool(snap.get("positions")))
        cfg = snap.get("config", {}).get("risk", {})
        self.rules.setText(
            f"Regras: máx {cfg.get('max_entries_per_day')} entradas/dia · perda diária "
            f"−{cfg.get('daily_loss_limit_pct')}% · objetivo "
            f"+{cfg.get('daily_profit_target_pct')}% · risco por operação "
            f"{cfg.get('risk_per_trade_pct')}% · alavancagem máx "
            f"{cfg.get('max_leverage')}x · spread máx {cfg.get('max_spread_pips')} pips · "
            f"pontuação mín {cfg.get('min_score')}. Os bloqueios persistem após reinício e só "
            "terminam no dia FX seguinte (17:00 Nova Iorque). O kill switch só se liberta "
            "manualmente com confirmação."
        )


class AgentsPanel(QWidget):
    def __init__(self) -> None:
        super().__init__()
        lay = QHBoxLayout(self)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.list = QListWidget()
        self.list.setMaximumWidth(220)
        for key, (title, _) in AGENT_INFO.items():
            it = QListWidgetItem(f"◆ {title.split('(')[0].strip()}")
            it.setData(Qt.ItemDataRole.UserRole, key)
            it.setForeground(QColor(AGENT_COLORS[key]))
            self.list.addItem(it)
        self.list.currentItemChanged.connect(lambda *_: self._render())
        split.addWidget(self.list)
        right = QWidget()
        rl = QVBoxLayout(right)
        self.title = QLabel()
        self.title.setObjectName("Big")
        self.desc = QLabel()
        self.desc.setWordWrap(True)
        self.status = QLabel()
        self.status.setObjectName("Muted")
        note = QLabel("ⓘ " + t("score_note"))
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {P.amber};")
        self.table = table(
            [
                "Par",
                "Ação",
                "Direção",
                "Pontuação",
                "Entrada",
                "Stop",
                "Alvo",
                "R/R",
                "Justificação",
            ]
        )
        for w in (self.title, self.desc, self.status, note):
            rl.addWidget(w)
        rl.addWidget(self.table, 1)
        split.addWidget(right)
        lay.addWidget(split)
        self.snap: dict[str, Any] = {}
        self.list.setCurrentRow(0)

    def select(self, key: str) -> None:
        for i in range(self.list.count()):
            if self.list.item(i).data(Qt.ItemDataRole.UserRole) == key:
                self.list.setCurrentRow(i)

    def update_from(self, snap: dict[str, Any]) -> None:
        self.snap = snap
        self._render()

    def _render(self) -> None:
        it = self.list.currentItem()
        if it is None:
            return
        key = str(it.data(Qt.ItemDataRole.UserRole))
        title, desc = AGENT_INFO[key]
        self.title.setText(title)
        self.title.setStyleSheet(f"color: {AGENT_COLORS[key]};")
        self.desc.setText(desc)
        st = self.snap.get("agents", {}).get(key, {})
        self.status.setText(
            f"Última execução: {hhmm(st.get('last_run'))} · sinais emitidos: "
            f"{st.get('signals', 0)} · v1.0.0"
        )
        rows = []
        if key == "coordinator":
            for sym, d in self.snap.get("decisions", {}).items():
                rows.append(
                    [
                        (sym, P.cyan),
                        (ACTION_LABELS.get(d["action"], d["action"]), None),
                        (DIR_LABELS.get(d["direction"], "—"), None),
                        (f"{d['score']:.2f}", None),
                        ("—", None),
                        ("—", None),
                        ("—", None),
                        ("—", None),
                        (f"[{REGIME_LABELS.get(d['regime'], d['regime'])}] {d['summary']}", None),
                    ]
                )
        else:
            for sym, props in self.snap.get("proposals", {}).items():
                pr = props.get(key)
                if not pr:
                    continue
                act = pr["action"]
                color = (
                    P.magenta if act == "open" else P.amber if act in ("veto", "close") else None
                )
                rows.append(
                    [
                        (sym, P.cyan),
                        (ACTION_LABELS.get(act, act), color),
                        (DIR_LABELS.get(pr["direction"], "—"), None),
                        (f"{pr['score']:.2f}", None),
                        (price(pr.get("entry_price"), sym), None),
                        (price(pr.get("stop_loss"), sym), None),
                        (price(pr.get("take_profit"), sym), None),
                        (
                            "—" if pr.get("risk_reward") is None else f"{pr['risk_reward']:.2f}",
                            None,
                        ),
                        (pr["rationale"], None),
                    ]
                )
        fill(self.table, rows)


class OrdersPanel(QWidget):
    def __init__(self) -> None:
        super().__init__()
        lay = QVBoxLayout(self)
        lay.addWidget(
            QLabel(
                "Ciclo de vida: Sinal → Proposta → Intenção autorizada → Ordem "
                "enviada → Aceite/Rejeitada → Execução parcial/total → Posição"
            )
        )
        self.table = table(["Hora", "Par", "Lado", "Unidades", "Tipo", "Estado", "Motivo"])
        self.table.itemSelectionChanged.connect(self._show)
        lay.addWidget(self.table, 2)
        self.history = QPlainTextEdit()
        self.history.setReadOnly(True)
        self.history.setFont(mono_font(9))
        lay.addWidget(self.history, 1)
        self.intents: list[dict[str, Any]] = []

    def update_from(self, snap: dict[str, Any]) -> None:
        self.intents = list(reversed(snap.get("recent_intents", [])))
        rows = []
        for i in self.intents:
            st = i["state"]
            color = {
                "filled": P.green,
                "partially_filled": P.cyan,
                "refused": P.muted,
                "rejected": P.red,
                "unknown": P.amber,
                "cancelled": P.muted,
            }.get(st)
            rows.append(
                [
                    (hhmm(i["created_at"]), P.muted),
                    (i["pair"], P.cyan),
                    ("▲ COMPRA" if i["side"] == "buy" else "▼ VENDA", None),
                    (f"{i['units']:,.0f}".replace(",", " "), None),
                    ("Entrada" if i["is_entry"] else "Saída", None),
                    (STATE_LABELS.get(OrderState(st), st), color),
                    (i.get("reason") or "", P.muted),
                ]
            )
        fill(self.table, rows)
        self._show()

    def _show(self) -> None:
        r = self.table.currentRow()
        if not 0 <= r < len(self.intents):
            return
        i = self.intents[r]
        lines = [f"Intenção {i['intent_id']} · ordem {i['client_order_id']}"]
        for state, ts, note in i["history"]:
            label = STATE_LABELS.get(OrderState(state), state)
            lines.append(f"  {hhmm(ts)}  {label:<34} {note}")
        text = "\n".join(lines)
        if self.history.toPlainText() != text:
            self.history.setPlainText(text)


EVENT_TEXT = {
    "account_created": "● Conta simulada criada",
    "engine_recovered": "● Estado recuperado após reinício",
    "trading_day_started": "◆ Novo dia FX",
    "risk_lock": "■ Bloqueio diário ativado",
    "risk_decision": "◆ Decisão de risco",
    "order_sent": "→ Ordem enviada",
    "order_result": "← Resultado da ordem",
    "order_unknown": "▲ Resposta ambígua — a verificar no corretor",
    "position_closed": "● Posição fechada pelo corretor",
    "flatten_all": "■ Fecho de todas as posições",
    "kill_switch": "■ Kill switch",
    "config_changed": "◆ Configuração alterada",
    "engine_paused": "❚❚ Motor em pausa",
    "engine_resumed": "▶ Motor retomado",
    "engine_error": "✖ Erro do motor",
    "cycle": "◇ Ciclo de análise",
}


class LogPanel(QWidget):
    def __init__(self) -> None:
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        head = QLabel("▶ REGISTO DE EVENTOS (cadeia de hash verificável)")
        head.setStyleSheet(f"color: {P.green}; font-weight: bold;")
        lay.addWidget(head)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setMaximumBlockCount(3000)
        self.text.setStyleSheet(f"color: {P.green}; background: #050a07;")
        self.text.setFont(mono_font(9))
        lay.addWidget(self.text)
        self.seen: set[int] = set()

    def add(self, ev: dict[str, Any]) -> None:
        seq = ev.get("seq")
        if isinstance(seq, int):
            if seq in self.seen:
                return
            self.seen.add(seq)
        kind = str(ev.get("kind"))
        if kind == "cycle" and not ev.get("payload", {}).get("selected"):
            return
        p = ev.get("payload", {})
        head = EVENT_TEXT.get(kind, kind)
        detail = ""
        if kind == "risk_decision":
            detail = f"{p.get('pair')} {p.get('direction')}: {p.get('summary')}"
        elif kind == "order_result":
            detail = (
                f"{p.get('pair')} {p.get('side')} → {p.get('state')} "
                f"{p.get('filled_units') or ''} @ {p.get('avg_price') or '—'} "
                f"{p.get('reason') or ''}"
            )
        elif kind == "order_sent":
            detail = f"{p.get('pair')} {p.get('side')} {p.get('units')}"
        elif kind == "position_closed":
            f = p.get("fill", {})
            detail = f"{f.get('pair')} motivo={p.get('reason')} P&L={f.get('realized_pnl', 0):.2f}"
        elif kind == "kill_switch":
            detail = "ATIVADO" if p.get("engaged") else "libertado"
        elif kind == "cycle":
            detail = f"selecionados: {', '.join(p.get('selected', []))}"
        elif kind == "trading_day_started":
            detail = f"{p.get('day')} capital {p.get('equity', 0):.2f}"
        elif kind == "risk_lock":
            detail = f"{p.get('lock')} ({p.get('pnl_pct')}%)"
        elif kind == "engine_error":
            detail = str(p.get("message"))
        self.text.appendPlainText(f"{hhmm(str(ev.get('ts')))}  {head}  {detail}")
