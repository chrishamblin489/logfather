"""AnnotatedVideoWidget: the video canvas with drawing/measuring annotations,
status overlay lines, target overlays, and the Bird's-Eye tray view.

Extracted verbatim from replay_view; talks to the viewer only via signals.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

import cv2
import numpy as np

from PySide6.QtCore import Qt, Signal, QEvent, QPointF, QRect, QRectF, QTimer
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QImage,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
    QTransform,
)
from PySide6.QtWidgets import (
    QColorDialog,
    QInputDialog,
    QLabel,
    QMenu,
    QVBoxLayout,
    QWidget,
)

from logfather.ui.app_assets import load_placeholder_image as _load_placeholder_image
from logfather.ui.viewer_widgets import _dist, _distance_to_segment


class AnnotatedVideoWidget(QWidget):
    annotation_created = Signal(dict)
    annotation_context_requested = Signal(int, object)
    annotation_updated = Signal(int, dict)

    def __init__(self, placeholder_text: str = "", parent=None):
        super().__init__(parent)
        self._frame: QImage | None = None
        self._annotations: list[dict] = []
        self._tool = "line"
        self._color = QColor("#ffcc00")
        self._placeholder_text = placeholder_text
        self._placeholder_image: QImage | None = None
        # A warning shown in place of footage (Chris, 2026-09-07: "CCTV
        # footage is deleted after 30 days"), with a camera icon.
        self._notice: str | None = None
        self._pending_start: QPointF | None = None
        self._pending_end: QPointF | None = None
        self._editable = True
        self._current_frame_index = 0
        self._scrub_callback = None
        self._key_handler = None
        self._edit_idx: int | None = None
        self._drag_handle: str | None = None
        self._timed_start: dict | None = None
        self._tray_points: list[QPointF] = []
        self._birds_eye_max = 220
        self._birds_eye_window: QWidget | None = None
        self._birds_eye_label: QLabel | None = None
        self._last_birds_eye: QImage | None = None
        self._tray_update_cb = None
        self._fps = 0.0
        self._status_lines: list[str] = []
        self._target_overlays: list[dict] = []
        self.setMouseTracking(True)

    def set_frame(self, frame: QImage | None):
        self._frame = frame
        self.update()

    def set_placeholder_text(self, text: str):
        self._placeholder_text = text
        self.update()

    def set_placeholder_image(self, image: QImage | None):
        self._placeholder_image = image
        self.update()

    def set_notice(self, text: str | None):
        self._notice = str(text) if text else None
        self.update()

    def _paint_notice(self, painter: QPainter) -> None:
        rect = self.rect()
        amber = QColor("#e2a53a")
        size = max(56, min(rect.width(), rect.height()) // 5)
        cx = rect.center().x()
        cy = rect.center().y() - size // 3
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(amber)
        # Camera body, lens hood and wall bracket: a dome-less CCTV camera.
        body = QRectF(cx - size * 0.55, cy - size * 0.28, size * 0.85, size * 0.4)
        painter.drawRoundedRect(body, size * 0.06, size * 0.06)
        hood = QPolygonF([
            QPointF(cx + size * 0.28, cy - size * 0.22),
            QPointF(cx + size * 0.62, cy - size * 0.36),
            QPointF(cx + size * 0.62, cy + size * 0.02),
            QPointF(cx + size * 0.28, cy + size * 0.1),
        ])
        painter.drawPolygon(hood)
        painter.drawRect(QRectF(cx - size * 0.62, cy - size * 0.02, size * 0.12, size * 0.5))
        painter.drawRect(QRectF(cx - size * 0.8, cy + size * 0.42, size * 0.5, size * 0.08))
        painter.setBrush(QColor("#000000"))
        painter.drawEllipse(QPointF(cx + size * 0.5, cy - size * 0.13), size * 0.07, size * 0.07)
        # A slash across it: no footage.
        pen = QPen(QColor("#f25c4c"))
        pen.setWidthF(max(3.0, size * 0.07))
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.drawLine(QPointF(cx - size * 0.7, cy + size * 0.5), QPointF(cx + size * 0.7, cy - size * 0.5))
        font = QFont(self.font())
        font.setPointSize(max(12, self.font().pointSize() + 4))
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor("#f3d9a4"))
        text_rect = QRectF(rect.left() + 16, cy + size * 0.6, rect.width() - 32, size)
        painter.drawText(text_rect, Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap, self._notice or "")

    def set_annotations(self, annotations: list[dict]):
        self._annotations = list(annotations)
        if self._edit_idx is not None and self._edit_idx >= len(self._annotations):
            self._edit_idx = None
        self.update()

    def set_tool(self, tool: str):
        self._tool = tool
        if tool != "timed_line":
            self._timed_start = None
        if tool != "tray":
            self._tray_points = []
            self.update()
            if tool != "tray":
                self._clear_birds_eye_popout()

    def set_color(self, color: QColor):
        self._color = QColor(color)

    def set_editable(self, editable: bool):
        self._editable = bool(editable)

    def set_current_frame_index(self, frame_index: int):
        self._current_frame_index = int(frame_index)
        self.update()

    def set_scrub_callback(self, cb):
        """cb(delta_frames: int)"""
        self._scrub_callback = cb

    def set_key_handler(self, cb):
        """cb(QKeyEvent)"""
        self._key_handler = cb

    def set_tray_update_callback(self, cb):
        """cb() called when tray points change."""
        self._tray_update_cb = cb

    def set_fps(self, fps: float):
        self._fps = float(fps or 0.0)

    def set_status_lines(self, lines: list[str] | None):
        self._status_lines = [str(line) for line in (lines or []) if str(line).strip()]
        self.update()

    def set_target_overlays(self, overlays: list[dict]) -> None:
        """
        overlays: list of dicts with keys:
          norm_x, norm_y  — position in [0..1] of frame dimensions
          label           — text drawn next to the dot (optional)
          color           — hex string (optional, default #3498db)
        """
        self._target_overlays = list(overlays)
        self.update()

    def eventFilter(self, obj, event):
        if obj is self._birds_eye_window and event.type() == QEvent.Resize:
            if self._last_birds_eye is not None:
                self._update_birds_eye_popout(self._last_birds_eye)
        return super().eventFilter(obj, event)

    def set_edit_index(self, idx: int | None):
        if idx is None:
            self._edit_idx = None
        else:
            self._edit_idx = int(idx)
        self.update()

    def get_edit_index(self) -> int | None:
        return self._edit_idx

    def _image_rect(self) -> QRect:
        if self._frame is None or self.width() <= 1 or self.height() <= 1:
            return QRect(0, 0, 0, 0)
        img_w = self._frame.width()
        img_h = self._frame.height()
        scale = min(self.width() / img_w, self.height() / img_h)
        draw_w = int(img_w * scale)
        draw_h = int(img_h * scale)
        x = (self.width() - draw_w) // 2
        y = (self.height() - draw_h) // 2
        return QRect(x, y, draw_w, draw_h)

    def _map_to_image(self, pos) -> QPointF | None:
        if self._frame is None:
            return None
        rect = self._image_rect()
        if rect.width() <= 0 or rect.height() <= 0:
            return None
        if not rect.contains(int(pos.x()), int(pos.y())):
            return None
        scale = rect.width() / self._frame.width()
        x = (pos.x() - rect.x()) / scale
        y = (pos.y() - rect.y()) / scale
        return QPointF(x, y)

    def _map_from_image(self, pt: QPointF) -> QPointF:
        rect = self._image_rect()
        if rect.width() <= 0 or rect.height() <= 0:
            return QPointF(0, 0)
        scale = rect.width() / self._frame.width()
        x = rect.x() + pt.x() * scale
        y = rect.y() + pt.y() * scale
        return QPointF(x, y)

    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton and self._editable:
            idx = self._hit_test_annotation(event.position())
            if idx is not None:
                self.annotation_context_requested.emit(idx, event.globalPosition())
                return
        if self._frame is None or event.button() != Qt.LeftButton or not self._editable:
            return
        if self._tool == "tray":
            # Only allow one bird's eye region at a time. If one exists, switch to edit.
            existing_idx = None
            for i, ann in enumerate(self._annotations):
                if ann.get("type") == "tray":
                    existing_idx = i
                    break
            if existing_idx is not None:
                self.set_edit_index(existing_idx)
                handle = self._hit_test_handle(event.position())
                if handle is not None:
                    self._drag_handle = handle
                return
            img_pt = self._map_to_image(event.position())
            if img_pt is None:
                return
            self._tray_points.append(img_pt)
            if len(self._tray_points) >= 4:
                ann = {
                    "type": "tray",
                    "points": [[p.x(), p.y()] for p in self._tray_points[:4]],
                    "color": self._color.name(),
                    "pinned": False,
                }
                birds_eye = self._build_birds_eye(self._tray_points[:4])
                if birds_eye is not None:
                    self._last_birds_eye = birds_eye
                self._tray_points = []
                self.annotation_created.emit(ann)
            self.update()
            return
        if self._edit_idx is not None:
            handle = self._hit_test_handle(event.position())
            if handle is not None:
                self._drag_handle = handle
                return
        img_pt = self._map_to_image(event.position())
        if img_pt is None:
            return
        self._pending_start = img_pt
        self._pending_end = img_pt
        self.update()

    def mouseMoveEvent(self, event):
        if self._pending_start is None or not self._editable:
            if self._drag_handle and self._edit_idx is not None:
                ann = self._annotations[self._edit_idx]
                img_pt = self._map_to_image(event.position())
                if img_pt is None:
                    return
                if self._drag_handle.startswith("tray:") and ann.get("type") == "tray":
                    try:
                        idx = int(self._drag_handle.split(":")[1])
                    except Exception:
                        return
                    pts = ann.get("points") or []
                    if len(pts) == 4 and 0 <= idx < 4:
                        pts[idx] = [img_pt.x(), img_pt.y()]
                        ann["points"] = pts
                        birds_eye = self._build_birds_eye([QPointF(p[0], p[1]) for p in pts])
                        if birds_eye is not None:
                            self._last_birds_eye = birds_eye
                        if self._birds_eye_window is not None and self._birds_eye_window.isVisible():
                            self._update_birds_eye_popout(birds_eye or self._last_birds_eye)
                        if self._tray_update_cb is not None:
                            self._tray_update_cb()
                elif self._drag_handle == "start":
                    ann["start"] = [img_pt.x(), img_pt.y()]
                elif self._drag_handle == "end":
                    ann["end"] = [img_pt.x(), img_pt.y()]
                self.update()
            return
        img_pt = self._map_to_image(event.position())
        if img_pt is None:
            return
        self._pending_end = img_pt
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.LeftButton or self._pending_start is None or not self._editable:
            if self._drag_handle and self._edit_idx is not None:
                ann = self._annotations[self._edit_idx]
                self._drag_handle = None
                self.annotation_updated.emit(self._edit_idx, ann)
                self.update()
            return
        img_pt = self._map_to_image(event.position())
        if img_pt is None:
            self._pending_start = None
            self._pending_end = None
            self.update()
            return
        start = self._pending_start
        end = img_pt
        self._pending_start = None
        self._pending_end = None
        if self._tool == "text":
            text, ok = QInputDialog.getText(self, "Add label", "Label text:")
            if ok and text.strip():
                ann = {
                    "type": "text",
                    "pos": [start.x(), start.y()],
                    "text": text.strip(),
                    "color": self._color.name(),
                    "pinned": False,
                }
                self.annotation_created.emit(ann)
        elif self._tool == "timed_line":
            if self._timed_start is None:
                self._timed_start = {
                    "pos": [start.x(), start.y()],
                    "frame_index": self._current_frame_index,
                }
            else:
                ann = {
                    "type": "timed_line",
                    "start": list(self._timed_start.get("pos", [start.x(), start.y()])),
                    "end": [end.x(), end.y()],
                    "start_frame": int(self._timed_start.get("frame_index", self._current_frame_index)),
                    "end_frame": int(self._current_frame_index),
                    "color": self._color.name(),
                    "pinned": False,
                }
                self._timed_start = None
                self.annotation_created.emit(ann)
        else:
            ann = {
                "type": self._tool,
                "start": [start.x(), start.y()],
                "end": [end.x(), end.y()],
                "color": self._color.name(),
                "pinned": False,
            }
            self.annotation_created.emit(ann)
        self.update()

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

    def keyPressEvent(self, event):
        if self._key_handler is not None:
            self._key_handler(event)
        else:
            super().keyPressEvent(event)

    def _handle_points_for_annotation(self, ann: dict) -> tuple[QPointF, QPointF] | None:
        if ann.get("type") not in ("line", "arrow", "measure", "timed_line"):
            return None
        start = ann.get("start") or [0, 0]
        end = ann.get("end") or [0, 0]
        start_pt = self._map_from_image(QPointF(start[0], start[1]))
        end_pt = self._map_from_image(QPointF(end[0], end[1]))
        return start_pt, end_pt

    def _hit_test_handle(self, pos) -> str | None:
        if self._edit_idx is None:
            return None
        ann = self._annotations[self._edit_idx]
        if ann.get("type") == "tray":
            pts = ann.get("points") or []
            if len(pts) != 4:
                return None
            widget_pts = [self._map_from_image(QPointF(p[0], p[1])) for p in pts]
            for i, pt in enumerate(widget_pts):
                if math.hypot(pt.x() - pos.x(), pt.y() - pos.y()) <= 8:
                    return f"tray:{i}"
            return None
        handle_pts = self._handle_points_for_annotation(ann)
        if handle_pts is None:
            return None
        start_pt, end_pt = handle_pts
        dist_start = math.hypot(start_pt.x() - pos.x(), start_pt.y() - pos.y())
        dist_end = math.hypot(end_pt.x() - pos.x(), end_pt.y() - pos.y())
        if dist_start <= 6:
            return "start"
        if dist_end <= 6:
            return "end"
        return None

    def _hit_test_annotation(self, pos) -> int | None:
        if not self._annotations:
            return None
        best_idx = None
        best_dist = 12.0
        for idx, ann in enumerate(self._annotations):
            if ann.get("type") == "text":
                pt = ann.get("pos") or [0, 0]
                widget_pt = self._map_from_image(QPointF(pt[0], pt[1]))
                dist = math.hypot(widget_pt.x() - pos.x(), widget_pt.y() - pos.y())
            elif ann.get("type") == "tray":
                pts = ann.get("points") or []
                if len(pts) < 2:
                    continue
                img_pts = [self._map_from_image(QPointF(p[0], p[1])) for p in pts]
                dist = 1e9
                for i in range(len(img_pts)):
                    a = img_pts[i]
                    b = img_pts[(i + 1) % len(img_pts)]
                    dist = min(dist, _distance_to_segment(pos.x(), pos.y(), a.x(), a.y(), b.x(), b.y()))
            else:
                start = ann.get("start") or [0, 0]
                end = ann.get("end") or [0, 0]
                s = self._map_from_image(QPointF(start[0], start[1]))
                e = self._map_from_image(QPointF(end[0], end[1]))
                dist = _distance_to_segment(pos.x(), pos.y(), s.x(), s.y(), e.x(), e.y())
            if dist <= best_dist:
                best_dist = dist
                best_idx = idx
        return best_idx

    def _order_tray_points(self, pts: list[QPointF]) -> list[QPointF]:
        if len(pts) != 4:
            return pts
        # Robust ordering: sort by angle around centroid, then rotate so top-left is first.
        cx = sum(p.x() for p in pts) / 4.0
        cy = sum(p.y() for p in pts) / 4.0
        def _ang(p):
            return math.atan2(p.y() - cy, p.x() - cx)
        ordered = sorted(pts, key=_ang)
        # Rotate so top-left (min x+y) is first.
        tl_idx = min(range(4), key=lambda i: ordered[i].x() + ordered[i].y())
        ordered = ordered[tl_idx:] + ordered[:tl_idx]
        # Ensure clockwise order: tl -> tr should have smaller y than tl -> bl
        if ordered[1].y() > ordered[3].y():
            ordered = [ordered[0], ordered[3], ordered[2], ordered[1]]
        return ordered

    def _build_birds_eye(self, pts: list[QPointF]) -> QImage | None:
        if self._frame is None or len(pts) != 4:
            return None
        ordered = self._order_tray_points(pts)
        tl, tr, br, bl = ordered
        edge_w = max(_dist(tl, tr), _dist(bl, br))
        edge_h = max(_dist(tl, bl), _dist(tr, br))
        w = int(edge_w)
        h = int(edge_h)
        w = max(40, min(800, w))
        h = max(40, min(800, h))
        try:
            frame = self._frame
            # Ensure we operate on RGB for predictable OpenCV warp.
            if frame.format() != QImage.Format_RGB888:
                frame = frame.convertToFormat(QImage.Format_RGB888)
            h_src = frame.height()
            w_src = frame.width()
            bytes_per_line = frame.bytesPerLine()
            ptr = frame.bits()
            size = h_src * bytes_per_line
            try:
                ptr.setsize(size)
            except Exception:
                try:
                    ptr = frame.constBits()
                    ptr.setsize(size)
                except Exception:
                    ptr = memoryview(ptr)
            arr = np.frombuffer(ptr, dtype=np.uint8, count=size).reshape((h_src, bytes_per_line // 3, 3))
            arr = arr[:, :w_src, :]
            src = np.array([[p.x(), p.y()] for p in ordered], dtype=np.float32)
            dst = np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], dtype=np.float32)
            mat = cv2.getPerspectiveTransform(src, dst)
            warped = cv2.warpPerspective(arr, mat, (w, h))
            warped = cv2.rotate(warped, cv2.ROTATE_180)
            warped = np.ascontiguousarray(warped)
            qimg = QImage(warped.data, w, h, w * 3, QImage.Format_RGB888)
            return qimg.copy()
        except Exception as exc:
            print(f"[tray] warp failed: {exc}", flush=True)
            return None

    def _update_birds_eye_popout(self, birds_eye: QImage | None):
        if self._birds_eye_window is None:
            win = QWidget(self, Qt.Window)
            win.setWindowTitle("Bird's Eye")
            win.resize(320, 320)
            win.setMinimumSize(200, 200)
            win.installEventFilter(self)
            layout = QVBoxLayout(win)
            layout.setContentsMargins(6, 6, 6, 6)
            label = QLabel("Waiting for Bird's Eye...")
            label.setAlignment(Qt.AlignCenter)
            layout.addWidget(label, 1)
            win.setLayout(layout)
            self._birds_eye_window = win
            self._birds_eye_label = label
            win.destroyed.connect(lambda _=None: self._clear_birds_eye_popout())
            win.show()
        else:
            self._birds_eye_window.show()
        if self._birds_eye_label is None:
            return
        if birds_eye is None or birds_eye.isNull():
            self._birds_eye_label.setText("Bird's Eye unavailable")
            return
        self._last_birds_eye = birds_eye
        max_w = max(1, self._birds_eye_label.width() - 4)
        max_h = max(1, self._birds_eye_label.height() - 4)
        scaled = birds_eye.scaled(max_w, max_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self._birds_eye_label.setPixmap(QPixmap.fromImage(scaled))

    def _clear_birds_eye_popout(self):
        if self._birds_eye_window is not None:
            try:
                self._birds_eye_window.close()
            except Exception:
                pass
        self._birds_eye_window = None
        self._birds_eye_label = None


    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#000000"))
        if self._frame is None:
            if self._notice:
                self._paint_notice(painter)
                painter.end()
                return
            if self._placeholder_image is not None and not self._placeholder_image.isNull():
                img = self._placeholder_image
                target = self.rect()
                img_size = img.size()
                img_size.scale(target.size(), Qt.KeepAspectRatio)
                x = target.x() + (target.width() - img_size.width()) // 2
                y = target.y() + (target.height() - img_size.height()) // 2
                painter.drawImage(QRect(x, y, img_size.width(), img_size.height()), img)
                if self._placeholder_text and self._placeholder_text != "No video loaded":
                    painter.setPen(QColor("#e2e8f0"))
                    painter.drawText(self.rect().adjusted(0, 0, 0, -12), Qt.AlignBottom | Qt.AlignHCenter, self._placeholder_text)
            elif self._placeholder_text:
                painter.setPen(QColor("#888888"))
                painter.drawText(self.rect(), Qt.AlignCenter, self._placeholder_text)
            painter.end()
            return
        rect = self._image_rect()
        painter.drawImage(rect, self._frame)
        painter.setRenderHint(QPainter.Antialiasing, True)
        font = QFont()
        font.setPointSize(10)
        painter.setFont(font)
        metrics = painter.fontMetrics()

        def draw_line(start_pt: QPointF, end_pt: QPointF, color: QColor, arrow: bool, alpha: int):
            pen = QPen(QColor(color.red(), color.green(), color.blue(), alpha))
            pen.setWidth(2)
            painter.setPen(pen)
            painter.drawLine(start_pt, end_pt)
            if arrow:
                dx = end_pt.x() - start_pt.x()
                dy = end_pt.y() - start_pt.y()
                length = math.hypot(dx, dy)
                if length > 0:
                    ux = dx / length
                    uy = dy / length
                    size = 10.0
                    left = QPointF(
                        end_pt.x() - ux * size - uy * size * 0.5,
                        end_pt.y() - uy * size + ux * size * 0.5,
                    )
                    right = QPointF(
                        end_pt.x() - ux * size + uy * size * 0.5,
                        end_pt.y() - uy * size - ux * size * 0.5,
                    )
                    painter.drawLine(end_pt, left)
                    painter.drawLine(end_pt, right)

        def draw_timed_marker(
            center_pt: QPointF,
            radius_outer: int = 5,
            radius_inner: int = 2,
            inner_color: QColor = QColor("#ffffff"),
            outer_color: QColor = QColor("#000000"),
        ):
            painter.setPen(QPen(outer_color))
            painter.setBrush(outer_color)
            painter.drawEllipse(center_pt, radius_outer, radius_outer)
            painter.setPen(QPen(inner_color))
            painter.setBrush(inner_color)
            painter.drawEllipse(center_pt, radius_inner, radius_inner)

        def draw_pin(at_pt: QPointF, color: QColor, alpha: int):
            pen = QPen(QColor(color.red(), color.green(), color.blue(), alpha))
            pen.setWidth(2)
            painter.setPen(pen)
            radius = 4
            painter.drawEllipse(QPointF(at_pt.x(), at_pt.y()), radius, radius)
            painter.drawLine(
                QPointF(at_pt.x(), at_pt.y() + radius),
                QPointF(at_pt.x(), at_pt.y() + radius + 10),
            )

        def draw_text_block(
            anchor: QPointF,
            lines: list[str],
            text_color: QColor = QColor("#ffffff"),
            line_colors: list[QColor] | None = None,
        ):
            pad = 2
            if not lines:
                return
            widths = [metrics.boundingRect(line).width() for line in lines]
            heights = [metrics.boundingRect(line).height() for line in lines]
            max_w = max(widths)
            line_h = max(heights)
            block_h = line_h * len(lines)
            bg_rect = QRect(
                int(anchor.x()),
                int(anchor.y()) - block_h,
                max_w + pad * 2,
                block_h + pad * 2,
            )
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(0, 0, 0, 200))
            painter.drawRect(bg_rect)
            for i, line in enumerate(lines):
                color = line_colors[i] if line_colors and i < len(line_colors) else text_color
                painter.setPen(QPen(color))
                painter.drawText(
                    QPointF(bg_rect.x() + pad, bg_rect.y() + pad + line_h * (i + 1) - 2),
                    line,
                )

        if self._status_lines:
            hud_font = QFont("Consolas")
            hud_font.setPointSize(11)
            painter.setFont(hud_font)
            hud_metrics = painter.fontMetrics()
            widths = [hud_metrics.horizontalAdvance(line) for line in self._status_lines]
            line_h = max(1, hud_metrics.height())
            pad_x = 8
            pad_y = 6
            spacing = 2
            hud_w = max(widths) + pad_x * 2 if widths else 0
            hud_h = (line_h * len(self._status_lines)) + (spacing * (len(self._status_lines) - 1)) + pad_y * 2
            hud_rect = QRect(rect.left() + 10, rect.top() + 10, hud_w, hud_h)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(0, 0, 0, 150))
            painter.drawRoundedRect(hud_rect, 6, 6)
            painter.setPen(QPen(QColor("#00ff66")))
            y = hud_rect.y() + pad_y + hud_metrics.ascent()
            for line in self._status_lines:
                painter.drawText(QPointF(hud_rect.x() + pad_x, y), line)
                y += line_h + spacing
            painter.setFont(font)

        for ann in list(self._annotations):
            color = QColor(ann.get("color") or "#ffcc00")
            frame_idx = ann.get("frame_index")
            if frame_idx is not None and frame_idx != self._current_frame_index:
                alpha = 80
            else:
                alpha = 255
            if ann.get("type") == "text":
                pos = ann.get("pos") or [0, 0]
                img_pt = QPointF(pos[0], pos[1])
                widget_pt = self._map_from_image(img_pt)
                painter.setPen(QPen(QColor(color.red(), color.green(), color.blue(), alpha)))
                painter.drawText(widget_pt + QPointF(4, -4), ann.get("text", ""))
                if ann.get("pinned"):
                    draw_pin(widget_pt + QPointF(6, 10), color, alpha)
            elif ann.get("type") == "tray":
                pts = ann.get("points") or []
                if len(pts) >= 2:
                    pen = QPen(QColor(color.red(), color.green(), color.blue(), alpha))
                    pen.setWidth(2)
                    painter.setPen(pen)
                    painter.setBrush(Qt.NoBrush)
                    widget_pts = [self._map_from_image(QPointF(p[0], p[1])) for p in pts]
                    for i in range(len(widget_pts)):
                        a = widget_pts[i]
                        b = widget_pts[(i + 1) % len(widget_pts)]
                        painter.drawLine(a, b)
                    painter.setBrush(QColor(color.red(), color.green(), color.blue(), alpha))
                    for pt in widget_pts:
                        painter.drawEllipse(pt, 4, 4)
            else:
                start = ann.get("start") or [0, 0]
                end = ann.get("end") or [0, 0]
                start_pt = self._map_from_image(QPointF(start[0], start[1]))
                end_pt = self._map_from_image(QPointF(end[0], end[1]))
                arrow = ann.get("type") == "arrow"
                draw_line(start_pt, end_pt, color, arrow, alpha)
                if ann.get("type") == "timed_line":
                    start_frame = int(ann.get("start_frame", 0))
                    end_frame = int(ann.get("end_frame", 0))
                    denom = (end_frame - start_frame) or 1
                    raw_ratio = (self._current_frame_index - start_frame) / denom
                    # Allow the marker to extrapolate a bit past the line end/start.
                    draw_ratio = max(-1.0, min(2.0, raw_ratio))
                    tick_x = start_pt.x() + (end_pt.x() - start_pt.x()) * draw_ratio
                    tick_y = start_pt.y() + (end_pt.y() - start_pt.y()) * draw_ratio
                    extrapolated = raw_ratio < 0.0 or raw_ratio > 1.0
                    inner_color = QColor("#ff3b30") if extrapolated else QColor("#ffffff")
                    draw_timed_marker(QPointF(tick_x, tick_y), inner_color=inner_color)
                    if self._fps > 0:
                        duration_sec = abs(end_frame - start_frame) / self._fps
                        elapsed_frames = max(0, self._current_frame_index - start_frame)
                        elapsed_sec = elapsed_frames / self._fps
                        lines = [f"Total: {duration_sec:.3f}s", f"Elapsed: {elapsed_sec:.3f}s"]
                        if ann.get("distance_m") is not None:
                            try:
                                dist_m = float(ann.get("distance_m"))
                                ratio = max(0.0, elapsed_sec / duration_sec if duration_sec > 0 else 0.0)
                                dist_now = dist_m * ratio
                                lines.append(f"Distance: {dist_now:.3f} m")
                                if duration_sec > 0:
                                    lines.append(f"Speed: {dist_m / duration_sec:.3f} m/s")
                            except (TypeError, ValueError):
                                pass
                        tick_pt = QPointF(tick_x, tick_y)
                        text_color = QColor("#ff3b30") if extrapolated else QColor("#ffffff")
                        line_colors = None
                        if extrapolated:
                            line_colors = [QColor("#ffffff")] * len(lines)
                            if ann.get("distance_m") is not None and len(lines) >= 3:
                                line_colors[2] = QColor("#ff3b30")
                        draw_text_block(
                            tick_pt + QPointF(8, 14),
                            lines,
                            text_color=text_color,
                            line_colors=line_colors,
                        )
                if ann.get("type") == "measure":
                    dx = (end[0] - start[0])
                    dy = (end[1] - start[1])
                    dist = math.hypot(dx, dy)
                    mid = QPointF((start_pt.x() + end_pt.x()) / 2, (start_pt.y() + end_pt.y()) / 2)
                    draw_text_block(mid + QPointF(6, -6), [f"{dist:.1f}px"])
                if ann.get("pinned"):
                    draw_pin(end_pt + QPointF(6, 10), color, alpha)

        if self._edit_idx is not None and self._editable:
            if self._edit_idx >= len(self._annotations):
                self._edit_idx = None
            else:
                ann = self._annotations[self._edit_idx]
                if ann.get("type") == "tray":
                    pts = ann.get("points") or []
                    if len(pts) == 4:
                        pen = QPen(QColor("#ffffff"))
                        pen.setWidth(1)
                        painter.setPen(pen)
                        painter.setBrush(QColor("#1e90ff"))
                        size = 6
                        for p in pts:
                            pt = self._map_from_image(QPointF(p[0], p[1]))
                            painter.drawRect(QRect(int(pt.x() - size / 2), int(pt.y() - size / 2), size, size))
                else:
                    handle_pts = self._handle_points_for_annotation(ann)
                    if handle_pts is not None:
                        start_pt, end_pt = handle_pts
                        pen = QPen(QColor("#ffffff"))
                        pen.setWidth(1)
                        painter.setPen(pen)
                        painter.setBrush(QColor("#1e90ff"))
                        size = 6
                        painter.drawRect(QRect(int(start_pt.x() - size / 2), int(start_pt.y() - size / 2), size, size))
                        painter.drawRect(QRect(int(end_pt.x() - size / 2), int(end_pt.y() - size / 2), size, size))

        if self._pending_start is not None and self._pending_end is not None:
            start_pt = self._map_from_image(self._pending_start)
            end_pt = self._map_from_image(self._pending_end)
            draw_line(start_pt, end_pt, self._color, self._tool == "arrow", 255)
            if self._tool == "measure":
                dx = self._pending_end.x() - self._pending_start.x()
                dy = self._pending_end.y() - self._pending_start.y()
                dist = math.hypot(dx, dy)
                mid = QPointF((start_pt.x() + end_pt.x()) / 2, (start_pt.y() + end_pt.y()) / 2)
                draw_text_block(mid + QPointF(6, -6), [f"{dist:.1f}px"])
        if self._tool == "tray" and self._tray_points:
            pen = QPen(self._color)
            pen.setWidth(2)
            painter.setPen(pen)
            painter.setBrush(QColor(self._color.red(), self._color.green(), self._color.blue(), 120))
            widget_pts = [self._map_from_image(p) for p in self._tray_points]
            for i in range(len(widget_pts) - 1):
                painter.drawLine(widget_pts[i], widget_pts[i + 1])
            for pt in widget_pts:
                painter.drawEllipse(pt, 4, 4)
        if self._timed_start is not None and self._tool == "timed_line":
            pos = self._timed_start.get("pos", [0, 0])
            start_pt = self._map_from_image(QPointF(pos[0], pos[1]))
            draw_timed_marker(start_pt)

        # Tray bird's-eye view overlay
        tray_ann = None
        for ann in reversed(self._annotations):
            if ann.get("type") == "tray" and len(ann.get("points") or []) == 4:
                tray_ann = ann
                break
        if tray_ann is not None:
            pts = [QPointF(p[0], p[1]) for p in tray_ann.get("points", [])]
            birds_eye = self._build_birds_eye(pts)
            if birds_eye is not None:
                self._last_birds_eye = birds_eye

        # Conveyor target overlays
        if self._target_overlays and self._frame is not None:
            fw = self._frame.width()
            fh = self._frame.height()
            painter.setRenderHint(QPainter.Antialiasing)

            def _img_to_widget(nx, ny):
                return self._map_from_image(QPointF(nx * fw, ny * fh))

            for ov in self._target_overlays:
                opacity = float(ov.get("opacity", 1.0))
                painter.setOpacity(opacity)

                hex_color = ov.get("color", "#3498db")
                color = QColor(hex_color)
                label = str(ov.get("label", ""))
                info_lines = [str(line) for line in (ov.get("info_lines") or []) if str(line).strip()]
                alert = bool(ov.get("alert", False))
                text_bg_color = QColor(str(ov.get("text_bg_color", "#000000")))
                if not text_bg_color.isValid():
                    text_bg_color = QColor("#000000")

                # Rectangle outline
                corners = ov.get("rect_corners")
                if corners and len(corners) == 4:
                    poly = QPolygonF([_img_to_widget(nx, ny) for nx, ny in corners])
                    painter.setPen(QPen(color, 1.5))
                    fill = QColor(color.red(), color.green(), color.blue(),
                                  max(0, min(255, int(40 * opacity))))
                    painter.setBrush(QBrush(fill))
                    painter.drawPolygon(poly)

                # Centre dot
                nx = float(ov.get("norm_x", 0))
                ny = float(ov.get("norm_y", 0))
                wp = _img_to_widget(nx, ny)
                painter.setPen(QPen(color, 1))
                painter.setBrush(QBrush(color))
                painter.drawEllipse(wp, 3, 3)
                if alert:
                    painter.setPen(QPen(QColor("#f1c40f"), 2))
                    painter.setBrush(Qt.NoBrush)
                    painter.drawEllipse(wp, 6, 6)

                # Label (most recent only — caller sets label="" for ghosts)
                if info_lines or label:
                    lines = info_lines or [label]
                    f = painter.font()
                    f.setPointSize(8)
                    painter.setFont(f)
                    metrics = painter.fontMetrics()
                    text_x = int(wp.x()) + 10
                    text_y = int(wp.y()) + 4
                    text_w = max(metrics.horizontalAdvance(line) for line in lines)
                    line_h = metrics.height()
                    text_h = line_h * len(lines)
                    bg_rect = QRect(
                        text_x - 4,
                        text_y - metrics.ascent() - 2,
                        text_w + 8,
                        text_h + 4,
                    )
                    painter.setPen(Qt.NoPen)
                    text_bg_color.setAlpha(220)
                    painter.setBrush(QBrush(text_bg_color))
                    painter.drawRoundedRect(bg_rect, 4, 4)
                    painter.setPen(QPen(Qt.white))
                    for line_idx, line in enumerate(lines):
                        if line_idx == 0:
                            bold_font = painter.font()
                            bold_font.setBold(True)
                            painter.setFont(bold_font)
                            metrics = painter.fontMetrics()
                        elif line_idx == 1:
                            normal_font = painter.font()
                            normal_font.setBold(False)
                            painter.setFont(normal_font)
                            metrics = painter.fontMetrics()
                            line_h = metrics.height()
                        painter.drawText(text_x, text_y + line_idx * line_h, line)

            painter.setOpacity(1.0)

        painter.end()

