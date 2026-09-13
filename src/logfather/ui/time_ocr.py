from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from time import perf_counter
import json
import math
import re
import os
from pathlib import Path
from typing import Iterable, Optional
import sys

from logfather.paths import bundle_root

import cv2
import numpy as np
import shutil
import subprocess

from PySide6.QtCore import Qt, QTimer, QRect, QPoint, Signal
from PySide6.QtGui import QColor, QImage, QPixmap, QPainter, QPen, QBrush, QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QSlider,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

try:
    import pytesseract
    from pytesseract import pytesseract as pytesseract_module
except Exception:  # pragma: no cover - optional dependency
    pytesseract = None
    pytesseract_module = None

OCR_SYNC_FAST_SECONDS = 1
OCR_SYNC_FALLBACK_SECONDS = 3
OCR_SYNC_COARSE_STEP_SECONDS = 0.2
# The readings table (Chris, 2026-09-12): every second change in the first
# OCR_TABLE_SECONDS after the sync frame, then one drift check every
# OCR_TABLE_INTERVAL_SECONDS through the rest of the clip; a drift check
# finds the next two second changes within OCR_TABLE_WINDOW_SECONDS of its
# checkpoint, giving the frame the second began on and how many frames it
# lasted.
OCR_TABLE_SECONDS = 10
OCR_TABLE_INTERVAL_SECONDS = 60
OCR_TABLE_WINDOW_SECONDS = 2.5


@dataclass(frozen=True)
class Roi:
    x: int
    y: int
    w: int
    h: int

    @classmethod
    def top_center(
        cls,
        frame_w: int,
        frame_h: int,
        *,
        width_ratio: float = 0.5,
        height_ratio: float = 0.085,
        y_offset_ratio: float = 0.013,
    ) -> "Roi":
        width_ratio = max(0.01, min(1.0, float(width_ratio)))
        height_ratio = max(0.01, min(1.0, float(height_ratio)))
        y_offset_ratio = max(0.0, min(0.9, float(y_offset_ratio)))

        w = max(1, int(round(frame_w * width_ratio)))
        h = max(1, int(round(frame_h * height_ratio)))
        x = max(0, int(round((frame_w - w) / 2)))
        y = max(0, int(round(frame_h * y_offset_ratio)))
        return cls(x=x, y=y, w=w, h=h)

    def crop(self, frame_bgr: np.ndarray) -> np.ndarray:
        h, w = frame_bgr.shape[:2]
        x1 = max(0, min(w - 1, self.x))
        y1 = max(0, min(h - 1, self.y))
        x2 = max(x1 + 1, min(w, x1 + self.w))
        y2 = max(y1 + 1, min(h, y1 + self.h))
        return frame_bgr[y1:y2, x1:x2]

    @classmethod
    def top_center_time(
        cls,
        frame_w: int,
        frame_h: int,
        *,
        width_ratio: float = 0.22,
        height_ratio: float = 0.06,
        y_offset_ratio: float = 0.013,
        x_offset_ratio: float = 0.0,
    ) -> "Roi":
        roi = cls.top_center(
            frame_w,
            frame_h,
            width_ratio=width_ratio,
            height_ratio=height_ratio,
            y_offset_ratio=y_offset_ratio,
        )
        if abs(x_offset_ratio) < 1e-6:
            return roi
        shift = int(round(frame_w * x_offset_ratio))
        return cls(x=roi.x + shift, y=roi.y, w=roi.w, h=roi.h)


class ScrubbableLabel(QLabel):
    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self._scrub_callback = None

    def set_scrub_callback(self, cb):
        self._scrub_callback = cb

    def wheelEvent(self, event):
        if self._scrub_callback is None:
            super().wheelEvent(event)
            return
        delta = event.angleDelta().y()
        if delta > 0:
            self._scrub_callback(-1)
        elif delta < 0:
            self._scrub_callback(1)
        event.accept()


def roi_to_ratios(roi: "Roi", frame_w: int, frame_h: int) -> "RoiSettings":
    """The inverse of Roi.top_center_time: the ratios that reproduce a box
    given in frame pixels (Chris, 2026-09-12: the box is dragged on the
    picture now, the ratios are what gets saved)."""
    frame_w = max(1, int(frame_w))
    frame_h = max(1, int(frame_h))
    w = max(1, int(roi.w))
    h = max(1, int(roi.h))
    width_ratio = max(0.01, min(1.0, w / frame_w))
    height_ratio = max(0.01, min(1.0, h / frame_h))
    y_offset_ratio = max(0.0, min(0.9, roi.y / frame_h))
    centred_x = (frame_w - w) / 2
    x_offset_ratio = (roi.x - centred_x) / frame_w
    return RoiSettings(width_ratio, height_ratio, y_offset_ratio, x_offset_ratio)


class RoiEditorLabel(ScrubbableLabel):
    """The OCR window's picture: shows a region of the frame (zoomed to the
    clock by default) with the OCR box drawn over it, and lets the box be
    dragged by its edges, corners or middle (Chris, 2026-09-12: sliders
    were far harder). Emits roi_changed with the box in frame pixels."""

    roi_changed = Signal(str, object)  # box name ("time" or "date"), Roi in frame pixels
    HANDLE = 8
    COLOURS = {"time": ("#00ff5a", "#003a14"), "date": ("#c77dff", "#2a0a45")}

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self._frame: QPixmap | None = None
        self._view = QRect()
        self._boxes: dict[str, Roi] = {}
        self._drag: tuple[str, str, QPoint, Roi] | None = None  # box, handle, press, base
        self.setMouseTracking(True)

    @property
    def _roi(self) -> Roi | None:
        return self._boxes.get("time")

    def set_picture(self, frame: QPixmap | None, view: QRect, roi: Roi | None, date_roi: Roi | None = None) -> None:
        self._frame = frame
        self._view = QRect(view)
        self._boxes = {}
        if roi is not None:
            self._boxes["time"] = roi
        if date_roi is not None:
            self._boxes["date"] = date_roi
        if frame is None:
            self.setText("Open a video to start")
        else:
            self.setText("")
        self._fit_height_to_view()
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_height_to_view()

    def _fit_height_to_view(self) -> None:
        """Keep the label exactly as tall as its view at this width, so no
        black band sits between the picture and the slider under it
        (Chris, 2026-09-12)."""
        if self._frame is None or self._view.width() <= 0 or self._view.height() <= 0:
            return
        wanted = int(round(self.width() * self._view.height() / self._view.width()))
        cap = 540
        try:
            screen = self.screen()
            if screen is not None:
                cap = max(240, int(screen.availableGeometry().height() * 0.45))
        except Exception:
            pass
        wanted = max(1, min(wanted, cap))
        if abs(self.height() - wanted) > 1 or self.minimumHeight() != wanted or self.maximumHeight() != wanted:
            self.setFixedHeight(wanted)

    # ---- geometry
    def _placement(self) -> tuple[float, float, float] | None:
        """(scale, x0, y0): the displayed view rect in label pixels."""
        if self._frame is None or self._view.width() <= 0 or self._view.height() <= 0:
            return None
        scale = min(self.width() / self._view.width(), self.height() / self._view.height())
        if scale <= 0:
            return None
        x0 = (self.width() - self._view.width() * scale) / 2
        y0 = (self.height() - self._view.height() * scale) / 2
        return scale, x0, y0

    def _to_label(self, fx: float, fy: float) -> tuple[float, float]:
        scale, x0, y0 = self._placement()
        return x0 + (fx - self._view.x()) * scale, y0 + (fy - self._view.y()) * scale

    def _to_frame(self, lx: float, ly: float) -> tuple[float, float]:
        scale, x0, y0 = self._placement()
        return self._view.x() + (lx - x0) / scale, self._view.y() + (ly - y0) / scale

    def _box_rect(self, name: str):
        roi = self._boxes.get(name)
        if roi is None or self._placement() is None:
            return None
        x1, y1 = self._to_label(roi.x, roi.y)
        x2, y2 = self._to_label(roi.x + roi.w, roi.y + roi.h)
        return QRect(int(round(x1)), int(round(y1)), max(1, int(round(x2 - x1))), max(1, int(round(y2 - y1))))

    def _roi_label_rect(self):
        return self._box_rect("time")

    def _hit(self, pos: QPoint) -> str | None:
        found = self._hit_box(pos)
        return found[1] if found else None

    def _hit_box(self, pos: QPoint) -> tuple[str, str] | None:
        """(box name, handle) under the pointer; the time box wins a tie."""
        for name in ("time", "date"):
            handle = self._hit_rect(self._box_rect(name), pos)
            if handle:
                return name, handle
        return None

    def _hit_rect(self, rect, pos: QPoint) -> str | None:
        if rect is None:
            return None
        m = self.HANDLE
        near_l = abs(pos.x() - rect.left()) <= m
        near_r = abs(pos.x() - rect.right()) <= m
        near_t = abs(pos.y() - rect.top()) <= m
        near_b = abs(pos.y() - rect.bottom()) <= m
        inside_x = rect.left() - m <= pos.x() <= rect.right() + m
        inside_y = rect.top() - m <= pos.y() <= rect.bottom() + m
        if near_l and near_t and inside_x and inside_y:
            return "tl"
        if near_r and near_t and inside_x and inside_y:
            return "tr"
        if near_l and near_b and inside_x and inside_y:
            return "bl"
        if near_r and near_b and inside_x and inside_y:
            return "br"
        if near_l and inside_y:
            return "l"
        if near_r and inside_y:
            return "r"
        if near_t and inside_x:
            return "t"
        if near_b and inside_x:
            return "b"
        if rect.contains(pos):
            return "move"
        return None

    _CURSORS = {"tl": Qt.SizeFDiagCursor, "br": Qt.SizeFDiagCursor, "tr": Qt.SizeBDiagCursor, "bl": Qt.SizeBDiagCursor,
                "l": Qt.SizeHorCursor, "r": Qt.SizeHorCursor, "t": Qt.SizeVerCursor, "b": Qt.SizeVerCursor, "move": Qt.SizeAllCursor}

    # ---- painting
    def paintEvent(self, event):
        if self._frame is None or self._placement() is None:
            super().paintEvent(event)
            return
        scale, x0, y0 = self._placement()
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#000000"))
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        target = QRect(int(round(x0)), int(round(y0)), int(round(self._view.width() * scale)), int(round(self._view.height() * scale)))
        painter.drawPixmap(target, self._frame, self._view)
        for name in ("date", "time"):
            rect = self._box_rect(name)
            if rect is None:
                continue
            line, dark = self.COLOURS[name]
            pen = QPen(QColor(line))
            pen.setWidth(2)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(rect)
            painter.setBrush(QBrush(QColor(line)))
            painter.setPen(QPen(QColor(dark), 1))
            h = self.HANDLE
            for cx, cy in ((rect.left(), rect.top()), (rect.right(), rect.top()), (rect.left(), rect.bottom()), (rect.right(), rect.bottom()),
                           (rect.center().x(), rect.top()), (rect.center().x(), rect.bottom()), (rect.left(), rect.center().y()), (rect.right(), rect.center().y())):
                painter.drawRect(QRect(int(cx - h / 2), int(cy - h / 2), h, h))
            # "Date" / "Time" above the box (Chris, 2026-09-12)
            caption = "Date" if name == "date" else "Time"
            font = painter.font()
            font.setBold(True)
            font.setPointSize(10)
            painter.setFont(font)
            painter.setPen(QPen(QColor(dark), 3))
            painter.drawText(rect.left() + 2, rect.top() - 8, caption)
            painter.setPen(QPen(QColor(line), 1))
            painter.drawText(rect.left() + 2, rect.top() - 8, caption)
        painter.end()

    # ---- mouse
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self._boxes:
            found = self._hit_box(event.position().toPoint())
            if found:
                name, handle = found
                base = self._boxes[name]
                self._drag = (name, handle, event.position().toPoint(), Roi(base.x, base.y, base.w, base.h))
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        pos = event.position().toPoint()
        if self._drag is None:
            hit = self._hit(pos) if self._boxes else None
            self.setCursor(self._CURSORS.get(hit, Qt.ArrowCursor))
            super().mouseMoveEvent(event)
            return
        name, kind, start, base = self._drag
        placement = self._placement()
        if placement is None:
            return
        scale = placement[0]
        dx = (pos.x() - start.x()) / scale
        dy = (pos.y() - start.y()) / scale
        fw, fh = self._frame.width(), self._frame.height()
        x, y, w, h = base.x, base.y, base.w, base.h
        if kind == "move":
            x, y = x + dx, y + dy
        else:
            if "l" in kind:
                x, w = x + dx, w - dx
            if "r" in kind:
                w = w + dx
            if "t" in kind:
                y, h = y + dy, h - dy
            if "b" in kind:
                h = h + dy
        w = max(8.0, w)
        h = max(4.0, h)
        x = max(0.0, min(fw - w, x))
        y = max(0.0, min(fh - h, y))
        self._boxes[name] = Roi(int(round(x)), int(round(y)), int(round(w)), int(round(h)))
        self.update()
        self.roi_changed.emit(name, self._boxes[name])
        event.accept()

    def mouseReleaseEvent(self, event):
        if self._drag is not None and event.button() == Qt.LeftButton:
            name = self._drag[0]
            self._drag = None
            if name in self._boxes:
                self.roi_changed.emit(name, self._boxes[name])
            event.accept()
            return
        super().mouseReleaseEvent(event)


@dataclass(frozen=True)
class OcrConfig:
    scale: float = 4.0
    blur_ksize: int = 1
    threshold: str = "otsu"  # "otsu", "adaptive", "none"
    invert: bool = True
    auto_invert: bool = False
    psm: int = 7
    whitelist: str = "0123456789:"
    lang: str = "eng"


