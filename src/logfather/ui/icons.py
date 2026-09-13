"""Small painted icons shared by windows (no image files to ship)."""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap

from logfather.ui import theme


def zoom_glyph_icon(kind: str, size: int = 24, *, span: float = 0.60, thickness: float = 0.14) -> QIcon:
    """A plus or minus drawn symmetrically about the icon centre (Chris,
    2026-09-05: the glyph must sit exactly in the middle of the circle).
    `span` is the bar length and `thickness` the bar width, both as a
    fraction of `size` (the replay chart's buttons use a fuller glyph)."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(theme.TEXT_BRIGHT))
    s = float(size)
    bar = s * thickness
    length = s * span
    painter.drawRect(QRectF((s - length) / 2, (s - bar) / 2, length, bar))
    if kind == "plus":
        painter.drawRect(QRectF((s - bar) / 2, (s - length) / 2, bar, length))
    painter.end()
    return QIcon(pm)


def gear_icon(size: int = 24) -> QIcon:
    """A settings gear: a ring with eight teeth and a hole (Chris,
    2026-09-07: the same gear top-right on every window)."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing)
    s = float(size)
    c = QPointF(s / 2, s / 2)
    tooth = QPen(QColor(theme.TEXT_BRIGHT))
    tooth.setWidthF(s * 0.16)
    tooth.setCapStyle(Qt.FlatCap)
    painter.setPen(tooth)
    for i in range(8):
        a = math.radians(i * 45)
        painter.drawLine(QPointF(c.x() + math.cos(a) * s * 0.28, c.y() + math.sin(a) * s * 0.28),
                         QPointF(c.x() + math.cos(a) * s * 0.46, c.y() + math.sin(a) * s * 0.46))
    ring = QPen(QColor(theme.TEXT_BRIGHT))
    ring.setWidthF(s * 0.17)
    painter.setPen(ring)
    painter.setBrush(Qt.NoBrush)
    painter.drawEllipse(c, s * 0.245, s * 0.245)
    painter.end()
    return QIcon(pm)


def thermometer_icon(size: int = 24) -> QIcon:
    """A thermometer: stem with a bulb, half filled (Chris, 2026-09-07:
    the Temps button on the Overview)."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing)
    s = float(size)
    outline = QPen(QColor(theme.TEXT_BRIGHT))
    outline.setWidthF(s * 0.09)
    painter.setPen(outline)
    painter.setBrush(Qt.NoBrush)
    stem = QRectF(s * 0.40, s * 0.10, s * 0.20, s * 0.55)
    painter.drawRoundedRect(stem, s * 0.10, s * 0.10)
    painter.setBrush(QColor(theme.TEXT_BRIGHT))
    painter.drawEllipse(QPointF(s * 0.50, s * 0.76), s * 0.17, s * 0.17)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("#ff8a65"))
    painter.drawRect(QRectF(s * 0.455, s * 0.38, s * 0.09, s * 0.30))
    painter.drawEllipse(QPointF(s * 0.50, s * 0.76), s * 0.11, s * 0.11)
    painter.end()
    return QIcon(pm)


def current_icon(size: int = 24) -> QIcon:
    """A lightning bolt for the Currents button (Chris, 2026-09-08)."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing)
    s = float(size)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("#ffd666"))
    from PySide6.QtGui import QPolygonF
    painter.drawPolygon(QPolygonF([
        QPointF(s * 0.58, s * 0.08), QPointF(s * 0.30, s * 0.54), QPointF(s * 0.48, s * 0.54),
        QPointF(s * 0.40, s * 0.92), QPointF(s * 0.70, s * 0.42), QPointF(s * 0.52, s * 0.42),
    ]))
    painter.end()
    return QIcon(pm)


