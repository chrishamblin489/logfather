"""One family of Grafana readings drawn as a strip under every system row
of the Overview: temperatures, currents (Chris, 2026-09-07/08). Each
channel owns its Data-box button and colour key, its fetch, its strip
height (dragged at the strip's bottom line) and the hover dots and box.

The owner is the OverviewWidget: it supplies the scene, the view, the
settings, the chosen span, and calls back for redraws.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable

from PySide6.QtCore import QEvent, QObject, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QAction, QBrush, QColor, QFont, QFontMetrics, QIcon, QPainterPath, QPen, QTransform
from PySide6.QtWidgets import QGraphicsItem, QGraphicsRectItem
from PySide6.QtWidgets import QGroupBox, QHBoxLayout, QLabel, QMenu, QSizePolicy, QToolButton, QVBoxLayout, QWidget

from logfather.core.telemetry import (
    ADDITIONAL_CHANNELS,
    CURRENT_CHOICES,
    CURRENT_COLOURS,
    PICKS_CHOICES,
    PICKS_COLOURS,
    PRESSURE_CHOICES,
    PRESSURE_COLOURS,
    TEMPERATURE_CHOICES,
    TEMPERATURE_COLOURS,
    window_stats,
)
from logfather.data.telemetry_loader import fetch_fleet_signals
from logfather.data.ui_state_store import load_ui_state, update_ui_state
from logfather.ui import theme
from logfather.ui.icons import current_icon, gauge_icon, pick_icon, plus_box_icon, thermometer_icon
from logfather.ui.qt_worker import JobSlot

# Compact boxes (Chris, 2026-09-11): small text and tight rows so the
# Errors and Data boxes leave room for the log tabs above them. The
# font-size on the box cascades to every widget inside it; rich-text key
# labels set it themselves.
COMPACT_FONT_PX = 11
COMPACT_BOX_STYLE = (
    f"QGroupBox {{ font-weight: normal; font-size: {COMPACT_FONT_PX}px; margin-top: 9px;"
    f" padding: 3px 6px 2px 6px; border: 1px solid {theme.BORDER}; border-radius: 6px; }}"
    f"QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {theme.TEXT_MUTED}; }}"
)
KEY_LABEL_STYLE = f"{theme.MUTED_LABEL} font-size: {COMPACT_FONT_PX}px;"


# The same colour as the table behind the labels (the app background the
# scenes paint), so the pill is invisible until a chart scrolls under it
# (Chris, 2026-09-11).
LABEL_BACKDROP = QColor(theme.BG)


class CollapsibleGroupBox(QGroupBox):
    """A group box whose contents live in `body`, so they can fold away
    behind a small arrow after the title (Chris, 2026-09-11: ^ hides the
    values, v shows them again). The arrow appears once `set_collapsible`
    names the ui_state key that remembers the choice."""

    COLLAPSED_KEY = "replay_boxes_collapsed"
    collapsed_changed = Signal(bool)

    def __init__(self, title: str, parent=None):
        super().__init__(title, parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.body = QWidget(self)
        outer.addWidget(self.body)
        self._arrow: QToolButton | None = None
        self._store_key: str | None = None
        self._collapsed = False

    def set_collapsible(self, store_key: str) -> None:
        self._store_key = store_key
        self._arrow = QToolButton(self)
        self._arrow.setAutoRaise(True)
        self._arrow.setFixedSize(16, 14)
        self._arrow.setCursor(Qt.PointingHandCursor)
        # A glyph, not the style's arrow: the app's stylesheet leaves the
        # style arrow invisible (Chris, 2026-09-11).
        self._arrow.setStyleSheet(
            f"QToolButton {{ border: none; background: transparent; padding: 0; color: {theme.TEXT_MUTED}; font-size: 10px; }}"
            f"QToolButton:hover {{ color: {theme.TEXT_BRIGHT}; }}"
        )
        self._arrow.clicked.connect(lambda: self.set_collapsed(not self._collapsed))
        stored = load_ui_state().get(self.COLLAPSED_KEY)
        collapsed = bool(stored.get(store_key)) if isinstance(stored, dict) else False
        self.set_collapsed(collapsed, remember=False)
        self._place_arrow()

    def set_collapsed(self, collapsed: bool, remember: bool = True) -> None:
        self._collapsed = bool(collapsed)
        self.body.setVisible(not self._collapsed)
        if self._arrow is not None:
            self._arrow.setText("▼" if self._collapsed else "▲")
            self._arrow.setToolTip("Show the values" if self._collapsed else "Hide the values")
        if remember and self._store_key:
            stored = load_ui_state().get(self.COLLAPSED_KEY)
            stored = dict(stored) if isinstance(stored, dict) else {}
            stored[self._store_key] = self._collapsed
            update_ui_state({self.COLLAPSED_KEY: stored})
        self.updateGeometry()
        self.collapsed_changed.emit(self._collapsed)

    def is_collapsed(self) -> bool:
        return self._collapsed

    def _place_arrow(self) -> None:
        if self._arrow is None:
            return
        # The title sits at left 10 px with 4 px padding (COMPACT_BOX_STYLE)
        # in the compact font; the arrow goes just after the text, on the
        # title line.
        font = QFont(self.font())
        font.setPixelSize(COMPACT_FONT_PX)
        x = 10 + 4 + QFontMetrics(font).horizontalAdvance(self.title()) + 4
        self._arrow.move(x, 0)
        self._arrow.raise_()
        # A folded box shrinks to its title; keep room for the arrow
        # (Chris, 2026-09-11: the show arrow vanished when Data was folded).
        self.setMinimumWidth(x + self._arrow.width() + 12)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place_arrow()

    def showEvent(self, event):
        super().showEvent(event)
        self._place_arrow()


def add_label_backdrop(item, colour: QColor = LABEL_BACKDROP, pad: float = 1.0) -> QGraphicsRectItem | None:
    """A dark pill behind a scene text label so it stays readable over
    whatever it scrolls across (Chris, 2026-09-11: the row headers sat
    directly on the chart). A child of the label, so it moves with it."""
    if not item.toPlainText().strip():
        return None
    rect = item.boundingRect().adjusted(2 - pad, 1, pad - 2, -1)
    bg = QGraphicsRectItem(rect, item)
    bg.setBrush(QBrush(colour))
    bg.setPen(QPen(Qt.NoPen))
    bg.setFlag(QGraphicsItem.ItemStacksBehindParent, True)
    bg.setAcceptedMouseButtons(Qt.NoButton)
    return bg


def key_swatch(colour: str) -> str:
    """A short colour block for a key label: the span's small font keeps
    the block shorter than the text beside it (Chris, 2026-09-11)."""
    return f'<span style="background-color:{colour}; font-size:6px;">&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;</span>'

STRIP_MIN, STRIP_MAX = 16, 240
DEFAULT_STRIP_HEIGHT = 44
GUIDE_COLOUR = "#ff8a65"


class SignalChannel(QObject):
    def __init__(
        self,
        owner,
        *,
        name: str,
        title: str,
        icon: QIcon,
        tooltip: str,
        choices: tuple[tuple[str, str], ...],
        colours: dict[str, str],
        unit: str,
        axis_unit: str,
        decimals: int,
        separator_before: str,
        ui_keys: str,
        ui_strip: str,
        default_strip_h: int,
        short: dict[str, str],
        loading_text: str,
        empty_text: str,
        axis_min: float | None = None,
        axis_title: str = "",
        axis_max: float | None = None,
        default_keys: list[str] | None = None,
    ):
        super().__init__(owner)
        self.owner = owner
        self.name = name
        self.title = title
        self.choices = choices
        self.colours = colours
        self.unit = unit
        self.axis_unit = axis_unit
        self.decimals = decimals
        self.ui_keys = ui_keys
        self.ui_strip = ui_strip
        self.short = short
        self.loading_text = loading_text
        self.empty_text = empty_text
        # A fixed floor for the strip's scale (pressure reads from 0 bar,
        # Chris, 2026-09-08); None scales to the readings in the window.
        self.axis_min = axis_min
        # A fixed ceiling too (memory is always 0-100 %, Chris, 2026-09-08).
        self.axis_max = axis_max
        # Written at the left of every strip (Chris, 2026-09-08).
        self.axis_title = axis_title or title
        state = load_ui_state()
        valid = [k for k, _label in choices]
        stored = state.get(ui_keys)
        if stored is None and default_keys:
            stored = list(default_keys)  # on by default until the user says otherwise
        self.keys: list[str] = [k for k in (stored if isinstance(stored, list) else []) if k in valid]
        try:
            self.strip_h = int(min(STRIP_MAX, max(STRIP_MIN, int(state.get(ui_strip)))))
        except (TypeError, ValueError):
            self.strip_h = default_strip_h
        self.data: dict[str, dict[str, object]] = {}
        self.window: tuple[datetime, datetime] | None = None
        self.keys_loaded: tuple[str, ...] = ()
        self.fetched_local: datetime | None = None
        self.slot = JobSlot(owner)
        self.edge_bands: list[tuple[float, float, float]] = []
        self.resize: tuple | None = None
        self.guide_item = None
        self.guide_label = None
        self.edge_hover = False
        self.hover_rows: list[tuple] = []
        self.hover_items: list = []
        # (item, kind, y) for the strip title and axis labels, so a scrolling
        # owner (the Replay timeline) can keep them at the viewport's left.
        self.label_items: list[tuple] = []
        # (robot, key) -> QPainterPath in (seconds since the epoch, value)
        # space, built once per data load; each redraw only places it
        # through a transform (the per-second live redraw was rebuilding
        # every segment in Python and froze the window, 2026-09-08).
        self._paths: dict[tuple[str, str], QPainterPath] = {}

        self.button = QToolButton()
        self.button.setIcon(icon)
        self.button.setIconSize(QSize(18, 18))
        self.button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.button.setPopupMode(QToolButton.InstantPopup)
        self.button.setToolTip(tooltip)
        self.button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        menu = QMenu(self.button)
        # Show all / Hide all first, then a rule, then the readings (Chris,
        # 2026-09-08).
        menu.addAction("Show all", lambda: self.set_all(True))
        menu.addAction("Hide all", lambda: self.set_all(False))
        menu.addSeparator()
        self.actions: dict[str, QAction] = {}
        for key, label in choices:
            if key == separator_before:
                menu.addSeparator()
            action = QAction(label, owner)
            action.setCheckable(True)
            action.setChecked(key in self.keys)
            action.toggled.connect(lambda _checked=False: self.on_changed())
            menu.addAction(action)
            self.actions[key] = action
        self.button.setMenu(menu)
        self.key_label = QLabel("")
        self.key_label.setTextFormat(Qt.RichText)
        self.key_label.setStyleSheet(KEY_LABEL_STYLE)
        self.refresh_label()

    # ---- selection ----------------------------------------------------------
    @property
    def active(self) -> bool:
        return bool(self.keys)

    def refresh_label(self) -> None:
        n = len(self.keys)
        self.button.setText(self.title if not n else f"{self.title} ({n})")
        bits = [
            f'{key_swatch(self.colours[key])}&nbsp;{label}'
            for key, label in self.choices if key in self.keys
        ]
        self.key_label.setText("&nbsp;&nbsp;".join(bits))
        # Only a label that sits in a layout is shown: a parentless QLabel
        # made visible becomes its own top-level window (the stray
        # "python" windows Chris saw, 2026-09-08). The Additional data
        # channels keep their own button and key unplaced.
        if self.key_label.parent() is not None:
            self.key_label.setVisible(bool(bits))
        else:
            self.key_label.hide()

    def set_all(self, on: bool) -> None:
        for action in self.actions.values():
            action.blockSignals(True)
            action.setChecked(on)
            action.blockSignals(False)
        self.on_changed()

    def on_changed(self) -> None:
        self.keys = [k for k, action in self.actions.items() if action.isChecked()]
        update_ui_state({self.ui_keys: list(self.keys)})
        self.refresh_label()
        self.owner._maybe_fetch_signals()
        self.owner._schedule_redraw()

    # ---- data ---------------------------------------------------------------
    def maybe_fetch(self, span: tuple[datetime, datetime], live: bool, fresh_for, now_local: datetime, force: bool = False) -> None:
        if not self.keys or self.slot.is_running():
            return
        wanted = tuple(k for k in self.keys if k not in self.keys_loaded)
        same_start = self.window is not None and self.window[0] == span[0]
        same_end = self.window is not None and self.window[1] == span[1]
        fresh = self.fetched_local is not None and (now_local - self.fetched_local) < fresh_for
        if not force and not wanted and same_start and (fresh if live else same_end):
            return
        keys = list(self.keys)
        settings = self.owner.settings
        self.slot.start(
            lambda job: fetch_fleet_signals(settings, keys, span[0], span[1], job),
            on_result=lambda result, keys=tuple(keys), span=span: self._on_loaded(result, keys, span, now_local),
            on_error=lambda message: self.owner.status_label.setText(f"{self.title}: {message}"),
        )

    def _on_loaded(self, result, keys, span, now_local) -> None:
        self.data = result or {}
        self._paths = {}
        self.keys_loaded = keys
        self.window = span
        self.fetched_local = now_local
        self.owner._schedule_redraw()

    # ---- drawing ------------------------------------------------------------
    def reset_for_redraw(self) -> None:
        self.edge_bands = []
        self.hover_rows = []
        self.hover_items = []
        self.label_items = []
        self.guide_item = None
        self.guide_label = None

    def strip_height(self, zoom: float) -> int:
        return int(self.strip_h * zoom) if self.keys else 0

    def _fmt(self, value: float) -> str:
        return f"{value:.{self.decimals}f}{self.unit}"

    def draw_strip(self, state, rect: QRectF, window_start: datetime, window_end: datetime, scene_width: float, right_pad: float, title_x: float = 22.0, show_latest: bool = True, label_bg: QColor | None = None) -> None:
        """`label_bg` is the colour of the table behind the labels (the
        Overview's rows alternate), so the label backdrops match it."""
        scene = self.owner.scene
        backdrop = label_bg or LABEL_BACKDROP
        bg = scene.addRect(rect, QPen(QColor("#31414d")), QBrush(QColor("#0b1014")))
        bg.setZValue(1)
        self.edge_bands.append((rect.bottom(), rect.left(), rect.right()))
        title_font = QFont()
        title_font.setPointSize(8)
        title_item = scene.addText(self.axis_title, title_font)
        title_item.setDefaultTextColor(QColor(theme.TEXT_MUTED))
        title_y = rect.top() + max(0.0, (rect.height() - title_item.boundingRect().height()) / 2)
        title_item.setPos(title_x, title_y)
        title_item.setZValue(4)
        add_label_backdrop(title_item, backdrop)
        self.label_items.append((title_item, "title", title_y))
        tracks = self.data.get(state.robot_id or "", {})
        w0 = int(window_start.timestamp() * 1000)
        w1 = int(window_end.timestamp() * 1000)
        chosen = []
        for key, label in self.choices:
            track = tracks.get(key) if key in self.keys else None
            if track is None:
                continue
            stats = window_stats(track, w0, w1)
            if stats is not None:
                chosen.append((key, label, track, stats))
        if not chosen:
            # Loading while a fetch runs, while a newly ticked reading has
            # not arrived yet, or before the first fetch; otherwise the
            # readings simply are not there (Chris, 2026-09-08).
            pending = any(k not in self.keys_loaded for k in self.keys)
            waiting = self.slot.is_running() or pending or self.window is None
            bg.setToolTip(self.loading_text if waiting else self.empty_text)
            note_font = QFont()
            note_font.setPointSize(8)
            note = scene.addText("Loading" if waiting else "No data available", note_font)
            note.setDefaultTextColor(QColor(theme.TEXT_FAINT))
            note_rect = note.boundingRect()
            note.setPos(rect.center().x() - note_rect.width() / 2, rect.center().y() - note_rect.height() / 2)
            note.setZValue(4)
            note.setAcceptedMouseButtons(Qt.NoButton)
            return
        lo = min(s[3][0] for s in chosen)
        hi = max(s[3][1] for s in chosen)
        if self.axis_min is not None:
            lo = min(self.axis_min, lo)
        if self.axis_max is not None:
            hi = max(self.axis_max, hi)
        if hi - lo < 1e-6:
            lo, hi = lo - 0.5, hi + 0.5
        inner_top = rect.top() + 2
        inner_h = rect.height() - 4
        span_s = max(1.0, (w1 - w0) / 1000.0)
        # Children of the background clip to it, so a day-long path shows
        # only the visible window.
        bg.setFlag(QGraphicsItem.ItemClipsChildrenToShape, True)
        sx = rect.width() / span_s
        sy = -inner_h / (hi - lo)
        transform = QTransform(sx, 0.0, 0.0, sy, rect.left() - (w0 / 1000.0) * sx, inner_top + inner_h - lo * sy)
        for key, _label, track, _stats in chosen:
            path = self._path_for(state.robot_id or "", key, track)
            pen = QPen(QColor(self.colours.get(key, "#ffffff")))
            pen.setWidthF(1.3)
            pen.setCosmetic(True)
            item = scene.addPath(path, pen)
            item.setParentItem(bg)
            item.setTransform(transform)
            item.setZValue(3.5)
            item.setAcceptedMouseButtons(Qt.NoButton)
        small = QFont()
        small.setPointSize(7)
        axis_fmt = "{:.0f}" if self.decimals <= 1 else "{:.1f}"
        # (temperatures whole degrees; amps and bar to one decimal)
        for text, y_pos in ((axis_fmt.format(hi) + self.axis_unit, rect.top() - 3), (axis_fmt.format(lo) + self.axis_unit, rect.bottom() - 13)):
            label_item = scene.addText(text, small)
            label_item.setDefaultTextColor(QColor(theme.TEXT_FAINT))
            label_item.setPos(rect.left() - 34, y_pos)
            label_item.setZValue(4)
            add_label_backdrop(label_item, backdrop)
            self.label_items.append((label_item, "axis", y_pos))
        if not show_latest:
            self.hover_rows.append((rect, lo, hi, inner_top, inner_h, [(k, label, t) for k, label, t, _s in chosen], state.name))
            return
        latest = " · ".join(
            f"{self.short.get(label, label.replace('Motor ', 'M'))} {axis_fmt.format(stats[2])}{self.axis_unit}"
            for _k, label, _t, stats in chosen
        )
        latest = self.owner._fit_text(latest, right_pad - 20, QFontMetrics(small)) or latest
        latest_item = scene.addText(latest, small)
        latest_item.setDefaultTextColor(QColor(theme.TEXT_MUTED))
        latest_item.setPos(scene_width - right_pad + 8, rect.top() - 4)
        latest_item.setZValue(4)
        self.hover_rows.append((rect, lo, hi, inner_top, inner_h, [(k, label, t) for k, label, t, _s in chosen], state.name))

    def _path_for(self, robot: str, key: str, track) -> QPainterPath:
        cached = self._paths.get((robot, key))
        if cached is not None:
            return cached
        path = QPainterPath()
        pen_down = False
        last_t = None
        # A gap over five minutes breaks the line; a month-long track is
        # sampled every 5 or 30 minutes, so the break scales with its
        # spacing (Chris, 2026-09-11: Picks was blank zoomed out to a month).
        break_ms = track.gap_break_ms()
        for t, v in zip(track.times_ms, track.values):
            if v is None:
                pen_down = False
                continue
            if pen_down and last_t is not None and t - last_t > break_ms:
                pen_down = False
            x = t / 1000.0
            if pen_down:
                path.lineTo(x, v)
            else:
                path.moveTo(x, v)
                pen_down = True
            last_t = t
        self._paths[(robot, key)] = path
        return path

    # ---- stretching by dragging the bottom line ----------------------------
    def edge_at(self, scene_pos) -> bool:
        x, y = float(scene_pos.x()), float(scene_pos.y())
        return any(abs(y - edge) <= 5 and x0 <= x <= x1 for edge, x0, x1 in self.edge_bands)

    def handle_resize(self, event) -> bool:
        """While dragging only a guide line moves; the rows are rebuilt once
        on release (rebuilding on every mouse move blanked the screen)."""
        owner = self.owner
        view, scene = owner.view, owner.scene
        event_type = event.type()
        if self.resize is not None:
            if event_type == QEvent.MouseMove:
                press_y, start_h, edge_y, x0, x1 = self.resize
                zoom = max(0.1, theme.zoom_factor())
                delta = (float(view.mapToScene(event.pos()).y()) - press_y) / zoom
                new_h = int(min(STRIP_MAX, max(STRIP_MIN, start_h + delta)))
                self.strip_h = new_h
                guide_y = edge_y + (new_h - start_h) * zoom
                if self.guide_item is None:
                    pen = QPen(QColor(GUIDE_COLOUR))
                    pen.setStyle(Qt.DashLine)
                    pen.setWidthF(1.5)
                    pen.setCosmetic(True)
                    self.guide_item = scene.addLine(x0, guide_y, x1, guide_y, pen)
                    self.guide_item.setZValue(20)
                    self.guide_label = scene.addText("")
                    self.guide_label.setDefaultTextColor(QColor(GUIDE_COLOUR))
                    self.guide_label.setZValue(20)
                self.guide_item.setLine(x0, guide_y, x1, guide_y)
                self.guide_label.setPlainText(f"{new_h} px")
                self.guide_label.setPos(x1 - 48, guide_y - 18)
                return True
            if event_type == QEvent.MouseButtonRelease:
                self.resize = None
                for item in (self.guide_item, self.guide_label):
                    if item is not None:
                        try:
                            scene.removeItem(item)
                        except RuntimeError:
                            pass
                self.guide_item = None
                self.guide_label = None
                view.viewport().unsetCursor()
                self.edge_hover = False
                update_ui_state({self.ui_strip: int(self.strip_h)})
                owner._redraw()
                return True
            return True
        if not self.edge_bands or owner._drag_candidate is not None:
            return False
        if event_type == QEvent.MouseMove:
            over = self.edge_at(view.mapToScene(event.pos()))
            if over != self.edge_hover:
                self.edge_hover = over
                if over:
                    view.viewport().setCursor(Qt.SizeVerCursor)
                else:
                    view.viewport().unsetCursor()
            return False
        if event_type == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
            scene_pos = view.mapToScene(event.pos())
            if self.edge_at(scene_pos):
                y = float(scene_pos.y())
                edge_y, x0, x1 = min(self.edge_bands, key=lambda band: abs(band[0] - y))
                self.resize = (y, int(self.strip_h), edge_y, x0, x1)
                owner.hide_thumbnail_preview()
                return True
        return False

    # ---- hover: a dot per trace and a box of the readings ------------------
    def strip_under(self, y: float | None):
        if y is None:
            return None
        for row in self.hover_rows:
            if row[0].top() <= y <= row[0].bottom():
                return row
        return None

    def clear_hover(self) -> None:
        scene = self.owner.scene
        for item in self.hover_items:
            try:
                if item.scene() is not None:
                    scene.removeItem(item)
            except RuntimeError:
                pass
        self.hover_items = []

    def draw_hover(self, hover_x: float, hover_dt: datetime, hover_y: float | None, timeline_x: float, timeline_width: float, grid_top: float) -> bool:
        strip = self.strip_under(hover_y)
        if strip is None:
            return False
        scene = self.owner.scene
        rect, lo, hi, inner_top, inner_h, tracks, system_name = strip
        t_ms = int(hover_dt.timestamp() * 1000)
        lines = []
        for key, label, track in tracks:
            value = track.value_at(t_ms)
            if value is None:
                continue
            y_dot = inner_top + inner_h - (value - lo) / (hi - lo) * inner_h
            colour = self.colours.get(key, "#ffffff")
            dot = scene.addEllipse(hover_x - 3.5, y_dot - 3.5, 7, 7, QPen(QColor("#0b1014"), 1), QBrush(QColor(colour)))
            dot.setZValue(5)
            dot.setAcceptedMouseButtons(Qt.NoButton)
            self.hover_items.append(dot)
            lines.append(f'<span style="color:{colour};">{label} {self._fmt(value)}</span>')
        if not lines:
            return True
        # System and time on top, a rule, then the readings, in smaller type.
        when = hover_dt.astimezone().strftime("%H:%M:%S")
        head = f'<span style="color:{theme.TEXT_BRIGHT}; font-weight:bold;">{system_name}</span> <span style="color:{theme.TEXT_MUTED};">{when}</span>'
        text = scene.addText("")
        small = QFont(text.font())
        small.setPointSizeF(max(6.0, small.pointSizeF() * 0.7))
        text.setFont(small)
        text.setHtml("<div style='white-space:nowrap;'>" + head + f"<hr style='color:{theme.BORDER_LIGHT};'>" + "<br>".join(lines) + "</div>")
        text.setZValue(6.2)
        text.setAcceptedMouseButtons(Qt.NoButton)
        # The document needs its ideal width or the rule collapses to a dot.
        text.setTextWidth(text.document().idealWidth())
        bounds = text.boundingRect()
        box_w, box_h = bounds.width() + 8, bounds.height() + 6
        box_x = hover_x + 12
        if box_x + box_w > timeline_x + timeline_width:
            box_x = hover_x - 12 - box_w
        box_y = rect.top() - box_h - 4 if rect.top() - box_h - 4 >= grid_top else rect.bottom() + 4
        box = scene.addRect(QRectF(box_x, box_y, box_w, box_h), QPen(QColor(theme.BORDER_LIGHT)), QBrush(QColor(theme.BG_RAISED)))
        box.setZValue(6.1)
        box.setAcceptedMouseButtons(Qt.NoButton)
        text.setPos(box_x + 4, box_y + 3)
        self.hover_items.extend([box, text])
        return True


class DataBoxes(QObject):
    """The Data box (Picks, Temps, Currents, Pressure) and the Additional
    data box, with their channels, for one owner: the Overview or the
    System Replay timeline (Chris, 2026-09-08). `prefix` keys the saved
    selection so each screen remembers its own."""

    def __init__(self, owner, prefix: str, picks_default: bool = False):
        super().__init__(owner)
        self.owner = owner
        self.picks = SignalChannel(
            owner, name="picks", title="Picks", icon=pick_icon(),
            tooltip="Pick rate (Grafana for Argus 2, the pick messages in Elastic for Argus 1)",
            choices=PICKS_CHOICES, colours=PICKS_COLOURS, unit="/min", axis_unit="/min", decimals=1,
            separator_before="", ui_keys=f"{prefix}_picks", ui_strip=f"{prefix}_picks_strip_height",
            default_strip_h=DEFAULT_STRIP_HEIGHT, short={"Picks per minute": "Picks"},
            loading_text="Loading pick rate...", empty_text="No picks in this window",
            axis_min=0.0, axis_title="Picks/min",
            default_keys=["picks_per_min"] if picks_default else None,
        )
        self.temps = SignalChannel(
            owner, name="temps", title="Temps", icon=thermometer_icon(),
            tooltip="Which temperatures to draw (from Grafana)",
            choices=TEMPERATURE_CHOICES, colours=TEMPERATURE_COLOURS, unit="\u00b0C", axis_unit="\u00b0", decimals=1,
            separator_before="motor_temp_1", ui_keys=f"{prefix}_temperatures", ui_strip=f"{prefix}_temp_strip_height",
            default_strip_h=DEFAULT_STRIP_HEIGHT,
            short={"CPU": "CPU", "RCU": "RCU", "GPU": "GPU", "Brake resistor": "Brake", "Hottest motor": "Hot"},
            loading_text="Loading temperatures...", empty_text="No temperature readings in this window",
            axis_title="Temperature",
        )
        self.currents = SignalChannel(
            owner, name="currents", title="Currents", icon=current_icon(),
            tooltip="Which motor currents to draw (from Grafana)",
            choices=CURRENT_CHOICES, colours=CURRENT_COLOURS, unit="A", axis_unit="A", decimals=2,
            separator_before="motor_current_1", ui_keys=f"{prefix}_currents", ui_strip=f"{prefix}_current_strip_height",
            default_strip_h=DEFAULT_STRIP_HEIGHT,
            short={"Highest motor": "Max"},
            loading_text="Loading currents...", empty_text="No current readings in this window",
            axis_title="Current",
        )
        self.pressure = SignalChannel(
            owner, name="pressure", title="Pressure", icon=gauge_icon(),
            tooltip="Draw the supply air pressure (from Grafana)",
            choices=PRESSURE_CHOICES, colours=PRESSURE_COLOURS, unit=" bar", axis_unit="b", decimals=2,
            separator_before="", ui_keys=f"{prefix}_pressure", ui_strip=f"{prefix}_pressure_strip_height",
            default_strip_h=DEFAULT_STRIP_HEIGHT,
            short={"Air pressure": "Air"},
            loading_text="Loading air pressure...", empty_text="No air pressure readings in this window",
            axis_min=0.0, axis_title="Pressure",
        )
        self.additional: list[SignalChannel] = []
        for spec in ADDITIONAL_CHANNELS:
            self.additional.append(SignalChannel(
                owner, name=spec["name"], title=spec["title"], icon=plus_box_icon(),
                tooltip=spec["title"], choices=spec["choices"], colours=spec["colours"],
                unit=spec["unit"], axis_unit=spec["axis_unit"], decimals=spec["decimals"],
                separator_before="", ui_keys=f"{prefix}_add_{spec['name']}", ui_strip=f"{prefix}_add_{spec['name']}_strip_height",
                default_strip_h=DEFAULT_STRIP_HEIGHT, short={},
                loading_text=f"Loading {spec['title'].lower()}...", empty_text=f"No {spec['title'].lower()} readings in this window",
                axis_min=spec["axis_min"], axis_max=spec.get("axis_max"), axis_title=spec["title"],
            ))
        self.data_channels = (self.picks, self.temps, self.currents, self.pressure)
        self.channels = (*self.data_channels, *self.additional)
        # The Data-box buttons share one width, the widest with a count.
        widest = 0
        for channel in self.data_channels:
            channel.button.setStyleSheet(f"font-size: {COMPACT_FONT_PX}px; padding: 0px 4px;")
            channel.button.setIconSize(QSize(14, 14))
            channel.button.setText(f"{channel.title} (6)")
            widest = max(widest, channel.button.sizeHint().width())
            channel.refresh_label()
        for channel in self.data_channels:
            channel.button.setFixedWidth(widest)
        # One combined menu for the Additional data box.
        self.additional_btn = QToolButton()
        self.additional_btn.setIcon(plus_box_icon())
        self.additional_btn.setIconSize(QSize(14, 14))
        self.additional_btn.setStyleSheet(f"font-size: {COMPACT_FONT_PX}px; padding: 0px 4px;")
        self.additional_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.additional_btn.setPopupMode(QToolButton.InstantPopup)
        self.additional_btn.setToolTip("Computer health and housekeeping readings, one strip each")
        self.additional_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        menu = QMenu(self.additional_btn)
        menu.addAction("Show all", lambda: self.set_all_additional(True))
        menu.addAction("Hide all", lambda: self.set_all_additional(False))
        menu.addSeparator()
        self.additional_proxies: list[tuple[SignalChannel, str, QAction]] = []
        for channel in self.additional:
            many = len(channel.choices) > 1
            for key, label in channel.choices:
                proxy = QAction(f"{channel.title}: {label}" if many else label, owner)
                proxy.setCheckable(True)
                proxy.setChecked(key in channel.keys)
                proxy.toggled.connect(lambda checked, ch=channel, k=key: self._on_additional_toggled(ch, k, checked))
                menu.addAction(proxy)
                self.additional_proxies.append((channel, key, proxy))
            if channel is not self.additional[-1]:
                menu.addSeparator()
        self.additional_btn.setMenu(menu)
        self.additional_key = QLabel("")
        self.additional_key.setTextFormat(Qt.RichText)
        self.additional_key.setStyleSheet(KEY_LABEL_STYLE)
        # The two boxes.
        style = COMPACT_BOX_STYLE
        self.data_box = CollapsibleGroupBox("Data")
        self.data_box.setStyleSheet(style)
        data_layout = QVBoxLayout(self.data_box.body)
        data_layout.setContentsMargins(4, 2, 4, 2)
        data_layout.setSpacing(3)
        for channel in self.data_channels:
            row = QHBoxLayout()
            row.setSpacing(10)
            row.addWidget(channel.button)
            row.addWidget(channel.key_label)
            row.addStretch(1)
            data_layout.addLayout(row)
            channel.refresh_label()  # hidden while parentless; now it has a home
        self.additional_box = CollapsibleGroupBox("Additional data")
        self.additional_box.setStyleSheet(style)
        add_layout = QVBoxLayout(self.additional_box.body)
        add_layout.setContentsMargins(4, 2, 4, 2)
        add_layout.setSpacing(1)
        add_row = QHBoxLayout()
        add_row.setSpacing(10)
        add_row.addWidget(self.additional_btn)
        add_row.addStretch(1)
        add_layout.addLayout(add_row)
        add_layout.addWidget(self.additional_key)
        add_layout.addStretch(1)
        self.refresh_additional_label()

    def row_layout(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.data_box)
        row.addSpacing(12)
        row.addWidget(self.additional_box)
        row.addStretch(1)
        return row

    # ---- the Additional data menu ------------------------------------------
    def _on_additional_toggled(self, channel: SignalChannel, key: str, checked: bool) -> None:
        action = channel.actions[key]
        if action.isChecked() != checked:
            action.setChecked(checked)  # the channel's own action does the work
        self.refresh_additional_label()

    def set_all_additional(self, on: bool) -> None:
        for _channel, _key, proxy in self.additional_proxies:
            proxy.setChecked(on)

    def refresh_additional_label(self) -> None:
        n = sum(len(channel.keys) for channel in self.additional)
        self.additional_btn.setText("" if not n else f"({n})")
        self.additional_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon if n else Qt.ToolButtonIconOnly)
        bits = []
        for channel in self.additional:
            many = len(channel.choices) > 1
            for key, label in channel.choices:
                if key in channel.keys:
                    text = f"{channel.title} {label}" if many else label
                    bits.append(f'{key_swatch(channel.colours[key])}&nbsp;{text}')
        self.additional_key.setText("&nbsp;&nbsp;".join(bits))
        self.additional_key.setVisible(bool(bits))

    # ---- the whole set ------------------------------------------------------
    @property
    def any_active(self) -> bool:
        return any(channel.active for channel in self.channels)

    def maybe_fetch(self, span, live: bool, fresh_for, now_local, force: bool = False) -> None:
        for channel in self.channels:
            channel.maybe_fetch(span, live, fresh_for, now_local, force)

    def reset_for_redraw(self) -> None:
        for channel in self.channels:
            channel.reset_for_redraw()

    def strip_heights(self, zoom: float) -> list[tuple[SignalChannel, int]]:
        return [(channel, channel.strip_height(zoom)) for channel in self.channels if channel.active]

    def handle_resize(self, event) -> bool:
        return any(channel.handle_resize(event) for channel in self.channels)

    def clear_hover(self) -> None:
        for channel in self.channels:
            channel.clear_hover()

    def draw_hover(self, hover_x, hover_dt, hover_y, timeline_x, timeline_width, grid_top) -> bool:
        return any(channel.draw_hover(hover_x, hover_dt, hover_y, timeline_x, timeline_width, grid_top) for channel in self.channels)
