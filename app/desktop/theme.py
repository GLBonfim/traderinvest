"""Visual identity: dark cyber operations room with neon accents.

State is NEVER conveyed by colour alone: every status also has a text label and a shape glyph
(`STATUS`), so the UI stays readable for colour-blind users and in screenshots.
"""

from dataclasses import dataclass

from PySide6.QtGui import QColor, QFont, QFontDatabase


@dataclass(frozen=True)
class Palette:
    bg: str = "#06080c"
    panel: str = "#0b1018"
    panel_alt: str = "#0f1622"
    border: str = "#1b2a3a"
    text: str = "#d8e6f0"
    muted: str = "#7d93a6"
    green: str = "#39ff88"
    cyan: str = "#33e1ff"
    magenta: str = "#ff3df0"
    amber: str = "#ffb020"
    red: str = "#ff3b4e"
    violet: str = "#9b6bff"


P = Palette()


def qc(hex_color: str, alpha: int = 255) -> QColor:
    c = QColor(hex_color)
    c.setAlpha(alpha)
    return c


# status -> (glyph, colour, label). Glyphs differ in SHAPE, not only in colour.
STATUS: dict[str, tuple[str, str, str]] = {
    "ok": ("●", P.green, "ATIVO"),
    "idle": ("○", P.muted, "EM ESPERA"),
    "signal": ("◆", P.cyan, "SINAL"),
    "warning": ("▲", P.amber, "AVISO"),
    "blocked": ("■", P.red, "BLOQUEADO"),
    "error": ("✖", P.red, "ERRO"),
    "paused": ("❚❚", P.amber, "EM PAUSA"),
}

AGENT_COLORS: dict[str, str] = {
    "trend": P.green,
    "momentum": P.cyan,
    "mean_reversion": P.violet,
    "regime": P.amber,
    "coordinator": P.magenta,
}


def mono_font(size: int = 10, bold: bool = False) -> QFont:
    for family in ("Cascadia Mono", "Consolas", "JetBrains Mono", "DejaVu Sans Mono"):
        if family in QFontDatabase.families():
            f = QFont(family, size)
            break
    else:
        f = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        f.setPointSize(size)
    f.setBold(bold)
    f.setStyleHint(QFont.StyleHint.Monospace)
    return f


def stylesheet() -> str:
    p = P
    return f"""
    QWidget {{ background: {p.bg}; color: {p.text}; font-size: 10pt; }}
    QMainWindow::separator {{ background: {p.border}; width: 2px; }}
    QFrame#Panel, QGroupBox {{ background: {p.panel}; border: 1px solid {p.border};
        border-radius: 4px; }}
    QGroupBox {{ margin-top: 14px; padding-top: 8px; font-weight: bold; color: {p.cyan}; }}
    QGroupBox::title {{ subcontrol-origin: margin; left: 8px; padding: 0 4px; }}
    QLabel#Muted {{ color: {p.muted}; }}
    QLabel#Big {{ font-size: 16pt; font-weight: bold; }}
    QLabel#Badge {{ border: 2px solid {p.amber}; color: {p.amber}; padding: 3px 10px;
        font-weight: bold; border-radius: 3px; background: #1a1305; }}
    QTabWidget::pane {{ border: 1px solid {p.border}; background: {p.panel}; }}
    QTabBar::tab {{ background: {p.panel_alt}; color: {p.muted}; padding: 6px 12px;
        border: 1px solid {p.border}; border-bottom: none; margin-right: 2px; }}
    QTabBar::tab:selected {{ color: {p.green}; border-color: {p.green}; background: {p.panel}; }}
    QTableWidget, QTableView {{ background: {p.panel}; gridline-color: {p.border};
        selection-background-color: #12324a; alternate-background-color: {p.panel_alt}; }}
    QHeaderView::section {{ background: {p.panel_alt}; color: {p.cyan}; border: none;
        border-right: 1px solid {p.border}; padding: 4px; font-weight: bold; }}
    QPushButton {{ background: {p.panel_alt}; border: 1px solid {p.border}; padding: 6px 12px;
        border-radius: 3px; color: {p.text}; }}
    QPushButton:hover {{ border-color: {p.cyan}; color: {p.cyan}; }}
    QPushButton:disabled {{ color: #44505c; border-color: #1a222c; }}
    QPushButton#Kill {{ background: #2a0710; border: 2px solid {p.red}; color: #ffd6db;
        font-weight: bold; padding: 8px 16px; }}
    QPushButton#Kill:hover {{ background: #4a0a18; }}
    QPushButton#Release {{ border: 2px solid {p.amber}; color: {p.amber}; }}
    QPushButton#Primary {{ border: 2px solid {p.green}; color: {p.green}; }}
    QPlainTextEdit, QTextEdit, QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
        background: {p.panel_alt}; border: 1px solid {p.border}; padding: 3px;
        selection-background-color: #12324a; }}
    QProgressBar {{ border: 1px solid {p.border}; background: {p.panel_alt}; height: 14px;
        text-align: center; color: {p.text}; }}
    QProgressBar::chunk {{ background: {p.green}; }}
    QToolTip {{ background: {p.panel_alt}; color: {p.text}; border: 1px solid {p.cyan}; }}
    QScrollBar:vertical {{ background: {p.panel}; width: 10px; }}
    QScrollBar::handle:vertical {{ background: {p.border}; min-height: 20px; }}
    QStatusBar {{ background: {p.panel}; color: {p.muted}; border-top: 1px solid {p.border}; }}
    """
