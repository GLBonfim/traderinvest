"""The isometric pixel-art operations room.

Everything is drawn procedurally (no third-party art, no licensing issue) onto a small
low-resolution canvas (`W` x `H` logical pixels) which is scaled up with nearest-neighbour
sampling, so it keeps a crisp pixel look at any window size. Labels are drawn afterwards at full
resolution so text stays readable. Each station shows its state with a glyph SHAPE and a text
label, never with colour alone.

Stations: the four specialist agents, the Portfolio Coordinator (central hologram), the Risk
vault, the Execution terminal and the broker portal. Engine events become data packets moving
between stations (proposal -> coordinator -> risk -> execution -> broker).
"""

import math
import random
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QImage,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QPolygonF,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

from app.desktop.theme import AGENT_COLORS, STATUS, P, mono_font, qc

W, H = 480, 270
N = 10  # room size in tiles
TW, TH = 32, 16  # tile size in low-res pixels
OX, OY = 240, 72  # screen position of grid (0, 0)
WALL = 58
FPS = 30


def iso(gx: float, gy: float, z: float = 0.0) -> QPointF:
    return QPointF(OX + (gx - gy) * TW / 2, OY + (gx + gy) * TH / 2 - z)


def poly(*pts: QPointF) -> QPolygonF:
    return QPolygonF(list(pts))


def shade(hex_color: str, factor: float, alpha: int = 255) -> QColor:
    c = QColor(hex_color)
    c = QColor(
        min(255, int(c.red() * factor)),
        min(255, int(c.green() * factor)),
        min(255, int(c.blue() * factor)),
    )
    c.setAlpha(alpha)
    return c


@dataclass
class Station:
    key: str
    label: str
    gx: float
    gy: float
    color: str
    kind: str  # desk | core | vault | terminal | portal
    status: str = "idle"
    detail: str = ""
    flash: float = 0.0  # seconds of highlight left

    @property
    def anchor(self) -> QPointF:
        return iso(self.gx + 0.5, self.gy + 0.5, 22)

    @property
    def feet(self) -> QPointF:
        return iso(self.gx + 0.5, self.gy + 0.5)

    def bbox(self) -> QRectF:
        c = self.feet
        return QRectF(c.x() - 22, c.y() - 40, 44, 50)


@dataclass
class Packet:
    path: list[QPointF]
    color: str
    duration: float
    delay: float = 0.0
    t: float = 0.0
    on_done: Callable[[], None] | None = None
    trail: deque[QPointF] = field(default_factory=lambda: deque(maxlen=6))

    def pos(self) -> QPointF:
        u = max(0.0, min(1.0, self.t / self.duration))
        u = u * u * (3 - 2 * u)  # ease in-out
        segs = len(self.path) - 1
        k = min(int(u * segs), segs - 1)
        f = u * segs - k
        a, b = self.path[k], self.path[k + 1]
        return QPointF(a.x() + (b.x() - a.x()) * f, a.y() + (b.y() - a.y()) * f)


@dataclass
class Burst:
    at: QPointF
    color: str
    t: float = 0.0
    life: float = 0.7


@dataclass
class FloatText:
    at: QPointF
    text: str
    color: str
    t: float = 0.0
    life: float = 2.2


RACKS: tuple[tuple[float, float], ...] = ((0.05, 8.2), (0.05, 9.1), (8.2, 0.05), (9.1, 0.05))

STATIONS: list[Station] = [
    Station("trend", "TENDÊNCIA", 1.0, 3.2, AGENT_COLORS["trend"], "desk"),
    Station("momentum", "MOMENTUM", 3.2, 1.0, AGENT_COLORS["momentum"], "desk"),
    Station("mean_reversion", "REVERSÃO", 1.0, 7.2, AGENT_COLORS["mean_reversion"], "desk"),
    Station("regime", "REGIME", 7.2, 1.0, AGENT_COLORS["regime"], "desk"),
    Station("coordinator", "COORDENADOR", 4.2, 4.2, AGENT_COLORS["coordinator"], "core"),
    Station("risk", "RISCO", 7.6, 4.6, P.red, "vault"),
    Station("execution", "EXECUÇÃO", 4.6, 7.6, P.cyan, "terminal"),
    Station("broker", "CORRETOR SIM", 8.2, 8.2, P.green, "portal"),
]