def gauge_icon(size: int = 24) -> QIcon:
    """A pressure gauge: an arc with a needle (Chris, 2026-09-08)."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing)
    s = float(size)
    pen = QPen(QColor(theme.TEXT_BRIGHT))
    pen.setWidthF(s * 0.1)
    pen.setCapStyle(Qt.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    painter.drawArc(QRectF(s * 0.14, s * 0.2, s * 0.72, s * 0.72), 200 * 16, 140 * 16)
    needle = QPen(QColor("#36cfc9"))
    needle.setWidthF(s * 0.1)
    needle.setCapStyle(Qt.RoundCap)
    painter.setPen(needle)
    painter.drawLine(QPointF(s * 0.5, s * 0.56), QPointF(s * 0.68, s * 0.34))
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(theme.TEXT_BRIGHT))
    painter.drawEllipse(QPointF(s * 0.5, s * 0.56), s * 0.07, s * 0.07)
    painter.end()
    return QIcon(pm)


def plus_box_icon(size: int = 24) -> QIcon:
    """A boxed plus for the Additional readings button (Chris, 2026-09-08)."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing)
    s = float(size)
    pen = QPen(QColor(theme.TEXT_BRIGHT))
    pen.setWidthF(s * 0.09)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    painter.drawRoundedRect(QRectF(s * 0.14, s * 0.14, s * 0.72, s * 0.72), s * 0.12, s * 0.12)
    pen.setWidthF(s * 0.12)
    pen.setCapStyle(Qt.RoundCap)
    painter.setPen(pen)
    painter.drawLine(QPointF(s * 0.5, s * 0.30), QPointF(s * 0.5, s * 0.70))
    painter.drawLine(QPointF(s * 0.30, s * 0.5), QPointF(s * 0.70, s * 0.5))
    painter.end()
    return QIcon(pm)


def pick_icon(size: int = 24) -> QIcon:
    """A product dropping into a tray, for the Picks button (Chris, 2026-09-08)."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing)
    s = float(size)
    pen = QPen(QColor(theme.TEXT_BRIGHT))
    pen.setWidthF(s * 0.1)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    painter.drawPolyline([QPointF(s * 0.16, s * 0.60), QPointF(s * 0.24, s * 0.88), QPointF(s * 0.76, s * 0.88), QPointF(s * 0.84, s * 0.60)])
    painter.drawLine(QPointF(s * 0.5, s * 0.10), QPointF(s * 0.5, s * 0.62))
    painter.drawPolyline([QPointF(s * 0.36, s * 0.48), QPointF(s * 0.5, s * 0.64), QPointF(s * 0.64, s * 0.48)])
    painter.end()
    return QIcon(pm)


def arrow_icon(direction: str, size: int = 24) -> QIcon:
    """A clear left / right chevron for the step-through-time buttons
    (Chris, 2026-09-06: the style's stock arrow was too faint)."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor(theme.TEXT_BRIGHT))
    pen.setWidthF(size * 0.13)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    s = float(size)
    if direction == "left":
        points = [QPointF(s * 0.62, s * 0.22), QPointF(s * 0.36, s * 0.50), QPointF(s * 0.62, s * 0.78)]
    else:
        points = [QPointF(s * 0.38, s * 0.22), QPointF(s * 0.64, s * 0.50), QPointF(s * 0.38, s * 0.78)]
    painter.drawPolyline(points)
    painter.end()
    return QIcon(pm)


_QUESTION_ROWS = (
    ".#####.",
    "##...##",
    "##...##",
    "....##.",
    "...##..",
    "...##..",
    "...##..",
    ".......",
    "...##..",
    "...##..",
)


def question_block_icon(size: int = 40) -> QIcon:
    """A pixel-art question block in the style of the classic platform
    game (Chris, 2026-09-06): gold block, dark outline, corner rivets and
    a chunky pale ? with a shadow. Drawn on a 16x16 grid."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setPen(Qt.NoPen)
    cell = size / 16.0
    gold, shade, outline, pale = QColor("#f8b838"), QColor("#a85400"), QColor("#1a0c00"), QColor("#fff4d6")

    def px(x: int, y: int, colour: QColor) -> None:
        painter.setBrush(colour)
        painter.drawRect(QRectF(x * cell, y * cell, cell + 0.5, cell + 0.5))

    for y in range(16):
        for x in range(16):
            if x in (0, 15) or y in (0, 15):
                px(x, y, outline)
            elif x == 14 or y == 14:
                px(x, y, shade)
            else:
                px(x, y, gold)
    for x, y in ((1, 1), (13, 1), (1, 13), (13, 13)):
        px(x, y, shade)
    ox, oy = 4, 3
    for dy, row in enumerate(_QUESTION_ROWS):
        for dx, ch in enumerate(row):
            if ch == "#":
                px(ox + dx + 1, oy + dy + 1, shade)
    for dy, row in enumerate(_QUESTION_ROWS):
        for dx, ch in enumerate(row):
            if ch == "#":
                px(ox + dx, oy + dy, pale)
    painter.end()
    return QIcon(pm)


def calendar_icon(size: int = 24) -> QIcon:
    """A small calendar page for the Choose days button (Chris,
    2026-09-07): header bar, two rings, a grid of days."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing)
    s = float(size)
    ink = QColor(theme.TEXT_BRIGHT)
    pen = QPen(ink)
    pen.setWidthF(max(1.5, s * 0.08))
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    painter.drawRoundedRect(QRectF(s * 0.14, s * 0.2, s * 0.72, s * 0.66), s * 0.1, s * 0.1)
    painter.setPen(Qt.NoPen)
    painter.setBrush(ink)
    painter.drawRect(QRectF(s * 0.14, s * 0.2, s * 0.72, s * 0.18))
    for x in (0.34, 0.66):
        painter.drawRoundedRect(QRectF(s * x - s * 0.045, s * 0.08, s * 0.09, s * 0.22), s * 0.04, s * 0.04)
    for row in (0.5, 0.66):
        for col in (0.3, 0.5, 0.7):
            painter.drawRect(QRectF(s * col - s * 0.05, s * row, s * 0.1, s * 0.09))
    painter.end()
    return QIcon(pm)


# ---- the three mode buttons (Chris, 2026-09-11) --------------------------

def _start(size: int):
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing)
    return pm, painter, float(size)


