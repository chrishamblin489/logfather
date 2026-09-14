"""The Software window: per-system timeline of package versions with the
git commit in brackets (Chris, 2026-09-05). One row block per system,
one lane per package; a bar per dated span, hover for the details.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from logfather.data.software_history import PACKAGES, SystemSoftware, commit_owners, fetch_software_history
from logfather.ui import theme
from logfather.ui.gear_menu import build_gear_button
from logfather.ui.qt_worker import JobSlot

_RANGES = ((30, "30 days"), (90, "90 days"), (182, "6 months"), (365, "1 year"))

LEFT_COL = 190
LANE_H = 20
HEADER_H = 26
BLOCK_GAP = 14
AXIS_H = 30


def _package_colour(index: int) -> QColor:
    hue = (index * 137.508) % 360.0
    colour = QColor()
    colour.setHsvF(hue / 360.0, 0.34, 0.80 if index % 2 == 0 else 0.68)
    return colour


class _TimelineWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self._systems: list[SystemSoftware] = []
        self._t0: datetime = datetime.now(timezone.utc) - timedelta(days=182)
        self._t1: datetime = datetime.now(timezone.utc)
        self._bars: list[tuple[QRectF, str]] = []
        self._hover: int | None = None
        self._owners: dict = {}
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_data(self, systems: list[SystemSoftware], t0: datetime, t1: datetime) -> None:
        self._systems = list(systems)
        self._owners = commit_owners(self._systems)
        self._t0, self._t1 = t0, t1
        blocks = sum(HEADER_H + (LANE_H * len(PACKAGES) if s.generation == "Argus 2" and s.spans else LANE_H) + BLOCK_GAP for s in systems)
        self.setMinimumHeight(AXIS_H + blocks + 20)
        self.updateGeometry()
        self.update()

    def _x(self, when: datetime, plot_left: float, plot_w: float) -> float:
        total = max(1.0, (self._t1 - self._t0).total_seconds())
        frac = (when - self._t0).total_seconds() / total
        return plot_left + max(0.0, min(1.0, frac)) * plot_w

    def mouseMoveEvent(self, event):
        pos = event.position()
        hit = None
        for i, (rect, _tip) in enumerate(self._bars):
            if rect.contains(pos):
                hit = i
                break
        if hit != self._hover:
            self._hover = hit
            self.update()
        if hit is None:
            QToolTip.hideText()
        else:
            rect, tip = self._bars[hit]
            QToolTip.showText(event.globalPosition().toPoint(), tip, self, rect.toRect())
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self._hover = None
        QToolTip.hideText()
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect()
        painter.fillRect(rect, QColor(theme.BG_DEEP))
        self._bars = []
        plot_left = rect.left() + LEFT_COL
        plot_w = max(50, rect.width() - LEFT_COL - 16)
        small = QFont(self.font().family(), max(8, self.font().pointSize() - 2))
        bold = QFont(self.font())
        bold.setBold(True)
        fm_small = QFontMetrics(small)

        # Month axis
        painter.setFont(small)
        cursor = datetime(self._t0.year, self._t0.month, 1, tzinfo=timezone.utc)
        while cursor <= self._t1:
            x = self._x(cursor, plot_left, plot_w)
            painter.setPen(QPen(QColor(theme.BORDER)))
            painter.drawLine(int(x), AXIS_H - 6, int(x), rect.bottom())
            painter.setPen(QColor(theme.TEXT_MUTED))
            painter.drawText(QRectF(x + 3, 4, 90, AXIS_H - 8), Qt.AlignLeft | Qt.AlignVCenter, cursor.strftime("%b %Y"))
            cursor = datetime(cursor.year + (cursor.month == 12), (cursor.month % 12) + 1, 1, tzinfo=timezone.utc)
        painter.setPen(QPen(QColor(theme.BORDER_LIGHT)))
        painter.drawLine(plot_left, AXIS_H, rect.right(), AXIS_H)

        y = AXIS_H + 6
        for system in self._systems:
            painter.setFont(bold)
            painter.setPen(QColor(theme.TEXT_BRIGHT))
            painter.drawText(QRectF(8, y, LEFT_COL - 12, HEADER_H), Qt.AlignLeft | Qt.AlignVCenter, system.name)
            painter.setFont(small)
            painter.setPen(QColor(theme.TEXT_MUTED))
            seen = ""
            if system.first_seen and system.last_seen:
                seen = f"{system.generation} · logging {system.first_seen.astimezone():%d %b} – {system.last_seen.astimezone():%d %b}"
            painter.drawText(QRectF(plot_left + 4, y, plot_w - 8, HEADER_H), Qt.AlignLeft | Qt.AlignVCenter, seen)
            y += HEADER_H
            if not (system.generation == "Argus 2" and system.spans):
                painter.setPen(QColor(theme.TEXT_DIM))
                painter.drawText(QRectF(plot_left + 4, y, plot_w - 8, LANE_H), Qt.AlignLeft | Qt.AlignVCenter, system.note or "no data")
                y += LANE_H + BLOCK_GAP
                continue
            for pi, pkg in enumerate(PACKAGES):
                lane_top = y + pi * LANE_H
                painter.setPen(QColor(theme.TEXT_MUTED))
                painter.setFont(small)
                painter.drawText(QRectF(18, lane_top, LEFT_COL - 24, LANE_H), Qt.AlignLeft | Qt.AlignVCenter, pkg)
                painter.setPen(QPen(QColor(theme.BORDER)))
                painter.drawLine(plot_left, int(lane_top + LANE_H - 1), rect.right(), int(lane_top + LANE_H - 1))
                colour = _package_colour(pi)
                for span in [s for s in system.spans if s.package == pkg]:
                    x1 = self._x(span.start, plot_left, plot_w)
                    x2 = self._x(span.end, plot_left, plot_w)
                    bar = QRectF(x1, lane_top + 2, max(3.0, x2 - x1), LANE_H - 5)
                    index = len(self._bars)
                    owners = self._owners.get((pkg, span.commit), set()) if span.commit else set()
                    unique = bool(span.commit) and owners == {system.name}
                    label = f"{span.version or '?'}" + (f" ({span.commit})" if span.commit else "")
                    tip = (
                        f"<b>{system.name} · {pkg}</b><br>version {span.version or 'unknown'}"
                        + (f"<br>commit {span.commit}" if span.commit else "")
                        + (f"<br>branch {span.branch}" if span.branch else "")
                        + f"<br>{span.start.astimezone():%d %b %Y} – {span.end.astimezone():%d %b %Y}"
                        + (f"<br>{span.node_starts:,} node starts" if span.node_starts else "")
                        + ("<br><b>Only this system runs this commit</b>" if unique else (f"<br>also on {', '.join(sorted(owners - {system.name}))}" if len(owners) > 1 else ""))
                    )
                    self._bars.append((bar, tip))
                    painter.setBrush(QBrush(colour))
                    if index == self._hover:
                        pen = QPen(QColor(theme.TEXT_BRIGHT))
                        pen.setWidth(2)
                        painter.setPen(pen)
                    elif unique:
                        # Code nobody else runs: red outline (Chris).
                        pen = QPen(QColor(theme.DANGER))
                        pen.setWidth(2)
                        painter.setPen(pen)
                    else:
                        painter.setPen(QPen(QColor(theme.BG_DEEP)))
                    painter.drawRect(bar)
                    if fm_small.horizontalAdvance(label) + 8 <= bar.width():
                        painter.setPen(QColor(theme.BG))
                        painter.drawText(bar.adjusted(4, 0, -4, 0), Qt.AlignLeft | Qt.AlignVCenter, label)
            y += LANE_H * len(PACKAGES) + BLOCK_GAP
        painter.end()


class SoftwareWindow(QDialog):
    def __init__(self, settings_provider: Callable, parent=None, gear_host=None):
        super().__init__(parent)
        self._gear_host = gear_host
        self.setWindowTitle("Software — versions and commits per system")
        self.setWindowFlags(Qt.Window | Qt.WindowTitleHint | Qt.WindowMinMaxButtonsHint | Qt.WindowCloseButtonHint)
        self.setSizeGripEnabled(True)
        self.setMinimumSize(820, 520)
        self.resize(1320, 820)
        self._settings_provider = settings_provider
        self._slot = JobSlot(self)
        self._days = 182
        self._started = False

        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        intro = QLabel(
            "Each PikPak system as a block, one lane per software package, a bar for every "
            "dated span of version (git commit). A red outline marks a commit that no other "
            "system runs. Argus 2 systems log this on every node start; Argus 1 systems log "
            "no version or commit fields."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {theme.TEXT_BRIGHT};")
        layout.addWidget(intro)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Range:"))
        self._range_group = QButtonGroup(self)
        for days, label in _RANGES:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setChecked(days == self._days)
            btn.clicked.connect(lambda _checked=False, d=days: self._set_days(d))
            self._range_group.addButton(btn)
            controls.addWidget(btn)
        controls.addSpacing(16)
        legend = QLabel("&nbsp;&nbsp;".join(
            f'<span style="background-color:{_package_colour(i).name()};">&nbsp;&nbsp;&nbsp;</span>&nbsp;{pkg}' for i, pkg in enumerate(PACKAGES)
        ))
        controls.addWidget(legend)
        controls.addStretch(1)
        self._status = QLabel("")
        self._status.setStyleSheet(theme.MUTED_LABEL)
        controls.addWidget(self._status)
        self._progress = QProgressBar()
        self._progress.setFixedWidth(140)
        self._progress.setFixedHeight(8)
        self._progress.setTextVisible(False)
        self._progress.setRange(0, 0)
        self._progress.hide()
        controls.addWidget(self._progress)
        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.clicked.connect(self.start)
        controls.addWidget(self._refresh_btn)
        if self._gear_host is not None:
            controls.addSpacing(8)
            controls.addWidget(build_gear_button(self, self._gear_host))
        layout.addLayout(controls)

        self._timeline = _TimelineWidget()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._timeline)
        scroll.setStyleSheet(f"QScrollArea {{ border: 1px solid {theme.BORDER}; background: {theme.BG_DEEP}; }}")
        layout.addWidget(scroll, 1)

    def start_if_needed(self) -> None:
        if not self._started:
            self.start()

    def _set_days(self, days: int) -> None:
        if days != self._days:
            self._days = days
            self.start()

    def start(self) -> None:
        self._started = True
        settings = self._settings_provider()
        days = self._days
        self._progress.show()
        self._status.setText("Querying Elastic...")
        self._slot.start(
            lambda job: fetch_software_history(settings, days, progress=job.emit_progress),
            on_result=self._on_result,
            on_error=self._on_error,
            on_progress=lambda m: self._status.setText(str(m or "")),
            on_finished=lambda: self._progress.hide(),
        )

    def shutdown(self) -> None:
        self._slot.shutdown()

    def closeEvent(self, event):
        event.ignore()
        self.hide()

    def _on_error(self, message: str) -> None:
        self._status.setText(f"Failed: {message}")

    def _on_result(self, systems: list[SystemSoftware]) -> None:
        now = datetime.now(timezone.utc)
        self._timeline.set_data(systems, now - timedelta(days=self._days), now)
        argus2 = sum(1 for s in systems if s.generation == "Argus 2" and s.spans)
        self._status.setText(f"{len(systems)} systems · {argus2} with version data · cached locally, refreshed for elapsed days only")
