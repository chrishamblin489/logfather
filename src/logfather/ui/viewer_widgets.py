"""Small reusable viewer widgets and their geometry helpers.

Extracted from replay_view: scrub/frame labels, LCD segment display, the
event marker bars, drift and clip-range sliders, and the log list model.
"""
from __future__ import annotations

import math

from PySide6.QtCore import Qt, Signal, QPointF, QRect, QAbstractListModel, QModelIndex
from PySide6.QtGui import QColor, QPainter, QPen, QBrush, QImage, QPolygonF
from PySide6.QtWidgets import QLabel, QLCDNumber, QMenu, QSlider, QWidget, QStyle, QStyleOptionSlider


def _distance_to_segment(px, py, x1, y1, x2, y2) -> float:
    vx = x2 - x1
    vy = y2 - y1
    wx = px - x1
    wy = py - y1
    denom = vx * vx + vy * vy
    if denom <= 0.0:
        return math.hypot(px - x1, py - y1)
    t = (wx * vx + wy * vy) / denom
    t = max(0.0, min(1.0, t))
    proj_x = x1 + t * vx
    proj_y = y1 + t * vy
    return math.hypot(px - proj_x, py - proj_y)


def _dist(a: QPointF, b: QPointF) -> float:
    return math.hypot(a.x() - b.x(), a.y() - b.y())



# -------- Custom video label to handle scroll wheel scrubbing --------