@dataclass(frozen=True)
class RoiSettings:
    width_ratio: float
    height_ratio: float
    y_offset_ratio: float
    x_offset_ratio: float


ADDITIONAL_CAMERA_KEY_SUFFIX = "/additional"


def additional_camera_roi_key(pikpak_id: str) -> str:
    """The additional camera keeps its own Date and Time boxes under
    "<PikPakNNN>/additional" (Chris, 2026-09-12); before this the two
    cameras of a system shared one entry and overwrote each other."""
    return f"{pikpak_id}{ADDITIONAL_CAMERA_KEY_SUFFIX}"


def load_roi_settings(settings_path: Path, key: str | None, section: str = "roi_by_key") -> RoiSettings | None:
    """`section` is "roi_by_key" for the time box, "date_roi_by_key" for the
    date box (Chris, 2026-09-12). An additional-camera key with no entry
    of its own falls back to the main camera's boxes as a starting point;
    saves always go to the key given."""
    if not key:
        return None
    if not settings_path.exists():
        return None
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    section_data = data.get(section, {})
    if not isinstance(section_data, dict):
        return None
    entry = section_data.get(key)
    if not isinstance(entry, dict) and key.endswith(ADDITIONAL_CAMERA_KEY_SUFFIX):
        entry = section_data.get(key[: -len(ADDITIONAL_CAMERA_KEY_SUFFIX)])
    if not isinstance(entry, dict):
        return None
    try:
        return RoiSettings(
            width_ratio=float(entry.get("width_ratio", 0.22)),
            height_ratio=float(entry.get("height_ratio", 0.06)),
            y_offset_ratio=float(entry.get("y_offset_ratio", 0.013)),
            x_offset_ratio=float(entry.get("x_offset_ratio", 0.0)),
        )
    except Exception:
        return None


def save_roi_settings(settings_path: Path, key: str | None, settings: RoiSettings, section: str = "roi_by_key") -> None:
    if not key:
        return
    try:
        data = {}
        if settings_path.exists():
            data = json.loads(settings_path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    roi_by_key = data.get(section)
    if not isinstance(roi_by_key, dict):
        roi_by_key = {}
        data[section] = roi_by_key
    roi_by_key[key] = {
        "width_ratio": settings.width_ratio,
        "height_ratio": settings.height_ratio,
        "y_offset_ratio": settings.y_offset_ratio,
        "x_offset_ratio": settings.x_offset_ratio,
    }
    try:
        settings_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def _preprocess_for_ocr(roi_bgr: np.ndarray, cfg: OcrConfig) -> np.ndarray:
    gray = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2GRAY)
    if cfg.scale and abs(cfg.scale - 1.0) > 1e-6:
        gray = cv2.resize(gray, None, fx=cfg.scale, fy=cfg.scale, interpolation=cv2.INTER_CUBIC)

    if cfg.blur_ksize and cfg.blur_ksize >= 3:
        k = cfg.blur_ksize if cfg.blur_ksize % 2 == 1 else cfg.blur_ksize + 1
        gray = cv2.GaussianBlur(gray, (k, k), 0)

    if cfg.threshold == "adaptive":
        bw = cv2.adaptiveThreshold(
            gray,
            255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            31,
            10,
        )
    elif cfg.threshold == "otsu":
        _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    else:
        bw = gray

    if cfg.auto_invert:
        if np.mean(bw) < 127:
            bw = cv2.bitwise_not(bw)

    if cfg.invert:
        bw = cv2.bitwise_not(bw)

    kernel = np.ones((2, 2), np.uint8)
    bw = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, kernel)

    return bw


def _ensure_tesseract() -> None:
    if pytesseract is None:
        raise RuntimeError(
            "pytesseract is not available. Install pytesseract and the Tesseract OCR "
            "engine locally to enable time OCR."
        )
    module = pytesseract_module or pytesseract

    def _configure_tessdata_for(cmd_path: Path) -> None:
        tessdata_dir = cmd_path.parent / "tessdata"
        if tessdata_dir.exists():
            os.environ["TESSDATA_PREFIX"] = str(tessdata_dir)
        else:
            # Avoid leaving a stale override from another Tesseract install.
            os.environ.pop("TESSDATA_PREFIX", None)

    bundled_root = bundle_root()
    bundled_exe = bundled_root / "tesseract" / "tesseract.exe"
    if bundled_exe.exists():
        module.tesseract_cmd = str(bundled_exe)
        # Always override stale machine/user Tesseract installs when the
        # bundled runtime is present, otherwise OCR can resolve traineddata
        # from an unrelated system path.
        _configure_tessdata_for(bundled_exe)
        return
    default_paths = [
        Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
        Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"),
    ]
    configured = getattr(module, "tesseract_cmd", "") or ""
    configured_path = Path(configured) if configured else None
    if configured_path and configured_path.exists():
        _configure_tessdata_for(configured_path)
        return
    for candidate in default_paths:
        if candidate.exists():
            module.tesseract_cmd = str(candidate)
            _configure_tessdata_for(candidate)
            return
    found = shutil.which("tesseract")
    if found:
        module.tesseract_cmd = found
        _configure_tessdata_for(Path(found))
        return

    ok, message = _probe_tesseract()
    if not ok:
        raise RuntimeError(message)


def _probe_tesseract() -> tuple[bool, str]:
    if pytesseract is None:
        return False, "pytesseract is not available"
    module = pytesseract_module or pytesseract
    cmd = getattr(module, "tesseract_cmd", "") or "tesseract"
    try:
        proc = subprocess.run([cmd, "--version"], capture_output=True, text=True)
    except FileNotFoundError:
        return False, f"Tesseract not found: {cmd}"
    except Exception as exc:
        return False, f"Failed to run {cmd}: {exc}"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        if detail:
            return False, detail
        return False, f"Tesseract exited with code {proc.returncode}"
    first_line = (proc.stdout or "").splitlines()[:1]
    return True, first_line[0] if first_line else "OK"


def ocr_time_from_frame(
    frame_bgr: np.ndarray,
    *,
    roi: Optional[Roi] = None,
    ocr_cfg: Optional[OcrConfig] = None,
) -> str:
    _ensure_tesseract()
    if ocr_cfg is None:
        ocr_cfg = OcrConfig()

    h, w = frame_bgr.shape[:2]
    if roi is None:
        roi = Roi.top_center_time(w, h)

    roi_bgr = roi.crop(frame_bgr)
    prepared = _preprocess_for_ocr(roi_bgr, ocr_cfg)

    config = f"--oem 3 --psm {ocr_cfg.psm} -c tessedit_char_whitelist={ocr_cfg.whitelist}"
    text = pytesseract.image_to_string(prepared, config=config, lang=ocr_cfg.lang)
    return text.strip()


_DATE_RE = re.compile(r"(\d{2})/(\d{2})/(\d{4})")


def parse_cctv_date(text: str):
    """The date burnt into the picture, as a date. The cameras always write
    DD/MM/YYYY (Chris, 2026-09-12). Tesseract tends to read the slashes in
    the camera's font as a 7 (or drop them), so after the exact form the
    ten characters are read by position, whatever sits where the slashes
    should be, and eight bare digits are read as DDMMYYYY. None when no
    real date comes out."""
    from datetime import date as _date

    cleaned = "".join(ch for ch in str(text or "") if ch.isdigit() or ch == "/")

    def build(day: str, month: str, year: str):
        try:
            return _date(int(year), int(month), int(day))
        except ValueError:
            return None

    match = _DATE_RE.search(cleaned)
    if match:
        found = build(*match.groups())
        if found is not None:
            return found
    if len(cleaned) == 10 and cleaned[0:2].isdigit() and cleaned[3:5].isdigit() and cleaned[6:10].isdigit():
        found = build(cleaned[0:2], cleaned[3:5], cleaned[6:10])
        if found is not None:
            return found
    digits = "".join(ch for ch in cleaned if ch.isdigit())
    if len(digits) == 8:
        return build(digits[0:2], digits[2:4], digits[4:8])
    return None


def _epoch_date():
    from datetime import date as _date
    return _date(1970, 1, 1)


def ocr_date_from_frame(frame_bgr: np.ndarray, *, roi: Roi) -> str:
    """One OCR pass over the date box: digits and separators only."""
    _ensure_tesseract()
    cfg = OcrConfig(whitelist="0123456789/")
    prepared = _preprocess_for_ocr(roi.crop(frame_bgr), cfg)
    config = f"--oem 3 --psm {cfg.psm} -c tessedit_char_whitelist={cfg.whitelist}"
    return pytesseract.image_to_string(prepared, config=config, lang=cfg.lang).strip()


def _read_frame(
    video_path: Path | str,
    *,
    frame_index: Optional[int] = None,
    time_seconds: Optional[float] = None,
) -> tuple[np.ndarray, int, float]:
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Unable to open video: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    if frame_index is None:
        if time_seconds is None:
            frame_index = 0
        else:
            frame_index = int(round(time_seconds * fps)) if fps > 0 else int(time_seconds * 25)

    cap.set(cv2.CAP_PROP_POS_FRAMES, int(frame_index))
    ret, frame = cap.read()
    cap.release()
    if not ret or frame is None:
        raise RuntimeError(f"Unable to read frame {frame_index} from video: {video_path}")
    return frame, int(frame_index), float(fps)


def ocr_time_from_video(
    video_path: Path | str,
    *,
    frame_index: Optional[int] = None,
    time_seconds: Optional[float] = None,
    roi: Optional[Roi] = None,
    ocr_cfg: Optional[OcrConfig] = None,
) -> str:
    frame, _, _ = _read_frame(video_path, frame_index=frame_index, time_seconds=time_seconds)
    return ocr_time_from_frame(frame, roi=roi, ocr_cfg=ocr_cfg)


def ocr_time_from_video_samples(
    video_path: Path | str,
    *,
    frame_indices: Optional[Iterable[int]] = None,
    time_seconds_list: Optional[Iterable[float]] = None,
    roi: Optional[Roi] = None,
    ocr_cfg: Optional[OcrConfig] = None,
) -> list[str]:
    if frame_indices is None and time_seconds_list is None:
        frame_indices = [0]

    results: list[str] = []
    if frame_indices is not None:
        for idx in frame_indices:
            results.append(ocr_time_from_video(video_path, frame_index=idx, roi=roi, ocr_cfg=ocr_cfg))
    if time_seconds_list is not None:
        for t in time_seconds_list:
            results.append(ocr_time_from_video(video_path, time_seconds=t, roi=roi, ocr_cfg=ocr_cfg))
    return results


_TIME_RE = re.compile(r"^(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})$")


def _normalize_ocr_text(text: str) -> str:
    cleaned = " ".join(text.split())
    return cleaned.strip()


def _combine_date_and_time(base_dt: datetime, time_text: str) -> datetime:
    hour, minute, second = (int(part) for part in time_text.split(":"))
    candidate = base_dt.replace(hour=hour, minute=minute, second=second, microsecond=0)
    if candidate < base_dt and (base_dt - candidate) > timedelta(hours=12):
        candidate += timedelta(days=1)
    return candidate


def _is_valid_time_text(text: str) -> bool:
    match = _TIME_RE.match(text)
    if not match:
        return False
    try:
        hour = int(match.group("hour"))
        minute = int(match.group("minute"))
        second = int(match.group("second"))
    except ValueError:
        return False
    return 0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59


