from __future__ import annotations

from datetime import datetime, timezone

from PySide6.QtCore import Qt, QPropertyAnimation, QSequentialAnimationGroup, QEasingCurve
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QFrame,
    QGraphicsOpacityEffect,
)

from logfather.data.target_buffer_loader import BufferEvent, PickTarget, get_cam_pos
from logfather.ui import theme


def _display_target_id(target: PickTarget) -> str:
    src = target.source_doc if isinstance(target.source_doc, dict) else {}
    for key in ("target_index", "product_id"):
        value = src.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return str(target.target_id)


def _fmt_pos(v) -> str:
    """Format a [x, y, z] position list as a compact string."""
    if isinstance(v, (list, tuple)) and len(v) == 3:
        return f"{v[0]:.3f}, {v[1]:.3f}, {v[2]:.3f}"
    return str(v)


def _summary_rows(src: dict) -> list[tuple[str, str]]:
    """Always-visible rows: product identity."""
    rows: list[tuple[str, str]] = []
    sku = src.get("sku")
    if isinstance(sku, dict):
        if sku.get("product"):
            rows.append(("Product", str(sku["product"])))
        if sku.get("tray"):
            rows.append(("Tray", str(sku["tray"])))
        if sku.get("tool"):
            rows.append(("Tool", str(sku["tool"])))
    return rows


def _detail_rows(src: dict) -> list[tuple[str, str]]:
    """Expanded rows: position and size/shape data."""
    rows: list[tuple[str, str]] = []

    cam_pos = get_cam_pos(src)
    if cam_pos:
        rows.append(("Position", _fmt_pos(cam_pos)))

    metrics = src.get("metrics")
    if isinstance(metrics, dict):
        angle = metrics.get("angle")
        if angle is not None:
            rows.append(("Angle", f"{angle:.1f}°"))
        led = metrics.get("long_edge_distance")
        if led is not None:
            rows.append(("Length", f"{led:.4f} m"))
        area = metrics.get("contour_area")
        if area is not None:
            rows.append(("Area", f"{area:.0f} px²"))
        front = metrics.get("front_corner_point")
        if front:
            rows.append(("Front corner", _fmt_pos(front)))
        back = metrics.get("back_corner_point")
        if back:
            rows.append(("Back corner", _fmt_pos(back)))

    return rows