class OpsRoom(QWidget):
    station_clicked = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setMinimumSize(QSize(W + 160, H + 90))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.stations = {s.key: Station(**{k: v for k, v in s.__dict__.items()}) for s in STATIONS}
        self.packets: list[Packet] = []
        self.bursts: list[Burst] = []
        self.texts: list[FloatText] = []
        self.phase = 0.0
        self.hover: str | None = None
        self.selected: str | None = None
        self.alert = ""  # "" | lock | kill
        self.paused = False
        self.online = False
        self.sessions: list[str] = []
        self.market_open = True
        self.entries = (0, 0, 5)  # used, reserved, max
        self.pnl_pct = 0.0
        self.loss_limit, self.profit_target = 5.0, 15.0
        self.chart_pair = "EURUSD"
        self.history: dict[str, deque[float]] = {}
        self.charts: dict[str, list[float]] = {}
        self.board: list[tuple[str, int]] = []  # (pair, direction -1/0/1)
        self.rng = random.Random(3)  # noqa: S311 - visual particles only
        self.dust = [
            [self.rng.uniform(0, W), self.rng.uniform(0, H), self.rng.uniform(0.2, 1)]
            for _ in range(40)
        ]
        self.static = self._render_static()
        self.canvas = QImage(W, H, QImage.Format.Format_ARGB32_Premultiplied)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._advance)
        self.timer.start(1000 // FPS)

    # --- state ------------------------------------------------------------------------------

    def set_offline(self, reason: str) -> None:
        self.online = False
        for s in self.stations.values():
            s.status, s.detail = "error", reason

    def apply_snapshot(self, snap: dict[str, Any]) -> None:
        self.online = True
        self.paused = bool(snap.get("paused"))
        self.sessions = list(snap.get("sessions", []))
        self.market_open = bool(snap.get("market_open", True))
        risk = snap.get("risk", {})
        self.entries = (
            int(risk.get("entries_used", 0)),
            int(risk.get("entries_reserved", 0)),
            int(risk.get("entries_max", 5)),
        )
        self.pnl_pct = float(risk.get("daily_pnl_pct") or 0.0)
        self.loss_limit = float(risk.get("loss_limit_pct", 5.0))
        self.profit_target = float(risk.get("profit_target_pct", 15.0))
        self.alert = "kill" if risk.get("kill_switch") else "lock" if risk.get("lock") else ""
        self.charts = {k: [float(x) for x in v] for k, v in snap.get("charts", {}).items()}
        quotes = snap.get("quotes", {})
        board = []
        for sym, q in quotes.items():
            h = self.history.setdefault(sym, deque(maxlen=240))
            prev = h[-1] if h else q["mid"]
            h.append(float(q["mid"]))
            first = h[0]
            board.append((sym, 1 if q["mid"] > first else -1 if q["mid"] < first else 0))
            del prev
        self.board = board
        proposals: dict[str, dict[str, Any]] = snap.get("proposals", {})
        for role in ("trend", "momentum", "mean_reversion", "regime"):
            st = self.stations[role]
            acts = [p[role] for p in proposals.values() if role in p]
            opens = [a for a in acts if a["action"] in ("open", "close")]
            vetoes = [a for a in acts if a["action"] == "veto"]
            if self.paused:
                st.status, st.detail = "paused", "motor em pausa"
            elif not acts:
                st.status, st.detail = "idle", "a aguardar dados"
            elif role == "regime":
                st.status = "warning" if vetoes else "ok"
                st.detail = f"{len(vetoes)} veto(s)" if vetoes else "sem vetos"
            elif opens:
                best = max(opens, key=lambda a: a["score"])
                st.status = "signal"
                st.detail = f"{len(opens)} sinal(is) · {best['pair']} {best['score']:.2f}"
            else:
                st.status, st.detail = "ok", "sem sinais"
        coord = self.stations["coordinator"]
        cs = snap.get("agents", {}).get("coordinator", {})
        sel = cs.get("selected") or []
        coord.status = "paused" if self.paused else "signal" if sel else "ok"
        coord.detail = ", ".join(sel) if sel else "sem ações"
        rk = self.stations["risk"]
        used, res, mx = self.entries
        if risk.get("kill_switch"):
            rk.status, rk.detail = "blocked", "KILL SWITCH ATIVO"
        elif risk.get("lock"):
            rk.status = "blocked"
            rk.detail = "PERDA DIÁRIA" if risk["lock"] == "daily_loss" else "OBJETIVO ATINGIDO"
        elif used + res >= mx:
            rk.status, rk.detail = "warning", "sem entradas disponíveis"
        else:
            rk.status = "ok"
            rk.detail = f"entradas {used + res}/{mx} · P&L {self.pnl_pct:+.2f}%"
        ex = self.stations["execution"]
        open_i = snap.get("open_intents", [])
        ex.status = "signal" if open_i else "ok"
        ex.detail = (
            f"{len(open_i)} ordem(ns) em curso"
            if open_i
            else (f"{len(snap.get('positions', []))} posição(ões) aberta(s)")
        )
        br = self.stations["broker"]
        br.status = "ok" if self.market_open else "idle"
        br.detail = "simulado · ligado" if self.market_open else "mercado fechado"

    def on_event(self, ev: dict[str, Any]) -> None:
        kind = ev.get("kind")
        p = ev.get("payload", {})
        s = self.stations
        if kind == "cycle" and p.get("signals"):
            for role in ("trend", "momentum", "mean_reversion", "regime"):
                if s[role].status in ("signal", "warning"):
                    self._send(role, "coordinator", s[role].color, 0.9)
        elif kind == "risk_decision":
            ok = bool(p.get("approved"))
            nxt: Callable[[], None]
            if ok:

                def nxt() -> None:
                    self._send("risk", "execution", P.green, 0.8)
            else:

                def nxt() -> None:
                    self.bursts.append(Burst(s["risk"].anchor, P.red))
                    self.texts.append(FloatText(s["risk"].anchor, "✖ RECUSADA", P.red))

            self._send("coordinator", "risk", P.magenta, 0.9, nxt)
        elif kind == "order_sent":
            self._send("execution", "broker", P.cyan, 0.8, delay=0.8)
        elif kind == "order_result":
            if p.get("state") in ("filled", "partially_filled"):
                self._send("broker", "execution", P.green, 0.8, delay=1.4)
                label = "▲ COMPRA" if p.get("side") == "buy" else "▼ VENDA"
                self.texts.append(
                    FloatText(s["broker"].anchor, f"{label} {p.get('pair', '')}", P.green, t=-1.4)
                )
            elif p.get("state") == "rejected":
                self.bursts.append(Burst(s["broker"].anchor, P.red))
        elif kind == "position_closed":
            fill = p.get("fill", {})
            pnl = float(fill.get("realized_pnl", 0.0))
            reason = {"stop_loss": "SL", "take_profit": "TP", "stop_out": "MARGEM"}.get(
                str(p.get("reason")), "FECHO"
            )
            sign = "+" if pnl >= 0 else "−"
            self._send("broker", "execution", P.amber, 0.8)
            self.texts.append(
                FloatText(
                    s["broker"].anchor,
                    f"{reason} {fill.get('pair', '')} {sign}{abs(pnl):.2f}",
                    P.green if pnl >= 0 else P.red,
                )
            )
        elif kind in ("risk_lock", "kill_switch"):
            s["risk"].flash = 3.0
            self.bursts.append(Burst(s["risk"].anchor, P.red, life=1.2))

    def _send(
        self,
        src: str,
        dst: str,
        color: str,
        duration: float,
        on_done: Callable[[], None] | None = None,
        delay: float = 0.0,
    ) -> None:
        a, b = self.stations[src], self.stations[dst]
        mid = iso((a.gx + b.gx) / 2 + 0.5, (a.gy + b.gy) / 2 + 0.5, 34)
        self.packets.append(
            Packet([a.anchor, mid, b.anchor], color, duration, delay, on_done=on_done)
        )
        a.flash = max(a.flash, 0.6)

    # --- animation --------------------------------------------------------------------------

    def _advance(self) -> None:
        dt = 1.0 / FPS
        self.phase += dt
        for pk in list(self.packets):
            if pk.delay > 0:
                pk.delay -= dt
                continue
            pk.t += dt
            pk.trail.append(pk.pos())
            if pk.t >= pk.duration:
                self.packets.remove(pk)
                if pk.on_done:
                    pk.on_done()
        for b in list(self.bursts):
            b.t += dt
            if b.t >= b.life:
                self.bursts.remove(b)
        for tx in list(self.texts):
            tx.t += dt
            if tx.t >= tx.life:
                self.texts.remove(tx)
        for st in self.stations.values():
            st.flash = max(0.0, st.flash - dt)
        for d in self.dust:
            d[1] -= 0.08 * d[2]
            d[0] += 0.03 * math.sin(self.phase + d[2] * 9)
            if d[1] < 0:
                d[0], d[1] = self.rng.uniform(0, W), H
        self.update()

    # --- static layer -----------------------------------------------------------------------

    def _render_static(self) -> QImage:
        img = QImage(W, H, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(qc(P.bg))
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        # back walls
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(qc("#0b1220"))
        p.drawPolygon(poly(iso(0, 0), iso(0, N), iso(0, N, WALL), iso(0, 0, WALL)))
        p.setBrush(qc("#0d1526"))
        p.drawPolygon(poly(iso(0, 0), iso(N, 0), iso(N, 0, WALL), iso(0, 0, WALL)))
        p.setPen(QPen(qc("#16233a"), 1))
        for i in range(1, N):
            p.drawLine(iso(0, i), iso(0, i, WALL))
            p.drawLine(iso(i, 0), iso(i, 0, WALL))
        for z in (14, 30):
            p.drawLine(iso(0, 0, z), iso(0, N, z))
            p.drawLine(iso(0, 0, z), iso(N, 0, z))
        p.setPen(QPen(qc(P.magenta, 200), 1))
        p.drawLine(iso(0, N, WALL), iso(0, 0, WALL))
        p.drawLine(iso(0, 0, WALL), iso(N, 0, WALL))
        # floor
        for gx in range(N):
            for gy in range(N):
                base = "#0c131d" if (gx + gy) % 2 else "#0e1622"
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(qc(base))
                p.drawPolygon(
                    poly(iso(gx, gy), iso(gx + 1, gy), iso(gx + 1, gy + 1), iso(gx, gy + 1))
                )
        p.setPen(QPen(qc("#14283a"), 1))
        for i in range(N + 1):
            p.drawLine(iso(i, 0), iso(i, N))
            p.drawLine(iso(0, i), iso(N, i))
        # neon conduits on the floor (data paths)
        p.setPen(QPen(qc(P.green, 70), 1))
        for a, b in (
            ("trend", "coordinator"),
            ("momentum", "coordinator"),
            ("mean_reversion", "coordinator"),
            ("regime", "coordinator"),
            ("coordinator", "risk"),
            ("risk", "execution"),
            ("execution", "broker"),
        ):
            sa, sb = self.stations[a], self.stations[b]
            p.drawLine(iso(sa.gx + 0.5, sa.gy + 0.5), iso(sb.gx + 0.5, sb.gy + 0.5))
        # server racks along the back walls
        for rx, ry in RACKS:
            self._box(p, rx, ry, 0.8, 0.8, 34, "#101a28", edge="#1f3550")
        # front floor edge
        p.setPen(QPen(qc(P.cyan, 120), 1))
        p.drawLine(iso(N, 0), iso(N, N))
        p.drawLine(iso(0, N), iso(N, N))
        p.end()
        return img

    @staticmethod
    def _box(
        p: QPainter,
        gx: float,
        gy: float,
        w: float,
        d: float,
        h: float,
        top: str,
        left: str | None = None,
        right: str | None = None,
        edge: str | None = None,
    ) -> None:
        if w <= 0 or d <= 0:
            return
        a, b, c, dd = iso(gx, gy), iso(gx + w, gy), iso(gx + w, gy + d), iso(gx, gy + d)
        at, bt, ct, dt = (
            iso(gx, gy, h),
            iso(gx + w, gy, h),
            iso(gx + w, gy + d, h),
            iso(gx, gy + d, h),
        )
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(qc(left) if left else shade(top, 0.55))
        p.drawPolygon(poly(dt, ct, c, dd))
        p.setBrush(qc(right) if right else shade(top, 0.75))
        p.drawPolygon(poly(bt, ct, c, b))
        p.setBrush(qc(top))
        p.drawPolygon(poly(at, bt, ct, dt))
        if edge:
            p.setPen(QPen(qc(edge), 1))
            p.drawLine(dt, ct)
            p.drawLine(ct, bt)
        del a

    # --- dynamic layer ----------------------------------------------------------------------

    def _draw_screens(self, p: QPainter) -> None:
        # right wall: live chart of the selected pair
        x0, x1, z0, z1 = 0.6, 5.4, 16.0, 50.0
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(qc("#03140d"))
        p.drawPolygon(poly(iso(x0, 0, z0), iso(x1, 0, z0), iso(x1, 0, z1), iso(x0, 0, z1)))
        p.setPen(QPen(qc(P.green, 160), 1))
        p.drawPolygon(poly(iso(x0, 0, z0), iso(x1, 0, z0), iso(x1, 0, z1), iso(x0, 0, z1)))
        hist = [
            *self.charts.get(self.chart_pair, []),
            *list(self.history.get(self.chart_pair, []))[-1:],
        ]
        if len(hist) > 2:
            lo, hi = min(hist), max(hist)
            span = (hi - lo) or 1e-9
            pts = []
            for i, v in enumerate(hist):
                u = i / (len(hist) - 1)
                pts.append(
                    iso(
                        x0 + 0.15 + (x1 - x0 - 0.3) * u, 0, z0 + 4 + (z1 - z0 - 8) * (v - lo) / span
                    )
                )
            p.setPen(QPen(qc(P.green), 1))
            p.drawPolyline(QPolygonF(pts))
        # scanline
        u = (self.phase * 0.25) % 1.0
        p.setPen(QPen(qc(P.green, 40), 1))
        p.drawLine(iso(x0, 0, z0 + (z1 - z0) * u), iso(x1, 0, z0 + (z1 - z0) * u))
        # right wall: market board (direction shape + colour)
        bx0 = 6.0
        for i, (_, d) in enumerate(self.board[:9]):
            col, row = i % 3, i // 3
            gx = bx0 + col * 1.1
            z = 44 - row * 11
            c = P.green if d > 0 else P.red if d < 0 else P.muted
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qc(c, 60))
            p.drawPolygon(
                poly(iso(gx, 0, z - 8), iso(gx + 0.9, 0, z - 8), iso(gx + 0.9, 0, z), iso(gx, 0, z))
            )
            p.setBrush(qc(c))
            cx = iso(gx + 0.45, 0, z - 4)
            if d > 0:
                p.drawPolygon(
                    poly(
                        QPointF(cx.x(), cx.y() - 2),
                        QPointF(cx.x() - 2, cx.y() + 1),
                        QPointF(cx.x() + 2, cx.y() + 1),
                    )
                )
            elif d < 0:
                p.drawPolygon(
                    poly(
                        QPointF(cx.x(), cx.y() + 2),
                        QPointF(cx.x() - 2, cx.y() - 1),
                        QPointF(cx.x() + 2, cx.y() - 1),
                    )
                )
            else:
                p.drawRect(QRectF(cx.x() - 2, cx.y(), 4, 1))
        # left wall: risk board — entry cells (filled = used, outline = reserved, dim = free)
        used, res, mx = self.entries
        for i in range(mx):
            gy = 1.0 + i * 0.75
            corners = poly(
                iso(0, gy, 36), iso(0, gy + 0.55, 36), iso(0, gy + 0.55, 46), iso(0, gy, 46)
            )
            if i < used:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(qc(P.amber))
            elif i < used + res:
                p.setPen(QPen(qc(P.cyan), 1))
                p.setBrush(Qt.BrushStyle.NoBrush)
            else:
                p.setPen(QPen(qc(P.green, 90), 1))
                p.setBrush(qc(P.green, 25))
            p.drawPolygon(corners)
        # daily P&L gauge between -loss limit and +target
        g0, g1 = 1.0, 5.0
        zero = g0 + (g1 - g0) * self.loss_limit / (self.loss_limit + self.profit_target)
        frac = max(
            -1.0,
            min(1.0, self.pnl_pct / (self.profit_target if self.pnl_pct > 0 else self.loss_limit)),
        )
        end = zero + (g1 - zero) * frac if frac > 0 else zero + (zero - g0) * frac
        p.setPen(QPen(qc(P.muted, 120), 1))
        p.drawLine(iso(0, g0, 26), iso(0, g1, 26))
        p.setPen(QPen(qc(P.green if frac >= 0 else P.red), 2))
        p.drawLine(iso(0, zero, 26), iso(0, end, 26))
        p.setPen(QPen(qc(P.text), 1))
        p.drawLine(iso(0, zero, 23), iso(0, zero, 29))

    def _draw_agent(self, p: QPainter, at: QPointF, color: str, active: bool, k: int) -> None:
        x, y = int(at.x()), int(at.y())
        bob = 1 if int(self.phase * 2 + k) % 2 else 0
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(qc("#000000", 90))
        p.drawEllipse(QRectF(x - 4, y - 1, 8, 3))
        p.setBrush(qc("#121a26"))
        p.drawRect(QRect(x - 2, y - 4, 2, 4))
        p.drawRect(QRect(x + 1, y - 4, 2, 4))
        p.setBrush(qc("#1d2a3c"))
        p.drawRect(QRect(x - 3, y - 11 + bob, 7, 7))
        p.setBrush(qc(color))
        p.drawRect(QRect(x, y - 11 + bob, 1, 7))
        arm = 1 if active and int(self.phase * 8 + k) % 2 else 0
        p.setBrush(qc("#2a3a52"))
        p.drawRect(QRect(x - 4, y - 10 + bob + arm, 1, 4))
        p.drawRect(QRect(x + 4, y - 10 + bob - arm, 1, 4))
        p.setBrush(qc("#c8d6e4"))
        p.drawRect(QRect(x - 2, y - 16 + bob, 5, 5))
        p.setBrush(qc(color))
        p.drawRect(QRect(x - 2, y - 14 + bob, 5, 1))
        if active:
            p.setBrush(qc(color, 140 + int(100 * abs(math.sin(self.phase * 4)))))
            p.drawRect(QRect(x, y - 19 + bob, 1, 2))

    def _draw_station(self, p: QPainter, st: Station, k: int) -> None:
        gx, gy = st.gx, st.gy
        hl = st.key in (self.hover, self.selected) or st.flash > 0
        edge = st.color if hl else None
        active = st.status in ("signal", "ok", "warning")
        if st.kind == "desk":
            self._draw_agent(p, iso(gx + 0.25, gy + 0.25), st.color, st.status == "signal", k)
            self._box(p, gx + 0.1, gy + 0.55, 0.8, 0.4, 9, "#1b2636", edge=edge or "#2b3c52")
            # monitor
            m0, m1 = iso(gx + 0.2, gy + 0.62, 10), iso(gx + 0.75, gy + 0.62, 10)
            glow = 120 + int(80 * abs(math.sin(self.phase * 1.7 + k)))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qc(st.color, glow if active else 50))
            p.drawPolygon(poly(m0, m1, QPointF(m1.x(), m1.y() - 7), QPointF(m0.x(), m0.y() - 7)))
            p.setBrush(qc("#000000", 120))
            for r in range(3):
                yy = m0.y() - 2 - r * 2 + ((self.phase * 6 + k) % 2)
                p.drawRect(QRectF(m0.x() + 2, yy, (m1.x() - m0.x()) * (0.4 + 0.2 * r), 1))
        elif st.kind == "core":
            self._box(p, gx, gy, 1.2, 1.2, 6, "#20122a", edge=edge or P.magenta)
            c = iso(gx + 0.6, gy + 0.6, 6)
            for i in range(3):
                rr = 9 + i * 3
                a0 = (self.phase * (60 + 30 * i)) % 360
                p.setPen(QPen(qc(P.magenta, 200 - i * 50), 1))
                p.setBrush(Qt.BrushStyle.NoBrush)
                rect = QRectF(c.x() - rr, c.y() - 14 - rr / 2 - i * 3, rr * 2, rr)
                p.drawArc(rect, int(a0 * 16), 200 * 16)
            p.setPen(Qt.PenStyle.NoPen)
            pulse = 160 + int(90 * abs(math.sin(self.phase * 2)))
            p.setBrush(qc(P.magenta, pulse))
            p.drawPolygon(
                poly(
                    QPointF(c.x(), c.y() - 26),
                    QPointF(c.x() + 4, c.y() - 18),
                    QPointF(c.x(), c.y() - 10),
                    QPointF(c.x() - 4, c.y() - 18),
                )
            )
            self._draw_agent(p, iso(gx + 1.35, gy + 0.2), st.color, st.status == "signal", k)
        elif st.kind == "vault":
            danger = self.alert != ""
            top = "#2a0d14" if danger else "#1a2230"
            self._box(p, gx, gy, 1.1, 1.0, 16, top, edge=edge or (P.red if danger else P.amber))
            c = iso(gx + 0.55, gy + 1.0, 8)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qc(P.red if danger else P.amber))
            # padlock: closed when blocked, open otherwise (shape, not only colour)
            p.drawRect(QRectF(c.x() - 3, c.y() - 3, 7, 5))
            p.setPen(QPen(qc(P.red if danger else P.amber), 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            shackle = (
                QRectF(c.x() - 2, c.y() - 7, 5, 6) if danger else QRectF(c.x() + 0, c.y() - 8, 5, 6)
            )
            p.drawArc(shackle, 0, 180 * 16)
            self._draw_agent(p, iso(gx - 0.2, gy + 0.3), P.amber, st.status != "idle", k)
        elif st.kind == "terminal":
            self._box(p, gx, gy, 1.0, 0.6, 10, "#0f2230", edge=edge or P.cyan)
            m0 = iso(gx + 0.1, gy + 0.05, 10)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qc(P.cyan, 140 + int(80 * abs(math.sin(self.phase * 3)))))
            p.drawRect(QRectF(m0.x() - 1, m0.y() - 10, 10, 7))
            self._draw_agent(p, iso(gx + 0.3, gy - 0.25), P.cyan, st.status == "signal", k)
        elif st.kind == "portal":
            c = iso(gx + 0.5, gy + 0.5)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qc(P.green, 40))
            p.drawEllipse(QRectF(c.x() - 14, c.y() - 5, 28, 10))
            p.setBrush(Qt.BrushStyle.NoBrush)
            for i in range(4):
                a = self.phase * 3 + i * math.pi / 2
                px_, py_ = c.x() + 10 * math.cos(a), c.y() - 18 + 14 * math.sin(a)
                p.setPen(QPen(qc(P.green, 220), 1))
                p.drawPoint(QPointF(px_, py_))
            p.setPen(QPen(qc(P.green if self.market_open else P.muted), 2))
            p.drawEllipse(QRectF(c.x() - 10, c.y() - 32, 20, 28))
            p.setPen(QPen(qc(P.green, 90), 1))
            p.drawEllipse(QRectF(c.x() - 7, c.y() - 28, 14, 20))

    def _compose(self) -> None:
        img = self.canvas
        img.fill(Qt.GlobalColor.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.drawImage(0, 0, self.static)
        # session tint
        tint = None
        if not self.market_open:
            tint = qc("#000000", 90)
        elif "London" in self.sessions and "New York" in self.sessions:
            tint = qc(P.magenta, 14)
        elif "New York" in self.sessions:
            tint = qc(P.violet, 12)
        elif "London" in self.sessions:
            tint = qc(P.green, 10)
        elif self.sessions:
            tint = qc(P.cyan, 12)
        if tint is not None:
            p.fillRect(QRect(0, 0, W, H), tint)
        self._draw_screens(p)
        for i, (gx, gy) in enumerate(RACKS):  # blinking rack lights
            for j in range(6):
                on = (int(self.phase * (3 + j)) + i * 7 + j * 3) % 5 != 0
                c = (
                    iso(gx + 0.8, gy + 0.4, 8 + j * 4)
                    if gx > gy
                    else iso(gx + 0.4, gy + 0.8, 8 + j * 4)
                )
                col = (P.green, P.cyan, P.amber)[(i + j) % 3]
                p.fillRect(QRectF(c.x() - 1, c.y(), 2, 1), qc(col, 230 if on else 40))
        for k, st in enumerate(sorted(self.stations.values(), key=lambda s: s.gx + s.gy)):
            self._draw_station(p, st, k)
        for pk in self.packets:
            if pk.delay > 0:
                continue
            for i, q in enumerate(pk.trail):
                p.fillRect(QRectF(q.x() - 1, q.y() - 1, 2, 2), qc(pk.color, 40 + i * 30))
            q = pk.pos()
            p.fillRect(QRectF(q.x() - 2, q.y() - 2, 4, 4), qc(pk.color))
            p.fillRect(QRectF(q.x() - 1, q.y() - 1, 2, 2), qc("#ffffff"))
        for b in self.bursts:
            r = 4 + 18 * b.t / b.life
            p.setPen(QPen(qc(b.color, int(255 * (1 - b.t / b.life))), 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(b.at, r, r / 2)
        for d in self.dust:
            p.fillRect(QRectF(d[0], d[1], 1, 1), qc(P.cyan, int(40 * d[2])))
        if self.alert:
            a = int(40 + 40 * abs(math.sin(self.phase * (5 if self.alert == "kill" else 2))))
            p.fillRect(QRect(0, 0, W, H), qc(P.red, a // 2))
            p.fillRect(QRect(0, 0, W, 3), qc(P.red, 200))
            p.fillRect(QRect(0, H - 3, W, 3), qc(P.red, 200))
        p.end()

    # --- Qt ---------------------------------------------------------------------------------

    def _viewport(self) -> tuple[float, float, float]:
        s = min(self.width() / W, self.height() / H)
        if s >= 2:
            s = math.floor(s * 4) / 4  # quarter steps: pixels stay nearly even, space is used
        ox = (self.width() - W * s) / 2
        oy = (self.height() - H * s) / 2
        return s, ox, oy

    def paintEvent(self, _ev: QPaintEvent) -> None:  # noqa: N802 - Qt override
        self._compose()
        s, ox, oy = self._viewport()
        p = QPainter(self)
        p.fillRect(self.rect(), qc(P.bg))
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        p.drawImage(QRectF(ox, oy, W * s, H * s), self.canvas, QRectF(0, 0, W, H))
        # scanlines (subtle CRT feel)
        p.setPen(QPen(qc("#000000", 38), 1))
        step = max(2, int(s))
        for yy in range(int(oy), int(oy + H * s), step):
            p.drawLine(int(ox), yy, int(ox + W * s), yy)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        font = mono_font(8, bold=True)
        font.setPixelSize(max(9, int(4.6 * s)))
        small = mono_font(8)
        small.setPixelSize(max(9, int(4.2 * s)))
        for st in self.stations.values():
            glyph, color, label = STATUS.get(st.status, STATUS["idle"])
            top = st.bbox()
            x = ox + (top.center().x()) * s
            y = oy + (top.top() - 4) * s
            p.setFont(font)
            title = f"{glyph} {st.label}"
            fm = p.fontMetrics()
            tw = fm.horizontalAdvance(title)
            box = QRectF(x - tw / 2 - 6, y - fm.height() - 2, tw + 12, fm.height() + 4)
            p.setPen(QPen(qc(color), 1))
            p.setBrush(qc(P.bg, 210))
            p.drawRect(box)
            p.setPen(qc(color))
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, title)
            if st.key in (self.hover, self.selected):
                p.setFont(small)
                sub = f"{label} · {st.detail}" if st.detail else label
                fm2 = p.fontMetrics()
                w2 = fm2.horizontalAdvance(sub)
                box2 = QRectF(x - w2 / 2 - 5, box.bottom() + 2, w2 + 10, fm2.height() + 2)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(qc(P.panel, 230))
                p.drawRect(box2)
                p.setPen(qc(P.text))
                p.drawText(box2, Qt.AlignmentFlag.AlignCenter, sub)
        p.setFont(font)
        for tx in self.texts:
            if tx.t < 0:
                continue
            a = max(0, min(255, int(255 * (1 - tx.t / tx.life))))
            p.setPen(qc(tx.color, a))
            px_ = ox + tx.at.x() * s
            py_ = oy + (tx.at.y() - 10 - 14 * tx.t / tx.life) * s
            p.drawText(QPointF(px_ - p.fontMetrics().horizontalAdvance(tx.text) / 2, py_), tx.text)
        # captions
        cap = mono_font(8, bold=True)
        cap.setPixelSize(max(9, int(4.4 * s)))
        p.setFont(cap)
        p.setPen(qc(P.muted))
        p.drawText(QPointF(ox + 6 * s, oy + 8 * s), "SALA DE OPERAÇÕES")
        p.setPen(qc(P.green))
        p.drawText(
            QPointF(ox + 6 * s, oy + 16 * s),
            f"◢ ECRÃ: {self.chart_pair} · últimas 24h em M15 (sintético)",
        )
        if self.alert:
            p.setFont(mono_font(max(9, int(5 * s)), bold=True))
            msg = "■ KILL SWITCH ATIVO" if self.alert == "kill" else "■ BLOQUEIO DIÁRIO ATIVO"
            p.setPen(qc("#ffffff"))
            fm = p.fontMetrics()
            p.drawText(
                QPointF(ox + (W * s - fm.horizontalAdvance(msg)) / 2, oy + (H - 10) * s), msg
            )
        if not self.online:
            p.setFont(mono_font(max(9, int(5 * s)), bold=True))
            p.setPen(qc(P.amber))
            msg = "▲ A LIGAR AO MOTOR…"
            fm = p.fontMetrics()
            p.drawText(QPointF(ox + (W * s - fm.horizontalAdvance(msg)) / 2, oy + H * s / 2), msg)
        elif self.paused:
            p.setFont(mono_font(max(9, int(5 * s)), bold=True))
            p.setPen(qc(P.amber))
            msg = "❚❚ EM PAUSA"
            fm = p.fontMetrics()
            p.drawText(QPointF(ox + (W * s - fm.horizontalAdvance(msg)) / 2, oy + 26 * s), msg)
        p.end()

    def _station_at(self, pos: QPoint) -> str | None:
        s, ox, oy = self._viewport()
        lx, ly = (pos.x() - ox) / s, (pos.y() - oy) / s
        for st in sorted(self.stations.values(), key=lambda q: -(q.gx + q.gy)):
            if st.bbox().contains(QPointF(lx, ly)):
                return st.key
        return None

    def mouseMoveEvent(self, ev: QMouseEvent) -> None:  # noqa: N802 - Qt override
        key = self._station_at(ev.position().toPoint())
        if key != self.hover:
            self.hover = key
            self.setCursor(Qt.CursorShape.PointingHandCursor if key else Qt.CursorShape.ArrowCursor)
            if key:
                st = self.stations[key]
                self.setToolTip(
                    f"{st.label}\n{STATUS.get(st.status, STATUS['idle'])[2]} · {st.detail}"
                )
            else:
                self.setToolTip("")

    def mousePressEvent(self, ev: QMouseEvent) -> None:  # noqa: N802 - Qt override
        key = self._station_at(ev.position().toPoint())
        if key:
            self.selected = key
            self.station_clicked.emit(key)