class ScrubbableLabel(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._scrub_callback = None

    def set_scrub_callback(self, cb):
        """cb(delta_frames: int)"""
        self._scrub_callback = cb

    def wheelEvent(self, event):
        if self._scrub_callback is not None:
            delta = event.angleDelta().y()
            if delta > 0:
                self._scrub_callback(-1)
            elif delta < 0:
                self._scrub_callback(1)
            event.accept()
        else:
            super().wheelEvent(event)


class VideoFrameLabel(ScrubbableLabel):
    def __init__(self, text="", parent=None):
        super().__init__(parent)
        if text:
            self.setText(text)
        self._frame: QImage | None = None

    def set_frame(self, frame: QImage | None):
        self._frame = frame
        self.update()

    def paintEvent(self, event):
        if self._frame is None or self.width() <= 1 or self.height() <= 1:
            return super().paintEvent(event)
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#000000"))
        img = self._frame
        if img is None:
            painter.end()
            return
        target = self.rect()
        img_size = img.size()
        img_size.scale(target.size(), Qt.KeepAspectRatio)
        x = target.x() + (target.width() - img_size.width()) // 2
        y = target.y() + (target.height() - img_size.height()) // 2
        painter.drawImage(QRect(x, y, img_size.width(), img_size.height()), img)
        painter.end()


class SegmentDisplay(QLCDNumber):
    # Compatibility shim so existing QLabel-style updates (`setText`) still work.
    def setText(self, text: str):
        self.display(str(text))



class EventMarkerBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._markers: list[tuple[float, QColor]] = []
        self._left_pad = 0
        self._right_pad = 0
        self._triangle_red_markers = False
        self.setMinimumHeight(12)

    def set_markers(self, markers: list[tuple[float, str]]):
        converted: list[tuple[float, QColor]] = []
        for ratio, color in markers:
            r = max(0.0, min(1.0, float(ratio)))
            try:
                q_color = QColor(color)
                if not q_color.isValid():
                    q_color = QColor("#ffffff")
            except Exception:
                q_color = QColor("#ffffff")
            converted.append((r, q_color))
        self._markers = converted
        self.update()

    def set_track_padding(self, left: int, right: int):
        left = max(0, int(left))
        right = max(0, int(right))
        if left == self._left_pad and right == self._right_pad:
            return
        self._left_pad = left
        self._right_pad = right
        self.update()

    def clear(self):
        self._markers = []
        self.update()

    def set_triangle_red_markers(self, enabled: bool):
        enabled = bool(enabled)
        if self._triangle_red_markers == enabled:
            return
        self._triangle_red_markers = enabled
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        rect = self.rect()
        painter.fillRect(rect, QColor("#1e1e1e"))
        if not self._markers:
            return
        track_rect = rect.adjusted(self._left_pad, 0, -self._right_pad, 0)
        if track_rect.width() <= 0:
            return
        baseline = track_rect.height() - 1
        painter.setRenderHint(QPainter.Antialiasing, False)
        for ratio, color in self._markers:
            span = max(1, track_rect.width() - 1)
            x = int(track_rect.left() + ratio * span)
            is_red_marker = (
                self._triangle_red_markers
                and color.red() >= 180
                and color.red() > (color.green() + 40)
                and color.red() > (color.blue() + 40)
            )
            if is_red_marker:
                painter.save()
                painter.setRenderHint(QPainter.Antialiasing, True)
                painter.setPen(Qt.NoPen)
                painter.setBrush(color)
                triangle_half_width = 4
                triangle_height = min(track_rect.height(), 7)
                triangle = QPolygonF(
                    [
                        QPointF(x, track_rect.top()),
                        QPointF(x - triangle_half_width, track_rect.top() + triangle_height),
                        QPointF(x + triangle_half_width, track_rect.top() + triangle_height),
                    ]
                )
                painter.drawPolygon(triangle)
                painter.restore()
                continue
            pen = QPen(color)
            pen.setWidth(2)
            painter.setPen(pen)
            painter.drawLine(x, track_rect.top(), x, baseline)
        painter.end()


class DriftSlider(QSlider):
    """Compact centre-zero slider with a red offset indicator."""

    def __init__(self, orientation, parent=None):
        super().__init__(orientation, parent)
        self.setFixedWidth(96)
        self.setFixedHeight(18)
        self.setCursor(Qt.PointingHandCursor)

    def _track_rect(self) -> QRect:
        return self.rect().adjusted(8, 5, -8, -5)

    def _value_to_x(self, value: int) -> float:
        track = self._track_rect()
        rng = max(1, self.maximum() - self.minimum())
        ratio = (value - self.minimum()) / rng
        return track.left() + ratio * track.width()

    def _set_value_from_x(self, x: float) -> None:
        track = self._track_rect()
        if track.width() <= 0:
            return
        clamped_x = max(track.left(), min(track.right(), x))
        ratio = (clamped_x - track.left()) / max(1, track.width())
        value = self.minimum() + ratio * (self.maximum() - self.minimum())
        self.setValue(int(round(value)))

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._set_value_from_x(event.position().x())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton:
            self._set_value_from_x(event.position().x())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def paintEvent(self, event):
        _ = event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        track = self._track_rect()
        cy = track.center().y()
        left = track.left()
        right = track.right()
        zero_value = 0 if self.minimum() <= 0 <= self.maximum() else self.minimum()
        mid_x = self._value_to_x(zero_value)
        value_x = self._value_to_x(self.value())

        painter.setPen(QPen(QColor("#4a5563"), 2))
        painter.drawLine(left, cy, right, cy)

        painter.setPen(QPen(QColor("#8b949e"), 1))
        painter.drawLine(int(mid_x), cy - 5, int(mid_x), cy + 5)

        painter.setPen(QPen(QColor("#d33b3b"), 2))
        painter.drawLine(QPointF(mid_x, cy), QPointF(value_x, cy))

        painter.setPen(QPen(QColor("#f0f6fc"), 1))
        painter.setBrush(QBrush(QColor("#f0f6fc")))
        painter.drawEllipse(QPointF(value_x, cy), 4, 4)
        painter.end()


class ClipRangeSlider(QSlider):
    clip_range_export_requested = Signal(int, int)

    def __init__(self, orientation, parent=None):
        super().__init__(orientation, parent)
        self._clip_start_value: int | None = None
        self._clip_end_value: int | None = None
        self._drag_handle: str | None = None
        self.setContextMenuPolicy(Qt.DefaultContextMenu)

    def clear_clip_range(self):
        self._clip_start_value = None
        self._clip_end_value = None
        self._drag_handle = None
        self.unsetCursor()
        self.update()

    def has_clip_range(self) -> bool:
        return self._clip_start_value is not None and self._clip_end_value is not None

    def ordered_clip_range(self) -> tuple[int, int] | None:
        if self._clip_start_value is None or self._clip_end_value is None:
            return None
        start = int(self._clip_start_value)
        end = int(self._clip_end_value)
        if start <= end:
            return start, end
        return end, start

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            handle = self._handle_at_pos(event.position().toPoint())
            if handle is not None:
                self._drag_handle = handle
                self._update_drag_handle(event.position().toPoint())
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_handle is not None:
            self._update_drag_handle(event.position().toPoint())
            event.accept()
            return
        if self._handle_at_pos(event.position().toPoint()) is not None:
            self.setCursor(Qt.SizeHorCursor)
        else:
            self.unsetCursor()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.RightButton:
            self._show_context_menu(event.position().toPoint())
            event.accept()
            return
        if event.button() == Qt.LeftButton and self._drag_handle is not None:
            self._update_drag_handle(event.position().toPoint())
            self._drag_handle = None
            self.unsetCursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        groove = self._groove_rect()
        if not groove.isValid():
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        ordered = self.ordered_clip_range()
        if ordered is not None:
            start_x = self._value_to_x(ordered[0], groove)
            end_x = self._value_to_x(ordered[1], groove)
            left_x = min(start_x, end_x)
            width = max(2, abs(end_x - start_x))
            fill_rect = QRect(int(left_x), groove.top(), int(width), groove.height())
            painter.fillRect(fill_rect, QColor(90, 145, 220, 70))
        for value, color in (
            (self._clip_start_value, QColor("#7dd3fc")),
            (self._clip_end_value, QColor("#fbbf24")),
        ):
            if value is None:
                continue
            x = self._value_to_x(value, groove)
            pen = QPen(color)
            pen.setWidth(2)
            painter.setPen(pen)
            painter.drawLine(int(x), groove.top() - 3, int(x), groove.bottom() + 3)
            painter.setBrush(color)
            painter.setPen(QPen(QColor("#101010")))
            painter.drawEllipse(QPointF(x, groove.center().y()), 5, 5)
        painter.end()

    def _show_context_menu(self, pos):
        click_value = self._pos_to_value(pos)
        menu = QMenu(self)
        set_start_action = menu.addAction("Set Clip Start")
        set_end_action = menu.addAction("Set Clip End")
        clear_action = None
        export_action = None
        if self.has_clip_range() and self._value_within_range(click_value):
            menu.addSeparator()
            export_action = menu.addAction("Export Clip")
            clear_action = menu.addAction("Clear Clip Range")
        elif self._clip_start_value is not None or self._clip_end_value is not None:
            menu.addSeparator()
            clear_action = menu.addAction("Clear Clip Range")
        chosen = menu.exec(self.mapToGlobal(pos))
        if chosen == set_start_action:
            self._clip_start_value = click_value
            if self._clip_end_value is not None and self._clip_end_value < click_value:
                self._clip_end_value = click_value
            self.update()
        elif chosen == set_end_action:
            self._clip_end_value = click_value
            if self._clip_start_value is not None and self._clip_start_value > click_value:
                self._clip_start_value = click_value
            self.update()
        elif chosen == clear_action:
            self.clear_clip_range()
        elif chosen == export_action:
            ordered = self.ordered_clip_range()
            if ordered is not None and ordered[1] > ordered[0]:
                self.clip_range_export_requested.emit(ordered[0], ordered[1])

    def _update_drag_handle(self, pos):
        value = self._pos_to_value(pos)
        if self._drag_handle == "start":
            if self._clip_end_value is not None and value > self._clip_end_value:
                value = self._clip_end_value
            self._clip_start_value = value
        elif self._drag_handle == "end":
            if self._clip_start_value is not None and value < self._clip_start_value:
                value = self._clip_start_value
            self._clip_end_value = value
        self.update()

    def _handle_at_pos(self, pos) -> str | None:
        groove = self._groove_rect()
        if not groove.isValid():
            return None
        margin = 8
        if self._clip_start_value is not None:
            x = self._value_to_x(self._clip_start_value, groove)
            if abs(pos.x() - x) <= margin:
                return "start"
        if self._clip_end_value is not None:
            x = self._value_to_x(self._clip_end_value, groove)
            if abs(pos.x() - x) <= margin:
                return "end"
        return None

    def _value_within_range(self, value: int) -> bool:
        ordered = self.ordered_clip_range()
        if ordered is None:
            return False
        return ordered[0] <= value <= ordered[1]

    def _groove_rect(self):
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        return self.style().subControlRect(QStyle.CC_Slider, opt, QStyle.SC_SliderGroove, self)

    def _pos_to_value(self, pos) -> int:
        groove = self._groove_rect()
        if not groove.isValid():
            return self.minimum()
        span = max(1, groove.width() - 1)
        ratio = (pos.x() - groove.left()) / span
        ratio = max(0.0, min(1.0, ratio))
        return int(round(self.minimum() + ratio * (self.maximum() - self.minimum())))

    def _value_to_x(self, value: int, groove) -> float:
        rng = max(1, self.maximum() - self.minimum())
        ratio = (value - self.minimum()) / rng
        return groove.left() + ratio * max(1, groove.width() - 1)


_HIGHLIGHT_ACTIVE_BG  = QColor("#cc2222")  # red   — playhead is inside this event
_HIGHLIGHT_NEAREST_BG = QColor("#7a4800")  # amber — next upcoming event (forward bound)
_HIGHLIGHT_FG         = QColor("#ffffff")
_FAULT_FG             = QColor("#ff7a45")  # orange-red — a fault or error line (Chris, 2026-09-10)
_FAULT_MARKERS = ("Fault on motor", "_error |", "| error |", "Warning update", "emergency_stop", "protective_stop")


def is_fault_row(text: str) -> bool:
    """A log line worth colouring: actuator faults, error states, drive
    warnings and stops, so they stand out among hundreds of routine lines."""
    return any(marker in text for marker in _FAULT_MARKERS)


class LogListModel(QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[str] = []
        self._active: set[int] = set()
        self._nearest: int | None = None

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._rows)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid() or index.row() >= len(self._rows):
            return None
        row = index.row()
        if role == Qt.DisplayRole:
            return self._rows[row]
        if role == Qt.BackgroundRole:
            if row in self._active:
                return _HIGHLIGHT_ACTIVE_BG
            if row == self._nearest:
                return _HIGHLIGHT_NEAREST_BG
        if role == Qt.ForegroundRole:
            if row in self._active or row == self._nearest:
                return _HIGHLIGHT_FG
            if is_fault_row(self._rows[row]):
                return _FAULT_FG
        return None

    def reset_data(self, rows: list[str]) -> None:
        self.beginResetModel()
        self._rows = rows
        self._active = set()
        self._nearest = None
        self.endResetModel()

    _HIGHLIGHT_ROLES = [Qt.BackgroundRole, Qt.ForegroundRole]

    def set_highlights(self, active: set[int], nearest: int | None) -> None:
        if active == self._active and nearest == self._nearest:
            return
        changed_rows = self._active | ({self._nearest} if self._nearest is not None else set())
        changed_rows |= active | ({nearest} if nearest is not None else set())
        self._active = active
        self._nearest = nearest
        for row in changed_rows:
            idx = self.index(row)
            self.dataChanged.emit(idx, idx, self._HIGHLIGHT_ROLES)