def play_icon(size: int = 24) -> QIcon:
    """A solid play triangle for PikPak Replay."""
    from PySide6.QtGui import QPolygonF

    pm, painter, s = _start(size)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(theme.TEXT_BRIGHT))
    painter.drawPolygon(QPolygonF([QPointF(s * 0.28, s * 0.18), QPointF(s * 0.82, s * 0.50), QPointF(s * 0.28, s * 0.82)]))
    painter.end()
    return QIcon(pm)


def search_icon(size: int = 24) -> QIcon:
    """A magnifying glass for Search."""
    pm, painter, s = _start(size)
    pen = QPen(QColor(theme.TEXT_BRIGHT))
    pen.setWidthF(s * 0.11)
    pen.setCapStyle(Qt.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    painter.drawEllipse(QRectF(s * 0.16, s * 0.16, s * 0.46, s * 0.46))
    painter.drawLine(QPointF(s * 0.58, s * 0.58), QPointF(s * 0.84, s * 0.84))
    painter.end()
    return QIcon(pm)


def grid_icon(size: int = 24) -> QIcon:
    """Four tiles: the whole fleet at a glance (the Overview icon)."""
    pm, painter, s = _start(size)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(theme.TEXT_BRIGHT))
    tile = s * 0.30
    gap = s * 0.10
    x0 = (s - 2 * tile - gap) / 2
    for i in range(2):
        for j in range(2):
            painter.drawRoundedRect(QRectF(x0 + i * (tile + gap), x0 + j * (tile + gap), tile, tile), s * 0.05, s * 0.05)
    painter.end()
    return QIcon(pm)


def rows_icon(size: int = 24) -> QIcon:
    """Three rows with a dot each: a list of systems (an Overview option)."""
    pm, painter, s = _start(size)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(theme.TEXT_BRIGHT))
    h = s * 0.14
    for k, y in enumerate((0.20, 0.43, 0.66)):
        painter.drawEllipse(QRectF(s * 0.14, s * y, h, h))
        painter.drawRoundedRect(QRectF(s * 0.36, s * y + h * 0.15, s * 0.50, h * 0.70), h * 0.3, h * 0.3)
    painter.end()
    return QIcon(pm)


def bars_icon(size: int = 24) -> QIcon:
    """Three rising bars: the fleet's numbers (an Overview option)."""
    pm, painter, s = _start(size)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(theme.TEXT_BRIGHT))
    w = s * 0.18
    for k, hgt in enumerate((0.34, 0.54, 0.72)):
        x = s * 0.16 + k * (w + s * 0.07)
        painter.drawRoundedRect(QRectF(x, s * 0.84 - s * hgt, w, s * hgt), s * 0.03, s * 0.03)
    painter.end()
    return QIcon(pm)


def radar_icon(size: int = 24) -> QIcon:
    """A radar sweep: watching the fleet (an Overview option)."""
    pm, painter, s = _start(size)
    pen = QPen(QColor(theme.TEXT_BRIGHT))
    pen.setWidthF(s * 0.09)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    c = QPointF(s / 2, s / 2)
    painter.drawEllipse(c, s * 0.36, s * 0.36)
    painter.drawEllipse(c, s * 0.18, s * 0.18)
    painter.drawLine(c, QPointF(s * 0.80, s * 0.24))
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(theme.TEXT_BRIGHT))
    painter.drawEllipse(c, s * 0.07, s * 0.07)
    painter.end()
    return QIcon(pm)


overview_icon = rows_icon  # Chris, 2026-09-11: three rows with a dot each


