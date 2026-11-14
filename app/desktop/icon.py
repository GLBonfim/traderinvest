"""Procedural pixel-art application icon (no third-party art): a neon isometric cube with a
rising chart line, drawn on a 32x32 grid and scaled with nearest-neighbour sampling."""

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QIcon, QImage, QPainter, QPixmap, QPolygon

from app.desktop.theme import P, qc


def icon_image(size: int = 256) -> QImage:
    g = QImage(32, 32, QImage.Format.Format_ARGB32_Premultiplied)
    g.fill(Qt.GlobalColor.transparent)
    p = QPainter(g)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(qc(P.bg))
    p.drawRoundedRect(QRect(0, 0, 32, 32), 6, 6)
    top = QPolygon([QPoint(16, 5), QPoint(27, 10), QPoint(16, 15), QPoint(5, 10)])
    left = QPolygon([QPoint(5, 10), QPoint(16, 15), QPoint(16, 27), QPoint(5, 22)])
    right = QPolygon([QPoint(16, 15), QPoint(27, 10), QPoint(27, 22), QPoint(16, 27)])
    p.setBrush(qc("#123a2a"))
    p.drawPolygon(top)
    p.setBrush(qc("#0b2219"))
    p.drawPolygon(left)
    p.setBrush(qc("#0f2d21"))
    p.drawPolygon(right)
    p.setPen(qc(P.green))
    p.drawPolyline(
        QPolygon(
            [
                QPoint(7, 20),
                QPoint(10, 17),
                QPoint(13, 19),
                QPoint(16, 13),
                QPoint(19, 15),
                QPoint(23, 9),
                QPoint(25, 7),
            ]
        )
    )
    p.setPen(qc(P.magenta))
    p.drawLine(QPoint(16, 15), QPoint(16, 27))
    p.drawLine(QPoint(5, 10), QPoint(16, 5))
    p.drawLine(QPoint(16, 5), QPoint(27, 10))
    p.end()
    return g.scaled(
        size, size, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.FastTransformation
    )


def app_icon() -> QIcon:
    icon = QIcon()
    for s in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(QPixmap.fromImage(icon_image(s)))
    return icon
