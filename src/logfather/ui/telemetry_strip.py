"""Telemetry tab in System Replay (Chris, 2026-09-07): the day's
temperatures and motor currents for the chosen system, one small chart per
group, with the playhead drawn across them so a spike lines up with the
footage and the log. Hover for the values at any time of day."""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QLabel, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

from logfather.core.telemetry import TelemetryDay, TrackGroup, downsample, value_range
from logfather.core.timeline_model import ensure_playhead_local
from logfather.ui import theme

TRACK_COLOURS = ["#5e9bff", "#ff8a65", "#2ecc71", "#f1c40f", "#d46bff", "#36cfc9", "#ff85c0", "#c0c0c0"]
PLAYHEAD = "#ff4d4f"
MARGIN_LEFT, MARGIN_RIGHT, MARGIN_TOP, MARGIN_BOTTOM = 52, 12, 26, 22


class _GroupChart(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._group: TrackGroup | None = None
        self._t0 = 0
        self._t1 = 1
        self._playhead_ms: int | None = None
        self._hover_x: float | None = None
        self.setMinimumHeight(150)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMouseTracking(True)

    def set_group(self, group: TrackGroup, t0: int, t1: int) -> None:
        self._group, self._t0, self._t1 = group, t0, t1
        self.update()

    def set_playhead_ms(self, t_ms: int | None) -> None:
        if t_ms != self._playhead_ms:
            self._playhead_ms = t_ms
            self.update()

    # ---- geometry -------------------------------------------------------
    def _plot_rect(self) -> QRectF:
        return QRectF(MARGIN_LEFT, MARGIN_TOP, max(1.0, self.width() - MARGIN_LEFT - MARGIN_RIGHT), max(1.0, self.height() - MARGIN_TOP - MARGIN_BOTTOM))

    def _x_for(self, t_ms: int, plot: QRectF) -> float:
        return plot.left() + (t_ms - self._t0) / max(1, self._t1 - self._t0) * plot.width()

    def _t_for(self, x: float, plot: QRectF) -> int:
        return int(self._t0 + (x - plot.left()) / max(1.0, plot.width()) * (self._t1 - self._t0))

    # ---- events ---------------------------------------------------------
    def mouseMoveEvent(self, event):
        self._hover_x = event.position().x()
        self.update()

    def leaveEvent(self, event):
        self._hover_x = None
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(theme.BG_DEEP))
        plot = self._plot_rect()
        group = self._group
        title_font = QFont(self.font())
        title_font.setBold(True)
        p.setFont(title_font)
        p.setPen(QColor(theme.TEXT_BRIGHT))
        title = group.name if group else ""
        p.drawText(QRectF(8, 4, self.width() - 16, 18), Qt.AlignLeft | Qt.AlignVCenter, title)
        legend_x = 8 + p.fontMetrics().horizontalAdvance(title) + 18
        p.setFont(self.font())
        if group is None or not group.tracks:
            return
        lo, hi = value_range(group.tracks)

        def y_for(v: float) -> float:
            return plot.bottom() - (v - lo) / (hi - lo) * plot.height()

        # hour grid + axis labels
        small = QFont(self.font())
        small.setPointSizeF(max(7.0, self.font().pointSizeF() - 2))
        p.setFont(small)
        grid = QPen(QColor(theme.BORDER))
        grid.setWidthF(1.0)
        for h in range(0, 25, 3):
            x = plot.left() + h / 24 * plot.width()
            p.setPen(grid)
            p.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            p.setPen(QColor(theme.TEXT_MUTED))
            p.drawText(QRectF(x - 20, plot.bottom() + 3, 40, 16), Qt.AlignHCenter | Qt.AlignTop, f"{h:02d}")
        for frac, value in ((0.0, lo), (1.0, hi)):
            y = plot.bottom() - frac * plot.height()
            p.setPen(QColor(theme.TEXT_MUTED))
            p.drawText(QRectF(0, y - 8, MARGIN_LEFT - 6, 16), Qt.AlignRight | Qt.AlignVCenter, f"{value:.0f}{group.unit}")

        # tracks as min/max columns (fast, keeps every spike visible)
        columns = max(1, int(plot.width()))
        for i, track in enumerate(group.tracks):
            colour = QColor(TRACK_COLOURS[i % len(TRACK_COLOURS)])
            pen = QPen(colour)
            pen.setWidthF(1.4)
            p.setPen(pen)
            last: tuple[float, float] | None = None
            for c, vmin, vmax in downsample(track.times_ms, track.values, self._t0, self._t1, columns):
                x = plot.left() + c + 0.5
                y0, y1 = y_for(vmax), y_for(vmin)
                if last is not None and c - last[0] <= 2:
                    p.drawLine(QPointF(last[1], last[2]), QPointF(x, (y0 + y1) / 2))
                p.drawLine(QPointF(x, y0), QPointF(x, y1 if y1 - y0 > 0.8 else y0 + 0.8))
                last = (c, x, (y0 + y1) / 2)

        # legend, to the right of the title
        x = max(plot.left(), legend_x)
        for i, track in enumerate(group.tracks):
            colour = QColor(TRACK_COLOURS[i % len(TRACK_COLOURS)])
            p.setPen(Qt.NoPen)
            p.setBrush(colour)
            p.drawRect(QRectF(x, 8, 10, 10))
            p.setPen(QColor(theme.TEXT))
            width = p.fontMetrics().horizontalAdvance(track.name) + 18
            p.drawText(QRectF(x + 14, 4, width, 18), Qt.AlignLeft | Qt.AlignVCenter, track.name)
            x += width + 6

        # playhead
        if self._playhead_ms is not None and self._t0 <= self._playhead_ms <= self._t1:
            x = self._x_for(self._playhead_ms, plot)
            pen = QPen(QColor(PLAYHEAD))
            pen.setWidthF(1.6)
            p.setPen(pen)
            p.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))

        # hover readout
        if self._hover_x is not None and plot.left() <= self._hover_x <= plot.right():
            t = self._t_for(self._hover_x, plot)
            pen = QPen(QColor(theme.TEXT_MUTED))
            pen.setStyle(Qt.DashLine)
            p.setPen(pen)
            p.drawLine(QPointF(self._hover_x, plot.top()), QPointF(self._hover_x, plot.bottom()))
            when = ensure_playhead_local(datetime.fromtimestamp(t / 1000)).strftime("%H:%M")
            parts = [when]
            for track in group.tracks:
                v = track.value_at(t)
                parts.append(f"{track.name} {v:.1f}{group.unit}" if v is not None else f"{track.name} –")
            text = "   ".join(parts)
            width = p.fontMetrics().horizontalAdvance(text) + 12
            bx = min(self._hover_x + 8, plot.right() - width)
            box = QRectF(max(plot.left(), bx), plot.top() + 4, width, 18)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(theme.BG_RAISED))
            p.drawRoundedRect(box, 4, 4)
            p.setPen(QColor(theme.TEXT_BRIGHT))
            p.drawText(box, Qt.AlignCenter, text)