def media_icon(kind: str, size: int = 28) -> QIcon:
    """Play or pause glyph in the theme's light ink, as on the conveyor
    calibration window; the main PikPak Replay Play button uses it too
    (Chris, 2026-09-11)."""
    from PySide6.QtGui import QPolygonF

    pm, painter, s = _start(size)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(theme.TEXT_BRIGHT))
    if kind == "play":
        painter.drawPolygon(QPolygonF([QPointF(s * 0.30, s * 0.18), QPointF(s * 0.30, s * 0.82), QPointF(s * 0.82, s * 0.50)]))
    else:
        bar_w = s * 0.18
        painter.drawRect(QRectF(s * 0.26, s * 0.18, bar_w, s * 0.64))
        painter.drawRect(QRectF(s * 0.56, s * 0.18, bar_w, s * 0.64))
    painter.end()
    return QIcon(pm)


def refresh_icon(size: int = 24) -> QIcon:
    """A circular arrow for the Refresh button (Chris, 2026-09-11)."""
    from PySide6.QtGui import QPolygonF

    pm, painter, s = _start(size)
    pen = QPen(QColor(theme.TEXT_BRIGHT))
    pen.setWidthF(s * 0.11)
    pen.setCapStyle(Qt.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    rect = QRectF(s * 0.20, s * 0.20, s * 0.60, s * 0.60)
    painter.drawArc(rect, 30 * 16, 300 * 16)
    # arrow head at the arc's end (top right)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(theme.TEXT_BRIGHT))
    tip_x, tip_y = s * 0.84, s * 0.30
    painter.drawPolygon(QPolygonF([QPointF(tip_x, tip_y), QPointF(tip_x - s * 0.22, tip_y - s * 0.02), QPointF(tip_x - s * 0.04, tip_y + s * 0.20)]))
    painter.end()
    return QIcon(pm)


def conveyor_icon(size: int = 24) -> QIcon:
    """A conveyor: a belt over three rollers (the Conveyor button, Chris,
    2026-09-12)."""
    pm, painter, s = _start(size)
    pen = QPen(QColor(theme.TEXT_BRIGHT))
    pen.setWidthF(s * 0.09)
    pen.setCapStyle(Qt.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    painter.drawLine(QPointF(s * 0.12, s * 0.42), QPointF(s * 0.88, s * 0.42))
    painter.drawLine(QPointF(s * 0.12, s * 0.70), QPointF(s * 0.88, s * 0.70))
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(theme.TEXT_BRIGHT))
    for cx in (0.24, 0.50, 0.76):
        painter.drawEllipse(QPointF(s * cx, s * 0.56), s * 0.09, s * 0.09)
    # a box riding the belt
    painter.drawRoundedRect(QRectF(s * 0.36, s * 0.16, s * 0.28, s * 0.20), s * 0.03, s * 0.03)
    painter.end()
    return QIcon(pm)


def punnet_icon(size: int = 24) -> QIcon:
    """A punnet of tomatoes: three red fruit in a light tray (the Track
    button, Chris, 2026-09-12)."""
    from PySide6.QtGui import QPolygonF

    pm, painter, s = _start(size)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("#e74c3c"))
    for cx in (0.30, 0.50, 0.70):
        painter.drawEllipse(QPointF(s * cx, s * 0.46), s * 0.13, s * 0.13)
    painter.setBrush(QColor("#2ecc71"))
    for cx in (0.30, 0.50, 0.70):
        painter.drawEllipse(QPointF(s * cx, s * 0.33), s * 0.045, s * 0.03)
    # the tray: a shallow trapezoid, lighter front face
    painter.setBrush(QColor(theme.TEXT_BRIGHT))
    painter.drawPolygon(QPolygonF([QPointF(s * 0.12, s * 0.52), QPointF(s * 0.88, s * 0.52), QPointF(s * 0.80, s * 0.84), QPointF(s * 0.20, s * 0.84)]))
    painter.setBrush(QColor(theme.TEXT_MUTED))
    painter.drawRect(QRectF(s * 0.12, s * 0.52, s * 0.76, s * 0.06))
    painter.end()
    return QIcon(pm)


def sync_icon(size: int = 24) -> QIcon:
    """A clock face with hands: the camera-clock sync (the Sync button,
    Chris, 2026-09-12)."""
    pm, painter, s = _start(size)
    pen = QPen(QColor(theme.TEXT_BRIGHT))
    pen.setWidthF(s * 0.09)
    pen.setCapStyle(Qt.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    c = QPointF(s / 2, s / 2)
    painter.drawEllipse(c, s * 0.36, s * 0.36)
    painter.drawLine(c, QPointF(s * 0.50, s * 0.26))   # minute hand up
    painter.drawLine(c, QPointF(s * 0.66, s * 0.58))   # hour hand to four o'clock
    painter.end()
    return QIcon(pm)