def _elapsed(added_at: datetime, now: datetime) -> str:
    delta = now - added_at
    total = int(delta.total_seconds())
    if total < 0:
        return "just now"
    if total < 60:
        return f"{total}s"
    m, s = divmod(total, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def _make_row(k: str, v: str) -> QHBoxLayout:
    row = QHBoxLayout()
    row.setSpacing(6)
    key_lbl = QLabel(k)
    key_lbl.setStyleSheet(theme.HINT_LABEL)
    key_lbl.setFixedWidth(90)
    key_lbl.setWordWrap(False)
    val_lbl = QLabel(v)
    val_lbl.setStyleSheet(theme.VALUE_LABEL)
    val_lbl.setWordWrap(True)
    row.addWidget(key_lbl)
    row.addWidget(val_lbl, 1)
    return row


class _TargetCard(QFrame):
    def __init__(self, target: PickTarget, gap_status: str = "normal", parent=None):
        super().__init__(parent)
        self._expanded = False
        self._target = target
        self._gap_status = str(gap_status or "normal")
        self.setFrameShape(QFrame.StyledPanel)
        self.setCursor(Qt.PointingHandCursor)
        self._apply_style()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(3)

        # Header row: product_id left, expand chevron + timestamp right
        header = QHBoxLayout()
        pid = _display_target_id(target)
        pid_lbl = QLabel(f"#{pid}" if pid else "—")
        pid_lbl.setStyleSheet(theme.CARD_TITLE)
        header.addWidget(pid_lbl)
        header.addStretch(1)

        self._chevron = QLabel("▶")
        self._chevron.setStyleSheet(theme.CHEVRON)
        header.addWidget(self._chevron)

        time_lbl = QLabel(target.added_at.astimezone().strftime("%H:%M:%S"))
        time_lbl.setStyleSheet(theme.CARD_TIME)
        header.addWidget(time_lbl)

        layout.addLayout(header)

        # Summary rows — always visible
        summary_rows = _summary_rows(target.source_doc)
        if summary_rows:
            for k, v in summary_rows:
                layout.addLayout(_make_row(k, v))
        else:
            layout.addWidget(QLabel("(no data)", styleSheet=theme.EMPTY_NOTE_INLINE))

        # Detail section — hidden until expanded
        self._detail = QWidget()
        detail_layout = QVBoxLayout(self._detail)
        detail_layout.setContentsMargins(0, 4, 0, 0)
        detail_layout.setSpacing(3)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet(theme.SEPARATOR)
        detail_layout.addWidget(sep)

        detail_rows = _detail_rows(target.source_doc)
        if detail_rows:
            for k, v in detail_rows:
                detail_layout.addLayout(_make_row(k, v))
        else:
            detail_layout.addWidget(QLabel("(no detail data)", styleSheet=theme.EMPTY_NOTE_INLINE))

        self._detail.setVisible(False)
        layout.addWidget(self._detail)

    def _apply_style(self) -> None:
        target = self._target
        valid = target.source_doc.get("valid", True)
        if not valid:
            bg, border = theme.CARD_INVALID_BG, theme.CARD_INVALID_BORDER
        else:
            try:
                odd = int(_display_target_id(target)) % 2 == 1
            except (TypeError, ValueError):
                odd = False
            bg = theme.CARD_BG_ALT if odd else theme.CARD_BG
            border = theme.CARD_BORDER
            if self._gap_status == "close":
                border = theme.GAP_CLOSE_BORDER
                bg = theme.GAP_CLOSE_BG_ODD if odd else theme.GAP_CLOSE_BG_EVEN
            elif self._gap_status == "wide":
                border = theme.GAP_WIDE_BORDER
                bg = theme.GAP_WIDE_BG_ODD if odd else theme.GAP_WIDE_BG_EVEN
        self.setStyleSheet(theme.target_card_style(bg, border))

    def set_gap_status(self, gap_status: str) -> None:
        gap_status = str(gap_status or "normal")
        if self._gap_status == gap_status:
            return
        self._gap_status = gap_status
        self._apply_style()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._expanded = not self._expanded
            self._detail.setVisible(self._expanded)
            self._chevron.setText("▼" if self._expanded else "▶")
        super().mousePressEvent(event)


class TargetBufferWidget(QWidget):
    """
    Panel that displays the current contents of the robot's pick-target buffer.

    Call :meth:`set_buffer_events` once after loading a day's data, then
    :meth:`update_for_time` whenever the video playhead moves.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(220)
        self._events: list[BufferEvent] = []
        self._current_targets: list[PickTarget] = []
        self._last_event: BufferEvent | None = None
        self._alerted_target_ids: set[str] = set()
        self._wide_gap_target_ids: set[str] = set()
        self._card_cache: dict[str, _TargetCard] = {}  # target_id -> card
        self._active_anims: list[QSequentialAnimationGroup] = []

        # Header
        self._header_lbl = QLabel("Targets")
        self._header_lbl.setStyleSheet(theme.BUFFER_HEADER)

        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(0)
        header_row.addWidget(self._header_lbl, 1)

        # Scroll area for target cards
        self._scroll_area = QScrollArea()
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll_area.setStyleSheet(theme.BUFFER_SCROLL)

        self._cards_container = QWidget()
        self._cards_container.setStyleSheet(theme.BUFFER_BG)
        self._cards_layout = QVBoxLayout(self._cards_container)
        self._cards_layout.setContentsMargins(0, 4, 0, 4)
        self._cards_layout.setSpacing(0)
        self._cards_layout.addStretch(1)

        self._scroll_area.setWidget(self._cards_container)

        # Empty state label
        self._empty_lbl = QLabel("Buffer empty")
        self._empty_lbl.setAlignment(Qt.AlignCenter)
        self._empty_lbl.setStyleSheet(theme.EMPTY_STATE)

        # No-data label (before any events are loaded)
        self._no_data_lbl = QLabel("No data loaded")
        self._no_data_lbl.setAlignment(Qt.AlignCenter)
        self._no_data_lbl.setStyleSheet(theme.NO_DATA_STATE)

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        root_layout.addLayout(header_row)
        root_layout.addWidget(self._scroll_area, 1)

        self._rebuild_cards()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_buffer_events(self, events: list[BufferEvent]) -> None:
        self._events = events
        self._current_targets = []
        self._last_event = None
        self._rebuild_cards(reference_time=None)

    def update_for_time(self, dt: datetime) -> None:
        from logfather.data.target_buffer_loader import buffer_state_at

        # Normalise to UTC. For naive datetimes, astimezone() assumes the
        # system local timezone (BST) before converting — replace() would
        # wrongly stamp them as UTC and produce a +1hr error.
        if dt.tzinfo is None:
            dt = dt.astimezone(timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)

        targets, last_ev = buffer_state_at(self._events, dt)
        if targets == self._current_targets and last_ev is self._last_event:
            return
        self._current_targets = targets
        self._last_event = last_ev
        self._rebuild_cards(reference_time=dt)

    def set_alerted_target_ids(self, target_ids: set[str]) -> None:
        self._alerted_target_ids = set(target_ids or set())
        for tid, card in self._card_cache.items():
            card.set_gap_status(self._gap_status_for_target(tid))

    def set_wide_gap_target_ids(self, target_ids: set[str]) -> None:
        self._wide_gap_target_ids = set(target_ids or set())
        for tid, card in self._card_cache.items():
            card.set_gap_status(self._gap_status_for_target(tid))

    def clear(self) -> None:
        self._events = []
        self._current_targets = []
        self._last_event = None
        self._rebuild_cards(reference_time=None)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _rebuild_cards(self, reference_time: datetime | None = None) -> None:
        layout = self._cards_layout
        now = reference_time if reference_time is not None else datetime.now(tz=timezone.utc)
        targets = self._current_targets

        # Detach reusable labels before touching the layout.
        layout.removeWidget(self._no_data_lbl)
        self._no_data_lbl.setParent(None)
        layout.removeWidget(self._empty_lbl)
        self._empty_lbl.setParent(None)

        if not self._events:
            # Purge all cached cards.
            for card in self._card_cache.values():
                layout.removeWidget(card)
                card.deleteLater()
            self._card_cache.clear()
            layout.insertWidget(0, self._no_data_lbl)

            return

        n = len(targets)
        visible = list(reversed(targets[-10:]))
        visible_ids = {t.target_id for t in visible}

        # Remove cards that have scrolled out of the visible window.
        for tid in list(self._card_cache):
            if tid not in visible_ids:
                card = self._card_cache.pop(tid)
                layout.removeWidget(card)
                card.deleteLater()

        if not targets:

            layout.insertWidget(0, self._empty_lbl)
            return



        for i, target in enumerate(visible):
            tid = target.target_id
            if tid in self._card_cache:
                card = self._card_cache[tid]
                card.set_gap_status(self._gap_status_for_target(tid))
                # Reorder if necessary.
                if layout.indexOf(card) != i:
                    layout.removeWidget(card)
                    layout.insertWidget(i, card)
            else:
                card = _TargetCard(
                    target,
                    gap_status=self._gap_status_for_target(tid),
                )
                self._card_cache[tid] = card
                layout.insertWidget(i, card)
                layout.activate()          # force layout pass so card.height() is real
                natural_h = card.height()
                self._animate_card_in(card, natural_h)

    def _gap_status_for_target(self, target_id: str) -> str:
        if target_id in self._alerted_target_ids:
            return "close"
        if target_id in self._wide_gap_target_ids:
            return "wide"
        return "normal"

    def _animate_card_in(self, card: _TargetCard, natural_h: int) -> None:
        """Expand height (pushes cards down) while fading content in."""
        effect = QGraphicsOpacityEffect(card)
        effect.setOpacity(0.0)
        card.setGraphicsEffect(effect)
        card.setMaximumHeight(0)

        h_anim = QPropertyAnimation(card, b"maximumHeight", self)
        h_anim.setStartValue(0)
        h_anim.setEndValue(natural_h)
        h_anim.setDuration(350)
        h_anim.setEasingCurve(QEasingCurve.OutCubic)

        o_anim = QPropertyAnimation(effect, b"opacity", self)
        o_anim.setStartValue(0.0)
        o_anim.setEndValue(1.0)
        o_anim.setDuration(350)
        o_anim.setEasingCurve(QEasingCurve.InQuad)

        group = QSequentialAnimationGroup(self)
        group.addAnimation(h_anim)
        group.addAnimation(o_anim)

        def _on_done(c=card, g=group):
            c.setMaximumHeight(16777215)
            c.setGraphicsEffect(None)
            if g in self._active_anims:
                self._active_anims.remove(g)

        group.finished.connect(_on_done)
        self._active_anims.append(group)
        group.start()