class TelemetryPanel(QWidget):
    """The Telemetry tab: a status line and one chart per track group."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._data: TelemetryDay | None = None
        self._charts: list[_GroupChart] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)
        self._status = QLabel("Choose a system and a day.")
        self._status.setStyleSheet(theme.MUTED_LABEL)
        self._status.setWordWrap(True)
        layout.addWidget(self._status)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.NoFrame)
        self._body = QWidget()
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(0, 0, 0, 0)
        self._body_layout.setSpacing(6)
        self._body_layout.addStretch(1)
        self._scroll.setWidget(self._body)
        layout.addWidget(self._scroll, 1)

    def set_data(self, data: TelemetryDay | None, status: str | None = None) -> None:
        self._data = data
        for chart in self._charts:
            self._body_layout.removeWidget(chart)
            chart.deleteLater()
        self._charts = []
        if data is None or data.is_empty():
            self._status.setText(status or "No telemetry for this system and day.")
            return
        for group in data.groups:
            chart = _GroupChart(self._body)
            chart.set_group(group, data.day_start_ms, data.day_end_ms)
            self._body_layout.insertWidget(self._body_layout.count() - 1, chart)
            self._charts.append(chart)
        tracks = sum(len(g.tracks) for g in data.groups)
        self._status.setText(status or f"{data.robot_id}: {tracks} signals, sampled every 30 s. Hover for values; the red line is the playhead.")

    def set_playhead(self, dt: datetime | None) -> None:
        t_ms = None if dt is None else int(ensure_playhead_local(dt).timestamp() * 1000)
        for chart in self._charts:
            chart.set_playhead_ms(t_ms)