class OcrVideoPlayer(QWidget):
    def __init__(
        self,
        *,
        settings_path: Path | None = None,
        settings_key: str | None = None,
        auto_analyze: bool = True,
        on_offset_approved=None,
    ):
        super().__init__()
        self.setWindowTitle("Sync CCTV Time")  # Chris, 2026-09-12
        self._roi_settings_path = settings_path
        self._roi_settings_key = settings_key
        self._auto_analyze = bool(auto_analyze)
        self._on_offset_approved = on_offset_approved

        self.cap: cv2.VideoCapture | None = None
        self.fps = 25.0
        self.frame_count = 0
        self.current_frame = 0
        self.playing = False
        self.current_video_path: str | None = None
        self.filename_dt: datetime | None = None
        self.estimated_start_dt: datetime | None = None
        self.time_frame_offset = 1

        self.video_label = RoiEditorLabel("Open a video to start")
        self.video_label.setAlignment(Qt.AlignCenter)
        self.video_label.setMinimumWidth(640)
        self.video_label.setMinimumHeight(120)
        self.video_label.set_scrub_callback(self._scrub_by_frames)
        self.video_label.roi_changed.connect(self._on_box_dragged)
        self._last_frame: np.ndarray | None = None
        # The zoomed view is fixed once chosen so the picture does not
        # shift under the box while it is dragged (Chris, 2026-09-12); it
        # is chosen again when the zoom tick changes or a clip opens.
        self._zoom_view: QRect | None = None
        # The box as dragged, in frame pixels: used as-is so the far corners
        # never shift from ratio rounding (Chris, 2026-09-12).
        self._dragged_roi: Roi | None = None
        self._dragged_frame_size: tuple[int, int] | None = None
        # The purple date box (Chris, 2026-09-12): read once per clip and
        # compared with the filename date.
        self._dragged_date_roi: Roi | None = None
        self._date_checked = False
        # The date is re-read two seconds after the last drag of the purple
        # box, not on every move (Chris, 2026-09-12: dragging kept
        # recalculating; the user may still want to pull another corner).
        # Frame 1 of the clip, read once: the date check and the "Date
        # (frame 1)" preview always use it, wherever the scrubber is
        # (Chris, 2026-09-12).
        self._first_frame_cache: np.ndarray | None = None
        self._date_recheck_timer = QTimer(self)
        self._date_recheck_timer.setSingleShot(True)
        self._date_recheck_timer.setInterval(2000)
        self._date_recheck_timer.timeout.connect(self._recheck_date_after_drag)
        self.cctv_date: "date | None" = None
        # Where the camera's date changed in this clip (Chris, 2026-09-12):
        # the clock is read from there, on the date it changed to.
        self.date_sync_frame: int | None = None
        self.cctv_synced_date = None

        self.ocr_label = QLabel("OCR: (not running)")
        self.ocr_label.setAlignment(Qt.AlignCenter)
        self.ocr_enabled_checkbox = QCheckBox("Enable OCR")
        self.ocr_enabled_checkbox.setChecked(True)
        self.ocr_history = QListWidget()
        self.ocr_history.setMinimumWidth(260)
        self.ocr_history.setUniformItemSizes(True)
        self.last_ocr_text: str | None = None
        self.roi_preview = QLabel("Time preview")
        self.roi_preview.setAlignment(Qt.AlignCenter)
        self.roi_preview.setMinimumSize(260, 80)
        # A large view of the purple date box above the time box (Chris,
        # 2026-09-12), each labelled.
        self.date_preview = QLabel("Date preview")
        self.date_preview.setAlignment(Qt.AlignCenter)
        self.date_preview.setMinimumSize(260, 80)
        # The date as it reads once the camera has synced, large, labelled
        # with the frame it happened on (Chris, 2026-09-12).
        self.synced_date_caption = QLabel("Synced date")
        self.synced_date_caption.setStyleSheet("color: #c77dff; font-weight: bold;")
        self.synced_date_preview = QLabel("(no date change found yet)")
        self.synced_date_preview.setAlignment(Qt.AlignCenter)
        self.synced_date_preview.setMinimumSize(260, 80)
        self.synced_date_caption.hide()
        self.synced_date_preview.hide()
        self.date_preview_caption = QLabel("Date (frame 1)")
        self.date_preview_caption.setStyleSheet("color: #c77dff; font-weight: bold;")
        self.time_preview_caption = QLabel("Time (frame 1)")
        self.time_preview_caption.setStyleSheet("color: #00ff5a; font-weight: bold;")

        self.time_label = QLabel("Time: 00:00:00.000")
        self.time_label.setAlignment(Qt.AlignCenter)
        self.status_label = QLabel("Frame: 0")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.tesseract_label = QLabel("Tesseract: (checking)")
        self.tesseract_label.setAlignment(Qt.AlignCenter)
        self.offset_label = QLabel("Offset: (not analyzed)")
        self.offset_label.setAlignment(Qt.AlignCenter)

        self.sync_btn = QPushButton("Sync Time")
        self.sync_btn.setEnabled(False)

        self.seek_slider = QSlider(Qt.Horizontal)
        self.seek_slider.setRange(0, 0)
        self.seek_slider.sliderMoved.connect(self._on_slider_moved)
        # Frame labels around the slider (Chris, 2026-09-12): "Frame 1" on
        # the left, "Frame x" (the last frame) on the right, and "Current
        # frame (y)" riding above the handle.
        self.first_frame_label = QLabel("Frame 1")
        self.last_frame_label = QLabel("Frame \u2013")
        self.first_frame_label.setStyleSheet("color: #9aa0a6;")
        self.last_frame_label.setStyleSheet("color: #9aa0a6;")
        self.current_frame_strip = QWidget()
        self.current_frame_strip.setFixedHeight(18)
        self.current_frame_label = QLabel("Current frame (1)", self.current_frame_strip)
        self.current_frame_label.setStyleSheet("color: #ecf0f4; font-weight: bold;")
        self.current_frame_label.adjustSize()
        self.current_frame_label.hide()

        default_roi = RoiSettings(0.22, 0.06, 0.013, 0.0)
        saved_roi = None
        if self._roi_settings_path and self._roi_settings_key:
            saved_roi = load_roi_settings(self._roi_settings_path, self._roi_settings_key)
        roi = saved_roi or default_roi
        # The box is dragged on the picture (Chris, 2026-09-12); these ratios
        # are the saved form, kept in step with every drag.
        self._roi_ratios = RoiSettings(roi.width_ratio, roi.height_ratio, roi.y_offset_ratio, roi.x_offset_ratio)
        saved_date_roi = None
        if self._roi_settings_path and self._roi_settings_key:
            saved_date_roi = load_roi_settings(self._roi_settings_path, self._roi_settings_key, section="date_roi_by_key")
        self._date_ratios = saved_date_roi or self._default_date_ratios()
        self.zoom_checkbox = QCheckBox("Zoom to the clock area")
        self.zoom_checkbox.setChecked(True)
        self.zoom_checkbox.setToolTip("Show just the top of the picture around the OCR box; untick to see the whole frame")
        self._roi_label = QLabel("")
        self._update_roi_label()

        # Top left: the clip's filename time, hovering shows the full name
        # (Chris, 2026-09-12).
        # A "?" at the top opens a flowchart of the steps (Chris, 2026-09-12).
        # The same pixel-art question block as the other windows (Chris).
        from PySide6.QtCore import QSize
        from PySide6.QtWidgets import QToolButton
        from logfather.ui.icons import question_block_icon
        self.help_btn = QToolButton()
        self.help_btn.setIcon(question_block_icon(32))
        self.help_btn.setIconSize(QSize(32, 32))
        self.help_btn.setFixedSize(36, 36)
        self.help_btn.setCursor(Qt.PointingHandCursor)
        self.help_btn.setStyleSheet(
            "QToolButton { border: none; background: transparent; padding: 0; }"
            "QToolButton:hover { background: rgba(255, 255, 255, 0.08); border-radius: 6px; }"
        )
        self.help_btn.setToolTip("How the date and time sync works")
        self.help_btn.clicked.connect(self._show_help_flowchart)
        self.filename_date_label = QLabel("Filename date: –")
        self.filename_date_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.cctv_date_label = QLabel("CCTV date: \u2013")
        self.cctv_date_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        # One line under it once the camera's date sync is found (Chris,
        # 2026-09-12); hidden otherwise.
        self.date_sync_label = QLabel("")
        self.date_sync_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.date_sync_label.hide()
        self.cctv_date_label.setToolTip("The date read once from the purple box, compared with the filename date")
        self.filename_time_label = QLabel("Filename time: –")
        self.filename_time_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        left_layout = QVBoxLayout()
        top_row = QHBoxLayout()
        top_row.addWidget(self.filename_date_label)
        top_row.addStretch(1)
        top_row.addWidget(self.zoom_checkbox)
        top_row.addSpacing(12)
        top_row.addWidget(self.help_btn)
        left_layout.addLayout(top_row)
        left_layout.addWidget(self.filename_time_label)
        left_layout.addWidget(self.cctv_date_label)
        left_layout.addWidget(self.date_sync_label)
        left_layout.addWidget(self.video_label)
        left_layout.addWidget(self.current_frame_strip)
        slider_row = QHBoxLayout()
        slider_row.setContentsMargins(0, 0, 0, 0)
        slider_row.addWidget(self.first_frame_label)
        slider_row.addWidget(self.seek_slider, 1)
        slider_row.addWidget(self.last_frame_label)
        left_layout.addLayout(slider_row)
        # Frame-step buttons under the slider, the same as the conveyor
        # calibration window: -10, -1, +1, +10 (Chris, 2026-09-12).
        from PySide6.QtCore import QSize
        step_row = QHBoxLayout()
        step_row.setSpacing(4)
        step_row.addStretch(1)
        minus = chr(0x2212)
        for label, delta, tip in (
            (f"{minus}10", -10, "Back 10 frames"),
            (f"{minus}1", -1, "Back 1 frame"),
            ("+1", 1, "Forward 1 frame"),
            ("+10", 10, "Forward 10 frames"),
        ):
            btn = QPushButton(label)
            btn.setFixedSize(QSize(66, 44))
            btn.setToolTip(tip)
            btn.clicked.connect(lambda _checked=False, d=delta: self._scrub_by_frames(d))
            step_row.addWidget(btn)
        step_row.addStretch(1)
        left_layout.addLayout(step_row)
        left_layout.addWidget(self.ocr_label)
        left_layout.addWidget(self.ocr_enabled_checkbox)
        # Tesseract path, Offset, Time and Frame lines removed from the
        # window (Chris, 2026-09-12); the labels keep their text for the
        # code that reads it and stay hidden.
        for hidden in (self.tesseract_label, self.offset_label, self.time_label, self.status_label):
            hidden.hide()
        hint = QLabel(
            "1. Ensure the Date and Time boxes are in the correct place on the CCTV image "
            "(drag a corner or edge to resize, the middle to move).\n"
            "2. Press \"Sync Time\" to find the exact frames when the second changes."
        )
        hint.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        hint.setWordWrap(True)
        left_layout.addWidget(hint)
        left_layout.addWidget(self._roi_label)
        left_layout.addWidget(self.sync_btn)
        left_layout.addStretch(1)

        root_layout = QHBoxLayout()
        root_layout.addLayout(left_layout, 1)
        right_layout = QVBoxLayout()
        # Order (Chris, 2026-09-12): Date (frame 1), then Date (frame x)
        # only when the first frame's date differed from the filename and
        # a sync was found, then Time (frame y).
        right_layout.addWidget(self.date_preview_caption)
        right_layout.addWidget(self.date_preview)
        right_layout.addWidget(self.synced_date_caption)
        right_layout.addWidget(self.synced_date_preview)
        right_layout.addWidget(self.time_preview_caption)
        right_layout.addWidget(self.roi_preview)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.Monospace)
        self.ocr_history.setFont(mono)
        history_header = QLabel(f"{'Frame':>7}  {'Exact time':<10}  FPS")
        # The readings table (Chris, 2026-09-12): every second change in
        # the first OCR_TABLE_SECONDS after the sync frame then a drift
        # check every OCR_TABLE_INTERVAL_SECONDS, found once by the
        # clock checks, with the frames each second lasted. Scrolling only
        # moves the green highlight (the last change at or before the
        # current frame); it never adds rows.
        self._readings: list[tuple[int, str, int | None]] = []
        self._highlighted_row = -1
        history_header.setFont(mono)
        # Same weight as the rows: bold Consolas is wider per character, so
        # the Exact time and FPS headings drifted right of their values
        # (Chris, 2026-09-12). The left inset matches the list frame and
        # item padding so the columns line up.
        history_header.setStyleSheet("color: #ecf0f4;")
        history_header.setContentsMargins(self.ocr_history.frameWidth() + 3, 0, 0, 0)
        history_header.setToolTip(f"Every second change in the {OCR_TABLE_SECONDS} s after the sync frame, then a drift check every {OCR_TABLE_INTERVAL_SECONDS} s: the frame the second began on and how many frames it lasted; green = the last row at or before the current frame")
        right_layout.addWidget(history_header)
        right_layout.addWidget(self.ocr_history, 1)
        root_layout.addLayout(right_layout)
        self.setLayout(root_layout)

        self.sync_btn.clicked.connect(self._analyze_first_10s)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._next_frame)
        self.ocr_available = pytesseract is not None
        if not self.ocr_available:
            self.ocr_label.setText("OCR: pytesseract not available")
        QTimer.singleShot(0, self._update_tesseract_status)
        self._closing = False

        self.zoom_checkbox.stateChanged.connect(self._on_zoom_toggled)
        self._load_roi_settings()
        self.ocr_enabled_checkbox.stateChanged.connect(self._on_ocr_toggle)

    def _update_filename_time_label(self) -> None:
        dt = self.filename_dt
        if dt is None:
            self.filename_date_label.setText("Filename date: not found in the name")
            self.filename_time_label.setText("Filename time: not found in the name")
        else:
            self.filename_date_label.setText(f"Filename date: {dt:%d-%m-%Y}")
            self.filename_time_label.setText(f"Filename time: {dt:%H}h {dt:%M}m {dt:%S}s")
        name = Path(self.current_video_path).name if self.current_video_path else ""
        self.filename_date_label.setToolTip(name)
        self.filename_time_label.setToolTip(name)

    HELP_STEPS = (
        ("A", "Load the Date and Time boxes as placed on the picture", None),
        ("B", "Read the date on frame 1", None),
        ("C", "Does the frame 1 date match the filename date?", "decision"),
        ("D", "No: check the date on each frame against the previous frame", None),
        ("E", "Date changed: show it as Date (frame x); green if it matches the filename, red if not", None),
        ("F", "Yes / then: carry on with the time checks (Sync Time reads the clock from that frame)", None),
        ("G", "Date or Time box moved? Wait two seconds, then start again from A", "loop"),
        ("H", "Store the new Date and Time box locations", None),
    )

    def _show_help_flowchart(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Sync CCTV Time - how it works")
        layout = QVBoxLayout(dlg)
        pic = QLabel()
        pic.setPixmap(self._draw_help_flowchart())
        layout.addWidget(pic)
        close = QPushButton("Close")
        close.clicked.connect(dlg.accept)
        layout.addWidget(close, 0, Qt.AlignRight)
        dlg.exec()

    def _draw_help_flowchart(self) -> QPixmap:
        """The steps as boxes down the page; C is a diamond whose Yes edge
        skips to F, and G loops back to A."""
        width, box_h, gap = 620, 46, 22
        rows = len(self.HELP_STEPS)
        height = 30 + rows * (box_h + gap) + 20
        pm = QPixmap(width, height)
        pm.fill(QColor("#0d1116"))
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.Antialiasing)
        font = painter.font()
        font.setPointSize(10)
        painter.setFont(font)
        left, box_w = 40, width - 120
        centres = []
        for i, (letter, text, kind) in enumerate(self.HELP_STEPS):
            top = 30 + i * (box_h + gap)
            rect = QRect(left, top, box_w, box_h)
            colour = QColor("#c77dff") if letter in ("B", "D", "E") else (QColor("#00ff5a") if letter == "F" else QColor("#9aa0a6"))
            painter.setPen(QPen(colour, 2))
            painter.setBrush(QBrush(QColor("#151b22")))
            if kind == "decision":
                from PySide6.QtGui import QPolygonF
                from PySide6.QtCore import QPointF
                cx, cy = rect.center().x(), rect.center().y()
                painter.drawPolygon(QPolygonF([QPointF(cx, top - 10), QPointF(rect.right() + 24, cy), QPointF(cx, rect.bottom() + 10), QPointF(rect.left() - 24, cy)]))
            else:
                painter.drawRoundedRect(rect, 8, 8)
            painter.setPen(QColor("#ecf0f4"))
            painter.drawText(rect.adjusted(12, 0, -12, 0), Qt.AlignVCenter | Qt.AlignLeft | Qt.TextWordWrap, f"{letter}.  {text}")
            centres.append((rect.center().x(), top, rect.bottom()))
            if i > 0:
                _px, _pt, prev_bottom = centres[i - 1]
                painter.setPen(QPen(QColor("#9aa0a6"), 2))
                painter.drawLine(rect.center().x(), prev_bottom + 6, rect.center().x(), top - 6)
                painter.drawLine(rect.center().x(), top - 6, rect.center().x() - 5, top - 12)
                painter.drawLine(rect.center().x(), top - 6, rect.center().x() + 5, top - 12)
        # C's Yes edge down the right-hand side to F, G's loop up the left to A
        c_cx, c_top, c_bottom = centres[2]
        f_cx, f_top, f_bottom = centres[5]
        x_right = left + box_w + 50
        painter.setPen(QPen(QColor("#00ff5a"), 2))
        painter.drawLine(left + box_w + 20, (c_top + c_bottom) // 2, x_right, (c_top + c_bottom) // 2)
        painter.drawLine(x_right, (c_top + c_bottom) // 2, x_right, (f_top + f_bottom) // 2)
        painter.drawLine(x_right, (f_top + f_bottom) // 2, left + box_w + 4, (f_top + f_bottom) // 2)
        painter.drawText(left + box_w + 24, (c_top + c_bottom) // 2 - 6, "Yes")
        painter.setPen(QPen(QColor("#9aa0a6"), 2))
        painter.drawText(c_cx + 8, c_bottom + 16, "No")
        g_cx, g_top, g_bottom = centres[6]
        painter.drawText(g_cx + 8, g_bottom + 16, "No")
        painter.setPen(QPen(QColor("#f0ad4e"), 2))
        painter.drawText(left - 26, (g_top + g_bottom) // 2 - 6, "Yes")
        a_cx, a_top, a_bottom = centres[0]
        x_left = 14
        painter.setPen(QPen(QColor("#f0ad4e"), 2))
        painter.drawLine(left, (g_top + g_bottom) // 2, x_left, (g_top + g_bottom) // 2)
        painter.drawLine(x_left, (g_top + g_bottom) // 2, x_left, (a_top + a_bottom) // 2)
        painter.drawLine(x_left, (a_top + a_bottom) // 2, left - 2, (a_top + a_bottom) // 2)
        painter.end()
        return pm

    def _open_video_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open Video",
            "",
            "Video Files (*.mp4 *.avi *.mkv *.mov);;All Files (*)",
        )
        if path:
            self.open_video(path)

    def closeEvent(self, event):
        self._closing = True
        try:
            self.timer.stop()
        except Exception:
            pass
        self.playing = False
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None
        super().closeEvent(event)

    def open_video(self, path: str):
        self._zoom_view = None
        self._dragged_roi = None
        self._dragged_date_roi = None
        self._dragged_frame_size = None
        self._readings = []
        self._highlighted_row = -1
        self._date_checked = False
        self._first_frame_cache = None
        self.cctv_date = None
        self.date_sync_frame = None
        self.cctv_synced_date = None
        self.date_sync_label.setText("")
        self.date_sync_label.hide()
        self.synced_date_caption.hide()
        self.synced_date_preview.hide()
        self._synced_date_pixmap = None
        self.cctv_date_label.setText("CCTV date: \u2013")
        self.cctv_date_label.setStyleSheet("")
        if self.cap is not None:
            self.cap.release()
            self.cap = None

        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            self.video_label.setText("Unable to open video")
            self.sync_btn.setEnabled(False)
            self.current_video_path = None
            return
        self.current_video_path = path

        self.cap = cap
        self.fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        self.frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        self.current_frame = 0
        self.current_time_seconds = 0.0
        self.filename_dt = None
        self.estimated_start_dt = None
        self.filename_dt = self._parse_filename_datetime()
        self._update_filename_time_label()
        self.offset_label.setText("Offset: (not analyzed)")
        self.time_label.setText("Time: 00:00:00.000")
        self.ocr_history.clear()
        self.last_ocr_text = None
        self.ocr_enabled_checkbox.setChecked(True)
        self.seek_slider.setRange(0, max(0, self.frame_count - 1))
        self.seek_slider.setValue(0)
        self.last_frame_label.setText(f"Frame {max(1, self.frame_count)}")
        self._place_current_frame_label()
        self.sync_btn.setEnabled(True)
        self.playing = False
        self.timer.stop()
        self._read_and_show(self.current_frame)
        if self._auto_analyze:
            self._analyze_first_10s()

    def _toggle_play_pause(self):
        if self.cap is None:
            return
        if self.playing:
            self.playing = False
            if hasattr(self, "play_btn"):
                self.play_btn.setText("Play")
            self.timer.stop()
        else:
            self.playing = True
            if hasattr(self, "play_btn"):
                self.play_btn.setText("Pause")
            interval_ms = int(1000 / self.fps) if self.fps > 0 else 40
            self.timer.start(interval_ms)

    def _next_frame(self):
        if self.cap is None:
            return
        self.current_frame += 1
        if self.frame_count and self.current_frame >= self.frame_count:
            self._toggle_play_pause()
            return
        self._read_and_show(self.current_frame)

    def _sync_slider(self, frame_index: int) -> None:
        """Move the slider to the frame without firing sliderMoved, and
        put "Current frame (y)" above the handle."""
        self.seek_slider.blockSignals(True)
        self.seek_slider.setValue(int(frame_index))
        self.seek_slider.blockSignals(False)
        self._place_current_frame_label()
        self._highlight_reading(int(frame_index))

    def _place_current_frame_label(self) -> None:
        if self.cap is None or self.frame_count <= 0:
            self.current_frame_label.hide()
            return
        label = self.current_frame_label
        label.setText(f"Current frame ({self.seek_slider.value() + 1})")
        label.adjustSize()
        span = max(1, self.seek_slider.maximum() - self.seek_slider.minimum())
        fraction = (self.seek_slider.value() - self.seek_slider.minimum()) / span
        handle_w = 16
        slider_left = self.seek_slider.x()
        track_w = max(0, self.seek_slider.width() - handle_w)
        centre = slider_left + handle_w / 2 + fraction * track_w
        x = int(round(centre - label.width() / 2))
        x = max(0, min(self.current_frame_strip.width() - label.width(), x))
        label.move(x, 0)
        label.show()

    def _on_slider_moved(self, value: int):
        if self.cap is None:
            return
        self._place_current_frame_label()
        self.playing = False
        if hasattr(self, "play_btn"):
            self.play_btn.setText("Play")
        self.timer.stop()
        self.current_frame = int(value)
        self._read_and_show(self.current_frame)

    def _read_and_show(self, frame_index: int):
        if self._closing:
            return
        if self.cap is None:
            return
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, int(frame_index))
        ret, frame = self.cap.read()
        if not ret or frame is None:
            return
        if self.fps > 0:
            self.current_time_seconds = frame_index / self.fps
        else:
            pos_msec = self.cap.get(cv2.CAP_PROP_POS_MSEC)
            self.current_time_seconds = pos_msec / 1000.0 if pos_msec and pos_msec > 0 else 0.0
        self._show_frame(frame)
        self._update_ocr(frame)
        self._update_status()
        self._sync_slider(frame_index)

    def _on_zoom_toggled(self, _state: int) -> None:
        self._zoom_view = None
        self._rerender()

    def _view_rect(self, frame_w: int, frame_h: int, roi: Roi) -> QRect:
        """The part of the frame shown: the whole frame, or a band around
        the clock (wide enough to drag comfortably) when zoomed. The band
        is chosen once and kept while the box is dragged."""
        if not self.zoom_checkbox.isChecked():
            return QRect(0, 0, frame_w, frame_h)
        if self._zoom_view is not None and self._zoom_view.width() <= frame_w and self._zoom_view.height() <= frame_h:
            return self._zoom_view
        view_w = int(max(frame_w * 0.5, roi.w * 3.0))
        view_h = int(max(frame_h * 0.18, roi.h * 5.0))
        view_w = min(frame_w, view_w)
        view_h = min(frame_h, view_h)
        cx = roi.x + roi.w / 2
        x = int(max(0, min(frame_w - view_w, cx - view_w / 2)))
        y = int(max(0, min(frame_h - view_h, roi.y - roi.h * 1.5)))
        self._zoom_view = QRect(x, y, view_w, view_h)
        return self._zoom_view

    def _rerender(self) -> None:
        if self._last_frame is not None and not self._closing:
            self._show_frame(self._last_frame)

    def _show_frame(self, frame_bgr: np.ndarray):
        if self._closing:
            return
        self._last_frame = frame_bgr
        frame_h, frame_w = frame_bgr.shape[:2]
        roi = self._current_roi(frame_w, frame_h)
        date_roi = self._current_date_roi(frame_w, frame_h)
        frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        h, w, ch = frame_rgb.shape
        qimg = QImage(frame_rgb.data, w, h, ch * w, QImage.Format_RGB888).copy()
        both = Roi(min(roi.x, date_roi.x), min(roi.y, date_roi.y),
                   max(roi.x + roi.w, date_roi.x + date_roi.w) - min(roi.x, date_roi.x),
                   max(roi.y + roi.h, date_roi.y + date_roi.h) - min(roi.y, date_roi.y))
        self.video_label.set_picture(QPixmap.fromImage(qimg), self._view_rect(frame_w, frame_h, both), roi, date_roi)
        if not self._date_checked:
            self._date_checked = True
            self._check_cctv_date(self._frame_or_first(frame_bgr))
        roi_bgr = roi.crop(frame_bgr)
        roi_rgb = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2RGB)
        rh, rw, rch = roi_rgb.shape
        roi_qimg = QImage(roi_rgb.data, rw, rh, rch * rw, QImage.Format_RGB888).copy()
        roi_pixmap = QPixmap.fromImage(roi_qimg)
        self.roi_preview.setPixmap(
            roi_pixmap.scaled(self.roi_preview.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        )
        # The date preview is the first frame's, read once; the time preview
        # follows the frame on screen (Chris, 2026-09-12).
        self.time_preview_caption.setText(f"Time (frame {self.current_frame + 1})")
        if self.current_frame == 0 or self._dragged_date_roi is not None or self.date_preview.pixmap() is None or self.date_preview.pixmap().isNull():
            first = self._first_frame()
            date_bgr = date_roi.crop(first if first is not None else frame_bgr)
            date_rgb = cv2.cvtColor(date_bgr, cv2.COLOR_BGR2RGB)
            dh, dw, dch = date_rgb.shape
            date_qimg = QImage(date_rgb.data, dw, dh, dch * dw, QImage.Format_RGB888).copy()
            self.date_preview.setPixmap(
                QPixmap.fromImage(date_qimg).scaled(self.date_preview.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            )
            self.date_preview_caption.setText("Date (frame 1)")

    def _update_ocr(self, frame_bgr: np.ndarray):
        if not self.ocr_available or not self.ocr_enabled_checkbox.isChecked():
            return
        try:
            roi = self._current_roi(frame_bgr.shape[1], frame_bgr.shape[0])
            raw_text = ocr_time_from_frame(frame_bgr, roi=roi)
        except Exception as exc:
            self.ocr_label.setText(f"OCR error: {exc}")
            self._update_tesseract_status()
            return
        text = _normalize_ocr_text(raw_text)
        valid = _is_valid_time_text(text)
        suffix = " (valid)" if valid else ""
        self.ocr_label.setText(f"OCR: {text or '(blank)'}{suffix}")
        self.last_ocr_text = text

    def _update_status(self):
        self.status_label.setText(f"Frame: {self.current_frame + 1}/{self.frame_count}")
        total_ms, hours, minutes, seconds, ms, actual_dt = self._display_time_parts()
        if actual_dt:
            self.time_label.setText(actual_dt.strftime("Time: %d/%m/%Y %H:%M:%S.") + f"{ms:03}")
        else:
            self.time_label.setText(f"Time: {hours:02}:{minutes:02}:{seconds:02}.{ms:03}")

    def _current_actual_time_str(self) -> str:
        total_ms, hours, minutes, seconds, ms, actual_dt = self._display_time_parts()
        if actual_dt:
            return actual_dt.strftime("%H:%M:%S.") + f"{ms:03}"
        return f"{hours:02}:{minutes:02}:{seconds:02}.{ms:03}"

    def _update_tesseract_status(self):
        if pytesseract is None:
            self.tesseract_label.setText("Tesseract: not available")
            return
        try:
            _ensure_tesseract()
        except Exception as exc:
            self.tesseract_label.setText(f"Tesseract: {exc}")
            return
        module = pytesseract_module or pytesseract
        configured = getattr(module, "tesseract_cmd", "") or ""
        path = Path(configured) if configured else None
        if path and path.exists():
            ok, msg = _probe_tesseract()
            if ok:
                self.tesseract_label.setText(f"Tesseract: {path}")
            else:
                self.tesseract_label.setText(f"Tesseract: {path} ({msg})")
        elif configured:
            resolved = shutil.which(configured)
            if resolved:
                self.tesseract_label.setText(f"Tesseract: {resolved}")
            else:
                self.tesseract_label.setText(f"Tesseract: {configured} (missing)")
        else:
            self.tesseract_label.setText("Tesseract: not configured")

    def _build_readings_table(self) -> None:
        """Fill the Frame / Exact time / FPS table: every second change in
        the first OCR_TABLE_SECONDS after the sync frame, then one drift
        check every OCR_TABLE_INTERVAL_SECONDS; read once."""
        self.ocr_history.clear()
        self._readings = []
        self._highlighted_row = -1
        if self.cap is None or self.fps <= 0 or self.frame_count <= 0 or not self.ocr_available:
            return
        first = self._first_frame()
        if first is None:
            return
        roi = self._current_roi(first.shape[1], first.shape[0])
        start = int(self.date_sync_frame or 0)
        last = self.frame_count - 1
        interval = max(1, int(round(OCR_TABLE_INTERVAL_SECONDS * self.fps)))
        window = max(2, int(math.ceil(OCR_TABLE_WINDOW_SECONDS * self.fps)))
        initial_end = min(last, start + int(math.ceil(OCR_TABLE_SECONDS * self.fps)))
        # (span start, span end, every boundary or just the first two)
        spans: list[tuple[int, int, bool]] = []
        if initial_end > start:
            spans.append((start, initial_end, True))
        spans.extend((f, f + window, False) for f in range(start + interval, last, interval) if f + window <= last)
        if not spans:
            self.ocr_history.addItem(QListWidgetItem("(clip too short for a check)"))
            return
        step = max(1, int(round(self.fps * OCR_SYNC_COARSE_STEP_SECONDS)))
        progress = QProgressDialog(f"Reading the first {OCR_TABLE_SECONDS} s, then a drift check every {OCR_TABLE_INTERVAL_SECONDS} s...", None, 0, len(spans), self)
        progress.setWindowTitle("Readings")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        cache: dict[int, str | None] = {}

        def read_text(frame_idx: int) -> str | None:
            if frame_idx in cache:
                return cache[frame_idx]
            QApplication.processEvents()
            text = None
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, int(frame_idx))
            ret, frame = self.cap.read()
            if ret and frame is not None:
                try:
                    candidate = _normalize_ocr_text(ocr_time_from_frame(frame, roi=roi))
                except Exception:
                    candidate = ""
                if _is_valid_time_text(candidate):
                    text = candidate
            cache[frame_idx] = text
            return text

        misses = 0
        try:
            for i, (span_start, span_end, every) in enumerate(spans):
                progress.setValue(i)
                QApplication.processEvents()
                boundaries = find_second_boundaries(read_text, span_start, span_end, step)
                if not boundaries:
                    misses += 1
                    continue
                if not every:
                    boundaries = boundaries[:2]
                for j, (frame_idx, text) in enumerate(boundaries):
                    if not every and j > 0:
                        break  # a drift check shows one row; its FPS comes from the second change
                    fps = boundaries[j + 1][0] - frame_idx if j + 1 < len(boundaries) else None
                    self._readings.append((frame_idx, text, fps))
                    self.ocr_history.addItem(QListWidgetItem(f"{frame_idx + 1:>7}  {text:<10}  {fps if fps is not None else '':>3}"))
        finally:
            progress.close()
        if misses:
            self.ocr_history.addItem(QListWidgetItem(f"({misses} of {len(spans)} checks could not read a second change)"))
        self._highlight_reading(self.current_frame)

    def _highlight_reading(self, frame_index: int) -> None:
        """Green on the row whose second change is the closest at or before
        the frame on screen; every other row plain."""
        if not self._readings:
            return
        row = -1
        for i, (frame_idx, _text, _fps) in enumerate(self._readings):
            if frame_idx <= frame_index:
                row = i
            else:
                break
        if row == self._highlighted_row:
            return
        if 0 <= self._highlighted_row < self.ocr_history.count():
            old = self.ocr_history.item(self._highlighted_row)
            old.setBackground(QBrush())
            old.setForeground(QBrush())
        if 0 <= row < self.ocr_history.count():
            item = self.ocr_history.item(row)
            item.setBackground(QColor("#2d6a2d"))
            item.setForeground(QColor("#ffffff"))
            self.ocr_history.scrollToItem(item)
        self._highlighted_row = row

    def _current_roi(self, frame_w: int, frame_h: int) -> Roi:
        if self._dragged_roi is not None and self._dragged_frame_size == (frame_w, frame_h):
            return self._dragged_roi
        r = self._roi_ratios
        return Roi.top_center_time(
            frame_w,
            frame_h,
            width_ratio=r.width_ratio,
            height_ratio=r.height_ratio,
            y_offset_ratio=r.y_offset_ratio,
            x_offset_ratio=r.x_offset_ratio,
        )

    def _default_date_ratios(self) -> RoiSettings:
        """The date box starts to the left of the time box, the same size."""
        t = self._roi_ratios
        return RoiSettings(t.width_ratio, t.height_ratio, t.y_offset_ratio, t.x_offset_ratio - t.width_ratio - 0.01)

    def _current_date_roi(self, frame_w: int, frame_h: int) -> Roi:
        if self._dragged_date_roi is not None and self._dragged_frame_size == (frame_w, frame_h):
            return self._dragged_date_roi
        r = self._date_ratios
        return Roi.top_center_time(frame_w, frame_h, width_ratio=r.width_ratio, height_ratio=r.height_ratio,
                                   y_offset_ratio=r.y_offset_ratio, x_offset_ratio=r.x_offset_ratio)

    def _on_box_dragged(self, name: str, roi: Roi) -> None:
        if name == "date":
            self._on_date_roi_dragged(roi)
        else:
            self._on_roi_dragged(roi)

    def _on_date_roi_dragged(self, roi: Roi) -> None:
        if self._last_frame is None:
            return
        frame_h, frame_w = self._last_frame.shape[:2]
        self._dragged_date_roi = Roi(roi.x, roi.y, roi.w, roi.h)
        self._dragged_frame_size = (frame_w, frame_h)
        self._date_ratios = roi_to_ratios(roi, frame_w, frame_h)
        self._rerender()
        self._date_recheck_timer.start()

    def _recheck_date_after_drag(self) -> None:
        """Step G: a box changed, so run the whole procedure again from A
        (the date steps, then F, the clock checks), then H: store where
        the boxes now are."""
        if self._last_frame is None or self._closing:
            return
        self._check_cctv_date(self._frame_or_first(self._last_frame))
        self._rerender()
        self._analyze_first_10s()
        self._store_box_locations()

    def _store_box_locations(self) -> None:
        """Step H: save the date and time box positions (as ratios of the
        frame) to ocr_settings.json for this camera."""
        self._save_roi_settings()
        if self._roi_settings_path and self._roi_settings_key and self._date_ratios is not None:
            try:
                save_roi_settings(self._roi_settings_path, self._roi_settings_key, self._date_ratios, section="date_roi_by_key")
            except Exception:
                pass

    def _frame_or_first(self, fallback: np.ndarray) -> np.ndarray:
        """Frame 1 when it can be read, else `fallback`. (`a or b` on
        numpy arrays raises, which silently killed the date procedure.)"""
        first = self._first_frame()
        return fallback if first is None else first

    def _first_frame(self) -> np.ndarray | None:
        """Frame 1 of the open clip, cached for the clip's lifetime."""
        if self._first_frame_cache is not None:
            return self._first_frame_cache
        if self.cap is None:
            return None
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        ret, frame = self.cap.read()
        if not ret or frame is None:
            return None
        self._first_frame_cache = frame
        return frame

    def _check_cctv_date(self, frame_bgr: np.ndarray) -> None:
        """The date procedure (Chris, 2026-09-12), run when a clip opens and
        again two seconds after the date box was last dragged:

        A. use the date and time boxes as placed;
        B. read the date on frame 1;
        C. compare it with the filename date - a match means the camera was
           synced from the start, so skip to F;
        D. otherwise scan the clip for the frame where the date changes
           from what frame 1 showed (one read a second, then every frame
           between the last old and the first new reading);
        E. show that frame's date in the "Date (frame x)" panel and compare
           it with the filename date: green when they agree, red when not;
        F. the clock checks carry on as before, reading from that frame;
        G. a moved date or time box waits two seconds and reruns from A;
        H. the date and time box locations are stored.
        """
        # A + B: frame 1, the date box as placed
        first = self._first_frame()
        if first is None:
            first = frame_bgr
        self.date_sync_frame = None
        self.cctv_synced_date = None
        self.date_sync_label.hide()
        self.synced_date_caption.hide()
        self.synced_date_preview.hide()
        if not self.ocr_available:
            self.cctv_date_label.setText("CCTV date: OCR not available")
            self.cctv_date_label.setStyleSheet("")
            return
        frame_h, frame_w = first.shape[:2]
        try:
            text = ocr_date_from_frame(first, roi=self._current_date_roi(frame_w, frame_h))
        except Exception as exc:
            self.cctv_date_label.setText(f"CCTV date: OCR error: {exc}")
            self.cctv_date_label.setStyleSheet("")
            return
        self.cctv_date = parse_cctv_date(text)
        filename_date = self.filename_dt.date() if self.filename_dt is not None else None
        # C: frame 1 against the filename
        if self.cctv_date is None:
            self.cctv_date_label.setText(f"CCTV date: not read on frame 1 ({text.strip() or 'blank'})")
            self.cctv_date_label.setStyleSheet("color: #9aa0a6;")
        elif filename_date is not None and self.cctv_date == filename_date:
            self.cctv_date_label.setText(f"CCTV date: {self.cctv_date:%d/%m/%Y} on frame 1 - matches the filename")
            self.cctv_date_label.setStyleSheet("color: #2ecc71;")
            self.date_sync_frame = 0
            self.cctv_synced_date = self.cctv_date
            return  # F
        elif self.cctv_date == _epoch_date():
            self.cctv_date_label.setText("CCTV date: 01/01/1970 on frame 1 - the camera had not synced yet")
            self.cctv_date_label.setStyleSheet("color: #f0ad4e;")
        elif filename_date is None:
            self.cctv_date_label.setText(f"CCTV date: {self.cctv_date:%d/%m/%Y} on frame 1 (no filename date to compare)")
            self.cctv_date_label.setStyleSheet("")
            return
        else:
            self.cctv_date_label.setText(f"CCTV date: {self.cctv_date:%d/%m/%Y} on frame 1 - differs from the filename ({filename_date:%d/%m/%Y})")
            self.cctv_date_label.setStyleSheet("color: #f0ad4e;")
        # D + E
        self._scan_for_date_sync(filename_date)

    def _scan_for_date_sync(self, filename_date) -> None:
        """Steps D and E: find the frame where the burnt-in date changes
        from frame 1's, show it, and judge it against the filename date."""
        if self.cap is None or self.fps <= 0 or self.frame_count <= 0:
            return
        first = self._first_frame()
        if first is None:
            return
        frame_h, frame_w = first.shape[:2]
        date_roi = self._current_date_roi(frame_w, frame_h)
        # Wording and a Cancel button (Chris, 2026-09-13).
        progress = QProgressDialog("Comparing displayed date to filename date...", "Cancel", 0, max(1, self.frame_count), self)
        progress.setWindowTitle("Camera date")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)

        def _on_progress(frame_idx: int) -> None:
            progress.setValue(min(self.frame_count, int(frame_idx)))
            QApplication.processEvents()

        try:
            change = find_date_change_frame(self.cap, self.fps, self.frame_count, date_roi, should_abort=progress.wasCanceled, on_progress=_on_progress)
        finally:
            cancelled = progress.wasCanceled()
            progress.close()
        if cancelled:
            self.date_sync_label.setText("Camera date sync: cancelled - the clock is read from frame 1")
            self.date_sync_label.setStyleSheet("color: #f0ad4e;")
            self.date_sync_label.setToolTip("The date scan was cancelled; drag the date box or reopen the clip to run it again")
            self.date_sync_label.show()
            return
        initial = self.cctv_date.strftime("%d/%m/%Y") if self.cctv_date else "unreadable"
        if change is None:
            self.date_sync_label.setText(f"Camera date sync: none - the date stayed {initial} for the whole clip")
            self.date_sync_label.setStyleSheet("color: #ff7a70; font-weight: bold;")
            self.date_sync_label.setToolTip("The camera never synced its date in this clip, so its clock cannot be trusted here")
            self.date_sync_label.show()
            return
        change_frame, _initial_date, new_date = change
        self.date_sync_frame = int(change_frame)
        self.cctv_synced_date = new_date
        agrees = filename_date is not None and new_date == filename_date
        if agrees:
            verdict = "matches the filename"
        elif filename_date is not None:
            verdict = f"does NOT match the filename ({filename_date:%d/%m/%Y})"
        else:
            verdict = "no filename date to compare"
        self.date_sync_label.setText(f"Camera date sync: {initial} -> {new_date:%d/%m/%Y} (frame {change_frame + 1}) - {verdict}")
        self.date_sync_label.setStyleSheet("color: #2ecc71;" if agrees else "color: #ff7a70; font-weight: bold;")
        self.date_sync_label.setToolTip(f"{change_frame} frames ({change_frame / self.fps:.1f} s) before the camera synced; the clock is read from that frame on {new_date:%d/%m/%Y}")
        self.date_sync_label.show()
        self._show_synced_date_preview(change_frame, date_roi)
        self.synced_date_caption.setStyleSheet("color: #c77dff; font-weight: bold;")  # purple like the box (Chris, 2026-09-12)
        if self.cap is not None:
            self._read_and_show(self.date_sync_frame)

    def _show_synced_date_preview(self, change_frame: int, date_roi: Roi) -> None:
        """The date box cropped from the frame the camera synced on."""
        if self.cap is None:
            return
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, int(change_frame))
        ret, frame = self.cap.read()
        if not ret or frame is None:
            return
        crop = date_roi.crop(frame)
        rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb.shape
        qimg = QImage(rgb.data, w, h, ch * w, QImage.Format_RGB888).copy()
        # Keep the full crop and scale it to the same size as the other two
        # previews once shown (the hidden label had no size to scale to,
        # so the picture came out cropped; Chris, 2026-09-12).
        self._synced_date_pixmap = QPixmap.fromImage(qimg)
        self.synced_date_caption.setText(f"Date (frame {change_frame + 1})")
        self.synced_date_caption.show()
        self.synced_date_preview.show()
        self._rescale_synced_date_preview()

    def _rescale_synced_date_preview(self) -> None:
        pm = getattr(self, "_synced_date_pixmap", None)
        if pm is None or pm.isNull():
            return
        target = self.date_preview.size()
        if target.width() < 260 or target.height() < 80:
            target = self.date_preview.minimumSize()
        self.synced_date_preview.setPixmap(pm.scaled(target, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def _update_roi_label(self):
        r = self._roi_ratios
        self._roi_label.setText(
            f"OCR box: w={r.width_ratio:.3f} h={r.height_ratio:.3f} y={r.y_offset_ratio:.3f} x={r.x_offset_ratio:+.3f} of the frame"
        )

    def _on_roi_dragged(self, roi: Roi) -> None:
        """The box was dragged on the picture: keep the ratios, save, and
        redraw the preview from the frame already on screen."""
        if self._last_frame is None:
            return
        frame_h, frame_w = self._last_frame.shape[:2]
        self._dragged_roi = Roi(roi.x, roi.y, roi.w, roi.h)
        self._dragged_frame_size = (frame_w, frame_h)
        self._roi_ratios = roi_to_ratios(roi, frame_w, frame_h)
        self._update_roi_label()
        self._rerender()
        if self.ocr_enabled_checkbox.isChecked() and self.ocr_available:
            self._update_ocr(self._last_frame)
        self._date_recheck_timer.start()

    def _on_ocr_toggle(self, _state: int):
        if self.cap is None:
            return
        if not self.ocr_enabled_checkbox.isChecked():
            self.ocr_label.setText("OCR: (disabled)")
            return
        self._read_and_show(self.current_frame)

    def _scrub_by_frames(self, delta_frames: int):
        if self.cap is None:
            return
        if self.playing:
            self.playing = False
            if hasattr(self, "play_btn"):
                self.play_btn.setText("Play")
            self.timer.stop()
        new_frame = self.current_frame + delta_frames
        if self.frame_count > 0:
            new_frame = max(0, min(self.frame_count - 1, new_frame))
        else:
            new_frame = max(0, new_frame)
        self.current_frame = new_frame
        self._read_and_show(self.current_frame)

    def _settings_path(self) -> Path:
        if self._roi_settings_path:
            return self._roi_settings_path
        return Path.cwd() / "time_ocr_settings.json"

    def _load_roi_settings(self) -> None:
        if self._roi_settings_path and self._roi_settings_key:
            settings = load_roi_settings(self._roi_settings_path, self._roi_settings_key)
            if settings:
                self._roi_ratios = settings
                self._update_roi_label()
                return
        path = self._settings_path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        r = self._roi_ratios
        vals = []
        for name, current in (("width_ratio", r.width_ratio), ("height_ratio", r.height_ratio), ("y_offset_ratio", r.y_offset_ratio), ("x_offset_ratio", r.x_offset_ratio)):
            value = data.get(name)
            vals.append(float(value) if isinstance(value, (int, float)) else current)
        self._roi_ratios = RoiSettings(*vals)
        self._update_roi_label()

    def _save_roi_settings(self) -> None:
        settings = self._roi_ratios
        if self._roi_settings_path and self._roi_settings_key:
            save_roi_settings(self._roi_settings_path, self._roi_settings_key, settings)
            return
        data = {
            "width_ratio": settings.width_ratio,
            "height_ratio": settings.height_ratio,
            "y_offset_ratio": settings.y_offset_ratio,
            "x_offset_ratio": settings.x_offset_ratio,
        }
        path = self._settings_path()
        try:
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

    def resizeEvent(self, event):
        QTimer.singleShot(0, self._place_current_frame_label)
        super().resizeEvent(event)
        self._rerender()
        self._rescale_synced_date_preview()

    def _analyze_first_10s(self):
        """Step F: the clock checks, then the readings table, then back to
        the frame that was on screen."""
        self._run_sync_analysis()
        if self._closing or self.cap is None:
            return
        self._build_readings_table()
        self._read_and_show(self.current_frame)

    def _run_sync_analysis(self):
        if self.cap is None:
            return
        if not self.ocr_enabled_checkbox.isChecked():
            self.offset_label.setText("Offset: OCR disabled")
            return
        if self.playing:
            self.playing = False
            self.play_btn.setText("Play")
            self.timer.stop()
        self.offset_label.setText("Offset: (analyzing...)")
        filename_dt = self._parse_filename_datetime()
        if filename_dt is None:
            self.offset_label.setText("Offset: filename time not found")
            return
        def _pick_best_start(samples: list[tuple[int, float, datetime, str]]) -> datetime | None:
            transition_result = self._estimate_start_from_transitions(samples)
            if transition_result is None:
                inferred = []
                for _frame_idx, video_t, ocr_dt, ocr_text in samples:
                    inferred_start = ocr_dt - timedelta(seconds=video_t)
                    inferred.append((inferred_start, ocr_text))
                inferred.sort(key=lambda item: item[0])
                if not inferred:
                    return None  # no usable OCR samples (e.g. clock unreadable)
                median_start = inferred[len(inferred) // 2][0]
                inliers = [
                    item for item in inferred
                    if abs((item[0] - median_start).total_seconds()) <= 2.0
                ]
                if not inliers:
                    return None
                outliers = [item for item in inferred if item not in inliers]
                if outliers:
                    for outlier_start, outlier_text in outliers:
                        delta = (outlier_start - median_start).total_seconds()
                        print(f"[ocr] disregarded sample {outlier_text} (offset {delta:+.2f}s)", flush=True)
                return inliers[len(inliers) // 2][0]
            best_start, median_start, outliers = transition_result
            if outliers:
                for outlier_start, outlier_text in outliers:
                    delta = (outlier_start - median_start).total_seconds()
                    print(f"[ocr] disregarded transition {outlier_text} (offset {delta:+.2f}s)", flush=True)
            return best_start

        fast_seconds = OCR_SYNC_FAST_SECONDS
        fallback_seconds = OCR_SYNC_FALLBACK_SECONDS
        # Read the clock from the frame where the camera's date synced, on
        # the date it synced to (Chris, 2026-09-12).
        start_frame = int(self.date_sync_frame or 0)
        if self.cctv_synced_date is not None:
            filename_dt = filename_dt.replace(year=self.cctv_synced_date.year, month=self.cctv_synced_date.month, day=self.cctv_synced_date.day)
        roi = None
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
        ret, frame = self.cap.read()
        if ret and frame is not None:
            roi = self._current_roi(frame.shape[1], frame.shape[0])

        samples: list[tuple[int, float, datetime, str]] = []
        if roi is not None:
            samples = _find_second_boundary_samples_for_cap(
                self.cap,
                self.fps,
                self.frame_count,
                seconds=fast_seconds,
                base_dt=filename_dt,
                roi=roi,
                parent=self,
                progress_label=f"Scanning first {fast_seconds}s (coarse)...",
                start_frame=start_frame,
            )
        best_start = _pick_best_start(samples) if samples else None
        if best_start is None:
            samples = self._collect_ocr_samples(
                start_frame=start_frame,
                seconds=fast_seconds,
                progress_label=f"Analyzing first {fast_seconds}s...",
            )
            best_start = _pick_best_start(samples)
        if best_start is None and fallback_seconds > fast_seconds:
            if roi is not None:
                samples = _find_second_boundary_samples_for_cap(
                    self.cap,
                    self.fps,
                    self.frame_count,
                    seconds=fallback_seconds,
                    base_dt=filename_dt,
                    roi=roi,
                    parent=self,
                    progress_label=f"Scanning first {fallback_seconds}s (coarse)...",
                    start_frame=start_frame,
                )
                best_start = _pick_best_start(samples) if samples else None
        if best_start is None and fallback_seconds > fast_seconds:
            samples = self._collect_ocr_samples(
                start_frame=start_frame,
                seconds=fallback_seconds,
                progress_label=f"Analyzing first {fallback_seconds}s...",
            )
            if not samples:
                self.offset_label.setText("Offset: no valid OCR samples")
                return
            best_start = _pick_best_start(samples)
        if best_start is None:
            self.offset_label.setText("Offset: no consistent OCR samples")
            return
        offset = best_start - filename_dt
        self.estimated_start_dt = best_start
        self.time_frame_offset, report = self._verify_frame_offset(best_start)
        self.offset_label.setText(
            f"Offset: {offset.total_seconds():+.2f}s vs filename"
        )
        self.ocr_enabled_checkbox.setChecked(False)
        if callable(self._on_offset_approved):
            def _notify():
                self._on_offset_approved(
                    best_start,
                    offset.total_seconds(),
                    int(self.time_frame_offset),
                )
            QTimer.singleShot(0, _notify)
        self.offset_label.setText(
            f"Offset applied: {offset.total_seconds():+.2f}s vs filename"
        )

    def _collect_ocr_samples(
        self,
        *,
        seconds: int,
        progress_label: str,
        start_frame: int = 0,
    ) -> list[tuple[int, float, datetime, str]]:
        if self.cap is None or self.fps <= 0 or self.frame_count <= 0:
            return []
        roi = None
        max_frame_idx = int(math.ceil(seconds * self.fps))
        if max_frame_idx < 0:
            return []
        max_frame_idx = min(self.frame_count - 1, max_frame_idx)
        max_frames = max_frame_idx + 1
        if max_frames <= 0:
            return []
        step = 1
        start_frame = max(0, int(start_frame))
        filename_dt = self._parse_filename_datetime()
        if filename_dt is None:
            return []
        samples: list[tuple[int, float, datetime, str]] = []
        progress = QProgressDialog(progress_label, None, 0, max_frames, self)
        progress.setWindowTitle("OCR Analysis")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        for frame_idx in range(start_frame, min(self.frame_count, start_frame + max_frames), step):
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, int(frame_idx))
            ret, frame = self.cap.read()
            if not ret or frame is None:
                continue
            self.current_frame = frame_idx
            self._show_frame(frame)
            self._update_status()
            self._sync_slider(frame_idx)
            progress.setValue(frame_idx + 1)
            QApplication.processEvents()
            if roi is None:
                roi = self._current_roi(frame.shape[1], frame.shape[0])
            try:
                raw_text = ocr_time_from_frame(frame, roi=roi)
            except Exception:
                continue
            text = _normalize_ocr_text(raw_text)
            if not _is_valid_time_text(text):
                continue
            ocr_dt = self._combine_date_and_time(filename_dt, text)
            if self.fps > 0:
                video_t = frame_idx / self.fps
            else:
                pos_msec = self.cap.get(cv2.CAP_PROP_POS_MSEC)
                video_t = pos_msec / 1000.0 if pos_msec and pos_msec > 0 else 0.0
            samples.append((frame_idx, video_t, ocr_dt, text))
        progress.setValue(max_frames)
        progress.close()
        return samples

    def _parse_filename_datetime(self) -> datetime | None:
        if self.filename_dt is not None:
            return self.filename_dt
        if self.current_video_path:
            name = Path(self.current_video_path).name
        else:
            return None
        match = re.search(r"(\d{14})", name)
        if not match:
            return None
        self.filename_dt = datetime.strptime(match.group(1), "%Y%m%d%H%M%S")
        return self.filename_dt

    def _combine_date_and_time(self, base_dt: datetime, time_text: str) -> datetime:
        hour, minute, second = (int(part) for part in time_text.split(":"))
        candidate = base_dt.replace(hour=hour, minute=minute, second=second, microsecond=0)
        if candidate < base_dt and (base_dt - candidate) > timedelta(hours=12):
            candidate += timedelta(days=1)
        return candidate

    def _offset_seconds(self) -> float:
        if self.fps > 0:
            return self.time_frame_offset / self.fps
        return 0.0

    def _display_time_seconds(self) -> float:
        return self.current_time_seconds + self._offset_seconds()

    def _display_time_parts(self) -> tuple[int, int, int, int, int, datetime | None]:
        display_seconds = self._display_time_seconds()
        if self.estimated_start_dt:
            actual_dt = self.estimated_start_dt + timedelta(seconds=display_seconds)
            hours = actual_dt.hour
            minutes = actual_dt.minute
            seconds = actual_dt.second
            ms = int(actual_dt.microsecond / 1000)
            total_ms = int(display_seconds * 1000)
        else:
            total_ms = int(display_seconds * 1000)
            if total_ms < 0:
                total_ms = 0
            hours = total_ms // 3_600_000
            rem = total_ms % 3_600_000
            minutes = rem // 60_000
            rem = rem % 60_000
            seconds = rem // 1000
            ms = rem % 1000
            actual_dt = None
        return total_ms, hours, minutes, seconds, ms, actual_dt


    def _time_text_to_seconds(self, time_text: str) -> int | None:
        try:
            hour, minute, second = (int(part) for part in time_text.split(":"))
        except ValueError:
            return None
        if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
            return None
        return hour * 3600 + minute * 60 + second

    def _estimate_start_from_transitions(
        self,
        samples: list[tuple[int, float, datetime, str]],
    ) -> tuple[datetime, datetime, list[tuple[datetime, str]]] | None:
        if len(samples) < 2:
            return None
        transitions = []
        for idx in range(1, len(samples)):
            prev_frame, prev_t, prev_dt, prev_text = samples[idx - 1]
            curr_frame, curr_t, curr_dt, curr_text = samples[idx]
            if prev_text == curr_text:
                continue
            prev_secs = self._time_text_to_seconds(prev_text)
            curr_secs = self._time_text_to_seconds(curr_text)
            if prev_secs is None or curr_secs is None:
                continue
            if curr_secs != (prev_secs + 1) % 86400:
                continue
            boundary_video_t = curr_t
            boundary_ocr_dt = curr_dt
            inferred_start = boundary_ocr_dt - timedelta(seconds=boundary_video_t)
            transitions.append((inferred_start, curr_text))
        if not transitions:
            return None
        transitions.sort(key=lambda item: item[0])
        median_start = transitions[len(transitions) // 2][0]
        inliers = [
            item for item in transitions
            if abs((item[0] - median_start).total_seconds()) <= 1.0
        ]
        if not inliers:
            return None
        outliers = [item for item in transitions if item not in inliers]
        best_start = inliers[len(inliers) // 2][0]
        return best_start, median_start, outliers

    def _verify_frame_offset(
        self,
        estimated_start: datetime,
    ) -> tuple[int, list[tuple[str, str]]]:
        if self.cap is None or self.fps <= 0 or self.frame_count <= 0:
            return 0, [("Unable to verify frame offset (no video/fps).", "info")]
        mid_frame = self.frame_count // 2
        mid_dt = estimated_start + timedelta(seconds=mid_frame / self.fps)
        target_second = mid_dt.replace(microsecond=0)
        target_seconds = (target_second - estimated_start).total_seconds()
        candidates = [-2, -1, 0, 1, 2]
        best_offset = 0
        best_score = -1
        lines: list[tuple[str, str]] = [
            (
                f"Verifying around mid-frame {mid_frame} "
                f"({target_second.strftime('%H:%M:%S')})",
                "info",
            ),
        ]

        for offset in candidates:
            boundary_frame = int(math.ceil(target_seconds * self.fps - offset))
            frames = [
                max(0, boundary_frame - 1),
                max(0, min(self.frame_count - 1, boundary_frame)),
                max(0, min(self.frame_count - 1, boundary_frame + 1)),
            ]
            score = 0
            lines.append((f"Offset {offset:+d} frames:", "info"))
            for frame_idx in frames:
                self.cap.set(cv2.CAP_PROP_POS_FRAMES, int(frame_idx))
                ret, frame = self.cap.read()
                if not ret or frame is None:
                    lines.append((f"  frame {frame_idx + 1}: read failed", "miss"))
                    continue
                roi = self._current_roi(frame.shape[1], frame.shape[0])
                try:
                    raw_text = ocr_time_from_frame(frame, roi=roi)
                except Exception as exc:
                    lines.append((f"  frame {frame_idx + 1}: OCR error {exc}", "miss"))
                    continue
                ocr_text = _normalize_ocr_text(raw_text)
                calc_dt = estimated_start + timedelta(
                    seconds=(frame_idx + offset) / self.fps
                )
                calc_text = calc_dt.strftime("%H:%M:%S")
                matched = ocr_text == calc_text
                if matched:
                    score += 1
                lines.append(
                    (
                        f"  frame {frame_idx + 1}: OCR={ocr_text or '(blank)'} "
                        f"calc={calc_text} {'OK' if matched else 'MISS'}",
                        "ok" if matched else "miss",
                    )
                )
            lines.append(("", "info"))
            if score > best_score:
                best_score = score
                best_offset = offset

        lines.append((f"Chosen offset: {best_offset:+d} frame(s)", "info"))
        return best_offset, lines

    def _show_verification_dialog(
        self,
        report: list[tuple[str, str]],
    ) -> bool:
        return show_verification_dialog(self, report)


@dataclass(frozen=True)
class OcrOffsetResult:
    video_start_dt: datetime
    offset_seconds: float
    frame_offset: int
    report: list[tuple[str, str]]


def parse_filename_datetime(video_path: str | Path) -> datetime | None:
    name = Path(video_path).name
    match = re.search(r"(\d{14})", name)
    if not match:
        return None
    return datetime.strptime(match.group(1), "%Y%m%d%H%M%S")


def show_verification_dialog(parent: QWidget, report: list[tuple[str, str]]) -> bool:
    dlg = QDialog(parent)
    dlg.setWindowTitle("Offset verification")
    dlg.resize(720, 520)

    browser = QTextBrowser()
    browser.setOpenExternalLinks(False)
    browser.setReadOnly(True)
    html_lines = []
    for text, status in report:
        if not text:
            html_lines.append("<div style='height:6px'></div>")
            continue
        if status == "ok":
            color = "#1f6f1f"
        elif status == "miss":
            color = "#7a1f1f"
        else:
            color = "#202020"
        safe = (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        html_lines.append(
            f"<div style='background:{color}; padding:4px 6px; "
            f"margin:2px 0; color:#ffffff; font-family:monospace;'>{safe}</div>"
        )
    browser.setHtml("".join(html_lines))

    buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    buttons.button(QDialogButtonBox.Ok).setText("Approve offset")
    buttons.button(QDialogButtonBox.Cancel).setText("Reject")
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)

    layout = QVBoxLayout()
    layout.addWidget(browser, 1)
    layout.addWidget(buttons)
    dlg.setLayout(layout)
    return dlg.exec() == QDialog.Accepted


class _Aborted(Exception):
    pass


def locate_date_change(read_date, frame_count: int, step: int):
    """Find the first frame whose date differs from frame 0's (Chris,
    2026-09-12: the camera can take a while to sync its date after the
    clip starts). `read_date(frame_idx)` returns a date or None. Coarse
    steps of `step` frames, then a frame-by-frame bisection between the
    last old reading and the first new one. Returns
    (change_frame, initial_date, new_date), or None when the date never
    changes in the clip."""
    if frame_count <= 0:
        return None
    step = max(1, int(step))
    initial = read_date(0)
    prev_frame = 0
    frame = step
    while frame < frame_count:
        current = read_date(frame)
        if current is not None and current != initial:
            lo, hi = prev_frame, frame
            new_date = current
            while hi - lo > 1:
                mid = (lo + hi) // 2
                mid_date = read_date(mid)
                if mid_date is not None and mid_date != initial:
                    hi, new_date = mid, mid_date
                else:
                    lo = mid
            return hi, initial, new_date
        prev_frame = frame
        frame += step
    return None


def find_second_boundaries(read_text, start_frame: int, end_frame: int, step: int) -> list[tuple[int, str]]:
    """Every frame in [start_frame, end_frame] where the burnt-in clock
    ticks to the next second, as (first frame of the new second, its
    text). `read_text(frame)` returns a valid HH:MM:SS or None. Reads
    every `step` frames, then bisects between the last old and the first
    new reading; an unreadable or odd reading inside the gap falls back to
    a frame-by-frame walk of the gap (Chris, 2026-09-12)."""
    step = max(1, int(step))
    start_frame, end_frame = int(start_frame), int(end_frame)
    frames = list(range(start_frame, end_frame + 1, step))
    if frames and frames[-1] != end_frame:
        frames.append(end_frame)
    found: list[tuple[int, str]] = []
    prev: tuple[int, str] | None = None
    for frame in frames:
        text = read_text(frame)
        if text is None:
            continue
        if prev is not None and text != prev[1]:
            prev_secs = _time_text_to_seconds(prev[1])
            secs = _time_text_to_seconds(text)
            if prev_secs is not None and secs is not None and secs == (prev_secs + 1) % 86400:
                lo, hi = prev[0], frame
                while hi - lo > 1:
                    mid = (lo + hi) // 2
                    mid_text = read_text(mid)
                    if mid_text == text:
                        hi = mid
                    elif mid_text == prev[1]:
                        lo = mid
                    else:
                        for walk in range(lo + 1, hi + 1):
                            if read_text(walk) == text:
                                hi = walk
                                break
                        break
                found.append((hi, text))
        prev = (frame, text)
    return found


def find_date_change_frame(cap, fps: float, frame_count: int, date_roi: Roi, *, should_abort=None, on_progress=None):
    """locate_date_change over a clip, reading the purple date box with
    Tesseract; one coarse read a second. None when aborted or unchanged."""
    step = max(1, int(round(fps * 1.0))) if fps > 0 else 25

    def read_date(frame_idx: int):
        if should_abort is not None and should_abort():
            raise _Aborted()
        if on_progress is not None:
            try:
                on_progress(frame_idx)
            except Exception:
                pass
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(frame_idx))
        ret, frame = cap.read()
        if not ret or frame is None:
            return None
        try:
            return parse_cctv_date(ocr_date_from_frame(frame, roi=date_roi))
        except Exception:
            return None

    try:
        return locate_date_change(read_date, frame_count, step)
    except _Aborted:
        return None


def analyze_video_offset(
    video_path: str | Path,
    *,
    settings_path: Path | None = None,
    settings_key: str | None = None,
    parent: QWidget | None = None,
    should_abort=None,
    on_stage=None,
    start_frame: int = 0,
) -> OcrOffsetResult | None:
    """`should_abort` (callable -> bool) is polled between frames so a
    worker-thread run can be interrupted quickly (e.g. at app shutdown);
    aborted runs return None. `on_stage` (callable(str)) is told when each
    analysis stage begins, for progress UI."""
    t_total = perf_counter()

    def _stage(label: str) -> float:
        if on_stage is not None:
            try:
                on_stage(label)
            except Exception:
                pass
        return perf_counter()

    _ensure_tesseract()
    base_dt = parse_filename_datetime(video_path)
    if base_dt is None:
        return None
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return None
    fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if fps <= 0 or frame_count <= 0:
        cap.release()
        return None

    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
    ret, frame = cap.read()
    if not ret or frame is None:
        cap.release()
        return None

    roi_settings = None
    if settings_path and settings_key:
        roi_settings = load_roi_settings(settings_path, settings_key)
    if roi_settings is None:
        roi_settings = RoiSettings(0.22, 0.06, 0.013, 0.0)
    roi = Roi.top_center_time(
        frame.shape[1],
        frame.shape[0],
        width_ratio=roi_settings.width_ratio,
        height_ratio=roi_settings.height_ratio,
        y_offset_ratio=roi_settings.y_offset_ratio,
        x_offset_ratio=roi_settings.x_offset_ratio,
    )
    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)

    def _aborted() -> bool:
        return should_abort is not None and should_abort()

    # The camera may sync its date some way into the clip (Chris,
    # 2026-09-12); when a date box is saved, find that frame first and
    # read the clock only from there, on the synced date.
    date_settings = None
    if settings_path and settings_key:
        date_settings = load_roi_settings(settings_path, settings_key, section="date_roi_by_key")
    if date_settings is not None:
        t_stage = _stage("checking the camera date")
        date_roi = Roi.top_center_time(
            frame.shape[1], frame.shape[0],
            width_ratio=date_settings.width_ratio, height_ratio=date_settings.height_ratio,
            y_offset_ratio=date_settings.y_offset_ratio, x_offset_ratio=date_settings.x_offset_ratio,
        )
        change = find_date_change_frame(cap, fps, frame_count, date_roi, should_abort=should_abort)
        if change is not None:
            change_frame, initial_date, new_date = change
            start_frame = max(start_frame, change_frame)
            base_dt = base_dt.replace(year=new_date.year, month=new_date.month, day=new_date.day)
            print(f"[ocr] camera date {initial_date} -> {new_date} at frame {change_frame} ({change_frame / fps:.1f}s); clock read from there", flush=True)
        print(f"[ocr] date check: {(perf_counter() - t_stage) * 1000:.0f}ms", flush=True)
        if _aborted():
            cap.release()
            return None
    start_frame = max(0, min(int(start_frame), max(0, frame_count - 1)))

    fast_seconds = OCR_SYNC_FAST_SECONDS
    fallback_seconds = OCR_SYNC_FALLBACK_SECONDS
    t_stage = _stage(f"scanning clock (coarse, first {fast_seconds}s)")
    samples = _find_second_boundary_samples_for_cap(
        cap,
        fps,
        frame_count,
        seconds=fast_seconds,
        base_dt=base_dt,
        roi=roi,
        parent=parent,
        progress_label=f"Scanning first {fast_seconds}s (coarse)...",
        should_abort=should_abort,
        start_frame=start_frame,
    )
    best_start = _estimate_start_from_samples(samples, base_dt)
    print(
        f"[ocr] coarse scan {fast_seconds}s: {(perf_counter() - t_stage) * 1000:.0f}ms "
        f"samples={len(samples)} found={best_start is not None}",
        flush=True,
    )
    if best_start is None and not _aborted():
        t_stage = _stage(f"reading clock every frame (first {fast_seconds}s)")
        samples = _collect_ocr_samples_for_cap(
            cap,
            fps,
            frame_count,
            seconds=fast_seconds,
            base_dt=base_dt,
            roi=roi,
            parent=parent,
            progress_label=f"Analyzing first {fast_seconds}s...",
            should_abort=should_abort,
            start_frame=start_frame,
        )
        best_start = _estimate_start_from_samples(samples, base_dt)
        print(
            f"[ocr] dense scan {fast_seconds}s: {(perf_counter() - t_stage) * 1000:.0f}ms "
            f"samples={len(samples)} found={best_start is not None}",
            flush=True,
        )
    if best_start is None and fallback_seconds > fast_seconds and not _aborted():
        t_stage = _stage(f"scanning clock (coarse, first {fallback_seconds}s)")
        samples = _find_second_boundary_samples_for_cap(
            cap,
            fps,
            frame_count,
            seconds=fallback_seconds,
            base_dt=base_dt,
            roi=roi,
            parent=parent,
            progress_label=f"Scanning first {fallback_seconds}s (coarse)...",
            should_abort=should_abort,
            start_frame=start_frame,
        )
        best_start = _estimate_start_from_samples(samples, base_dt)
        print(
            f"[ocr] coarse scan {fallback_seconds}s: {(perf_counter() - t_stage) * 1000:.0f}ms "
            f"samples={len(samples)} found={best_start is not None}",
            flush=True,
        )
    if best_start is None and fallback_seconds > fast_seconds and not _aborted():
        t_stage = _stage(f"reading clock every frame (first {fallback_seconds}s)")
        samples = _collect_ocr_samples_for_cap(
            cap,
            fps,
            frame_count,
            seconds=fallback_seconds,
            base_dt=base_dt,
            roi=roi,
            parent=parent,
            progress_label=f"Analyzing first {fallback_seconds}s...",
            should_abort=should_abort,
            start_frame=start_frame,
        )
        best_start = _estimate_start_from_samples(samples, base_dt)
        print(
            f"[ocr] dense scan {fallback_seconds}s: {(perf_counter() - t_stage) * 1000:.0f}ms "
            f"samples={len(samples)} found={best_start is not None}",
            flush=True,
        )
    if best_start is None or _aborted():
        cap.release()
        print(
            f"[ocr] analyze total: {(perf_counter() - t_total) * 1000:.0f}ms (no result)",
            flush=True,
        )
        return None

    offset_seconds = (best_start - base_dt).total_seconds()
    t_stage = _stage("verifying frame offset")
    frame_offset, report = _verify_frame_offset_for_cap(
        cap,
        fps,
        frame_count,
        best_start,
        roi, start_frame=start_frame)
    print(
        f"[ocr] verify: {(perf_counter() - t_stage) * 1000:.0f}ms",
        flush=True,
    )
    print(
        f"[ocr] analyze total: {(perf_counter() - t_total) * 1000:.0f}ms "
        f"(offset={offset_seconds:+.3f}s frames={frame_offset})",
        flush=True,
    )
    cap.release()
    result = OcrOffsetResult(
        video_start_dt=best_start,
        offset_seconds=offset_seconds,
        frame_offset=frame_offset,
        report=report,
    )
    return result


def _collect_ocr_samples_for_cap(
    cap: cv2.VideoCapture,
    fps: float,
    frame_count: int,
    *,
    seconds: int,
    base_dt: datetime,
    roi: Roi,
    parent: QWidget | None,
    progress_label: str,
    should_abort=None,

    start_frame: int = 0,) -> list[tuple[int, float, datetime, str]]:
    if frame_count <= 0 or fps <= 0:
        return []
    max_frame_idx = int(math.ceil(seconds * fps))
    if max_frame_idx < 0:
        return []
    max_frame_idx = min(frame_count - 1, max_frame_idx)
    max_frames = max_frame_idx + 1
    if max_frames <= 0:
        return []
    samples: list[tuple[int, float, datetime, str]] = []
    progress = None
    if parent is not None:
        progress = QProgressDialog(progress_label, None, 0, max_frames, parent)
        progress.setWindowTitle("OCR Analysis")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
    start_frame = max(0, int(start_frame))
    for frame_idx in range(start_frame, min(frame_count, start_frame + max_frames)):
        if should_abort is not None and should_abort():
            break
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(frame_idx))
        ret, frame = cap.read()
        if not ret or frame is None:
            continue
        try:
            raw_text = ocr_time_from_frame(frame, roi=roi)
        except Exception:
            continue
        text = _normalize_ocr_text(raw_text)
        if not _is_valid_time_text(text):
            continue
        ocr_dt = _combine_date_and_time(base_dt, text)
        video_t = frame_idx / fps
        samples.append((frame_idx, video_t, ocr_dt, text))
        if progress:
            progress.setValue(frame_idx + 1)
            QApplication.processEvents()
    if progress:
        progress.setValue(max_frames)
        progress.close()
    return samples


def _ocr_sample_for_frame(
    cap: cv2.VideoCapture,
    frame_idx: int,
    fps: float,
    base_dt: datetime,
    roi: Roi,
) -> tuple[int, float, datetime, str] | None:
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(frame_idx))
    ret, frame = cap.read()
    if not ret or frame is None:
        return None
    try:
        raw_text = ocr_time_from_frame(frame, roi=roi)
    except Exception:
        return None
    text = _normalize_ocr_text(raw_text)
    if not _is_valid_time_text(text):
        return None
    ocr_dt = _combine_date_and_time(base_dt, text)
    video_t = frame_idx / fps
    return frame_idx, video_t, ocr_dt, text


def _find_second_boundary_samples_for_cap(
    cap: cv2.VideoCapture,
    fps: float,
    frame_count: int,
    *,
    seconds: int,
    base_dt: datetime,
    roi: Roi,
    parent: QWidget | None,
    progress_label: str,
    should_abort=None,

    start_frame: int = 0,) -> list[tuple[int, float, datetime, str]]:
    if frame_count <= 0 or fps <= 0:
        return []
    max_frame_idx = int(math.ceil(seconds * fps))
    if max_frame_idx < 0:
        return []
    max_frame_idx = min(frame_count - 1, max_frame_idx)
    if max_frame_idx <= 0:
        return []

    step = max(1, int(round(fps * OCR_SYNC_COARSE_STEP_SECONDS)))
    start_frame = max(0, int(start_frame))
    last = min(frame_count - 1, start_frame + max_frame_idx)
    if last <= start_frame:
        return []
    frame_indices = list(range(start_frame, last + 1, step))
    if frame_indices[-1] != last:
        frame_indices.append(last)

    progress = None
    if parent is not None:
        progress = QProgressDialog(progress_label, None, 0, len(frame_indices), parent)
        progress.setWindowTitle("OCR Analysis")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)

    prev: tuple[int, float, datetime, str] | None = None
    for idx, frame_idx in enumerate(frame_indices):
        if should_abort is not None and should_abort():
            break
        sample = _ocr_sample_for_frame(cap, frame_idx, fps, base_dt, roi)
        if progress:
            progress.setValue(idx + 1)
            QApplication.processEvents()
        if sample is None:
            continue
        if prev is not None:
            prev_text = prev[3]
            curr_text = sample[3]
            if prev_text != curr_text:
                prev_secs = _time_text_to_seconds(prev_text)
                curr_secs = _time_text_to_seconds(curr_text)
                if prev_secs is not None and curr_secs is not None:
                    if curr_secs == (prev_secs + 1) % 86400:
                        start = min(prev[0], sample[0])
                        end = max(prev[0], sample[0])
                        boundary_sample = None
                        for frame_scan in range(start, end + 1):
                            if should_abort is not None and should_abort():
                                break
                            scanned = _ocr_sample_for_frame(
                                cap,
                                frame_scan,
                                fps,
                                base_dt,
                                roi,
                            )
                            if scanned and scanned[3] == curr_text:
                                boundary_sample = scanned
                                break
                        if boundary_sample is None:
                            boundary_sample = sample
                        if progress:
                            progress.setValue(len(frame_indices))
                            progress.close()
                        return [prev, boundary_sample]
        prev = sample

    if progress:
        progress.setValue(len(frame_indices))
        progress.close()
    return []


def _estimate_start_from_samples(
    samples: list[tuple[int, float, datetime, str]],
    base_dt: datetime,
) -> datetime | None:
    transition = _estimate_start_from_transitions(samples)
    if transition is not None:
        return transition[0]
    if not samples:
        return None  # nothing read: the caller moves on to the next stage (2026-09-12)
    inferred = []
    for frame_idx, video_t, ocr_dt, ocr_text in samples:
        inferred.append((ocr_dt - timedelta(seconds=video_t), ocr_text))
    inferred.sort(key=lambda item: item[0])
    median_start = inferred[len(inferred) // 2][0]
    inliers = [
        item for item in inferred
        if abs((item[0] - median_start).total_seconds()) <= 2.0
    ]
    if not inliers:
        return None
    return inliers[len(inliers) // 2][0]


def _time_text_to_seconds(time_text: str) -> int | None:
    try:
        hour, minute, second = (int(part) for part in time_text.split(":"))
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        return None
    return hour * 3600 + minute * 60 + second


def _estimate_start_from_transitions(
    samples: list[tuple[int, float, datetime, str]],
) -> tuple[datetime, datetime, list[tuple[datetime, str]]] | None:
    if len(samples) < 2:
        return None
    transitions = []
    for idx in range(1, len(samples)):
        _prev_frame, prev_t, prev_dt, prev_text = samples[idx - 1]
        _curr_frame, curr_t, curr_dt, curr_text = samples[idx]
        if prev_text == curr_text:
            continue
        prev_secs = _time_text_to_seconds(prev_text)
        curr_secs = _time_text_to_seconds(curr_text)
        if prev_secs is None or curr_secs is None:
            continue
        if curr_secs != (prev_secs + 1) % 86400:
            continue
        boundary_video_t = curr_t
        boundary_ocr_dt = curr_dt
        inferred_start = boundary_ocr_dt - timedelta(seconds=boundary_video_t)
        transitions.append((inferred_start, curr_text))
    if not transitions:
        return None
    transitions.sort(key=lambda item: item[0])
    median_start = transitions[len(transitions) // 2][0]
    inliers = [
        item for item in transitions
        if abs((item[0] - median_start).total_seconds()) <= 1.0
    ]
    if not inliers:
        return None
    outliers = [item for item in transitions if item not in inliers]
    best_start = inliers[len(inliers) // 2][0]
    return best_start, median_start, outliers


def _verify_frame_offset_for_cap(
    cap: cv2.VideoCapture,
    fps: float,
    frame_count: int,
    estimated_start: datetime,
    roi: Roi,

    start_frame: int = 0,) -> tuple[int, list[tuple[str, str]]]:
    if fps <= 0 or frame_count <= 0:
        return 0, [("Unable to verify frame offset (no video/fps).", "info")]
    mid_frame = min(frame_count - 1, max(frame_count // 2, int(start_frame) + int(fps * 2)))
    mid_dt = estimated_start + timedelta(seconds=mid_frame / fps)
    target_second = mid_dt.replace(microsecond=0)
    target_seconds = (target_second - estimated_start).total_seconds()
    candidates = [-2, -1, 0, 1, 2]
    best_offset = 0
    best_score = -1
    lines: list[tuple[str, str]] = [
        (
            f"Verifying around mid-frame {mid_frame} "
            f"({target_second.strftime('%H:%M:%S')})",
            "info",
        ),
    ]
    for offset in candidates:
        boundary_frame = int(math.ceil(target_seconds * fps - offset))
        frames = [
            max(0, boundary_frame - 1),
            max(0, min(frame_count - 1, boundary_frame)),
            max(0, min(frame_count - 1, boundary_frame + 1)),
        ]
        score = 0
        lines.append((f"Offset {offset:+d} frames:", "info"))
        for frame_idx in frames:
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(frame_idx))
            ret, frame = cap.read()
            if not ret or frame is None:
                lines.append((f"  frame {frame_idx + 1}: read failed", "miss"))
                continue
            try:
                raw_text = ocr_time_from_frame(frame, roi=roi)
            except Exception as exc:
                lines.append((f"  frame {frame_idx + 1}: OCR error {exc}", "miss"))
                continue
            ocr_text = _normalize_ocr_text(raw_text)
            calc_dt = estimated_start + timedelta(seconds=(frame_idx + offset) / fps)
            calc_text = calc_dt.strftime("%H:%M:%S")
            matched = ocr_text == calc_text
            if matched:
                score += 1
            lines.append(
                (
                    f"  frame {frame_idx + 1}: OCR={ocr_text or '(blank)'} "
                    f"calc={calc_text} {'OK' if matched else 'MISS'}",
                    "ok" if matched else "miss",
                )
            )
        lines.append(("", "info"))
        if score > best_score:
            best_score = score
            best_offset = offset
    lines.append((f"Chosen offset: {best_offset:+d} frame(s)", "info"))
    return best_offset, lines


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="CCTV Time OCR")
    parser.add_argument("--video", help="Path to video file")
    parser.add_argument("--gui", action="store_true", help="Open the GUI player")
    args = parser.parse_args()

    if args.gui or not args.video:
        app = QApplication([])
        win = OcrVideoPlayer()
        win.resize(900, 600)
        win.show()
        if args.video:
            win.open_video(args.video)
        raise SystemExit(app.exec())

    frame, _, _ = _read_frame(args.video, frame_index=0, time_seconds=None)
    text = ocr_time_from_frame(frame)
    print(text)
