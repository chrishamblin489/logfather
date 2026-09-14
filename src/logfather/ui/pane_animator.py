"""One sliding pane, written once.

``PaneAnimator`` slides one pane of a ``QSplitter`` to a pixel size over
a short eased animation (or, built with ``for_width``, slides a widget's
own width by pinning its minimum and maximum). Every step re-reads the
splitter, gives the animated pane its new size and hands the difference
to one *absorbing* sibling (the centre pane, by default); every other
pane keeps the size it has. A ``widget`` given to the constructor is
shown before an opening slide (``show_to``) and hidden when a closing
slide reaches 0 px (``hide``) - so the splitter stops reserving room for
it. Starting a slide while one is running stops the running one and
continues from wherever the pane is now.

Before this module four sites open-coded the same stop / start / step /
finish triple, two of them with the step wrapped in ``except Exception:
pass`` (docs/CODE_REVIEW_2026-09-14.md, item 2d).
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QEasingCurve, QObject, QVariantAnimation
from PySide6.QtWidgets import QSplitter, QWidget

DEFAULT_DURATION_MS = 170


class PaneAnimator:
    """Slide pane ``index`` of ``splitter`` to a size.

    ``absorb`` names the sibling that gives or takes the pixels (default:
    the neighbour towards the centre - pane 1 for pane 0, the pane before
    otherwise); it never drops below ``absorb_min_px``. With
    ``cap_to_total`` the animated pane is also held to ``total -
    absorb_min_px`` so the two always fit. ``fallback_total(px)`` supplies
    the total when the splitter has no size yet (before its first
    layout). ``animation`` is the underlying ``QVariantAnimation``:
    connect to its ``finished`` or drive it by hand in a test.
    """

    def __init__(
        self,
        splitter: QSplitter | None,
        index: int = 0,
        *,
        widget: QWidget | None = None,
        absorb: int | None = None,
        absorb_min_px: int = 1,
        cap_to_total: bool = False,
        fallback_total: Callable[[int], int] | None = None,
        duration_ms: int = DEFAULT_DURATION_MS,
        easing: QEasingCurve.Type = QEasingCurve.OutCubic,
        parent: QObject | None = None,
    ) -> None:
        self._splitter = splitter
        self._index = int(index)
        self._absorb = (1 if self._index == 0 else self._index - 1) if absorb is None else int(absorb)
        self._widget = widget
        self._width_widget: QWidget | None = None
        self._absorb_min_px = int(absorb_min_px)
        self._cap_to_total = bool(cap_to_total)
        self._fallback_total = fallback_total
        self.animation = QVariantAnimation(parent)
        self.animation.setDuration(int(duration_ms))
        self.animation.setEasingCurve(easing)
        self.animation.valueChanged.connect(self._on_step)
        self.animation.finished.connect(self._on_finished)

    @classmethod
    def for_width(
        cls,
        widget: QWidget,
        *,
        duration_ms: int = DEFAULT_DURATION_MS,
        easing: QEasingCurve.Type = QEasingCurve.OutCubic,
        parent: QObject | None = None,
    ) -> "PaneAnimator":
        """An animator that slides ``widget``'s own width (min = max = px)
        instead of a splitter pane - for a column in a plain layout."""
        self = cls(None, duration_ms=duration_ms, easing=easing, parent=parent)
        self._width_widget = widget
        return self

    # ---- public -----------------------------------------------------------

    def animate_to(self, px: int) -> None:
        """Slide from the pane's current size to ``px``. Already there:
        no slide, but a 0 px pane's widget is hidden."""
        current = self._current_px()
        if current is None:
            return
        px = self._clamp(int(px), self._total())
        if current == px:
            self._settle(px)
            return
        self.stop()
        self.animation.setStartValue(current)
        self.animation.setEndValue(px)
        self.animation.start()

    def show_to(self, px: int) -> None:
        """Show the widget, then slide the pane open to ``px``."""
        if self._widget is not None:
            self._widget.setVisible(True)
        self.animate_to(px)

    def hide(self) -> None:
        """Slide the pane closed; the widget is hidden when it gets there."""
        self.animate_to(0)

    def snap_to(self, px: int) -> None:
        """Jump to ``px`` with no slide (stopping one that is running)."""
        self.stop()
        px = self._clamp(int(px), self._total())
        if self._widget is not None and px > 0:
            self._widget.setVisible(True)
        self._apply(px)
        self._settle(px)

    def is_running(self) -> bool:
        return self.animation.state() == QVariantAnimation.Running

    def stop(self) -> None:
        """Stop a running slide where it is (no ``finished``, no hide)."""
        if self.is_running():
            self.animation.stop()

    # ---- the slide ---------------------------------------------------------

    def _on_step(self, value) -> None:
        self._apply(self._clamp(int(value), self._total()))

    def _on_finished(self) -> None:
        self._settle(int(self.animation.endValue()))

    def _settle(self, px: int) -> None:
        if px == 0 and self._widget is not None:
            self._widget.setVisible(False)

    def _sizes(self) -> list[int] | None:
        """The splitter's sizes, or None when it lacks the panes."""
        if self._splitter is None:
            return None
        sizes = [int(s) for s in self._splitter.sizes()]
        if len(sizes) <= max(self._index, self._absorb):
            return None
        return sizes

    def _total(self) -> int:
        sizes = self._sizes()
        return sum(sizes) if sizes else 0

    def _current_px(self) -> int | None:
        if self._width_widget is not None:
            return max(0, int(self._width_widget.width()))
        sizes = self._sizes()
        if sizes is None:
            return None
        return max(0, sizes[self._index])

    def _clamp(self, px: int, total: int) -> int:
        px = max(0, px)
        if self._cap_to_total and total > self._absorb_min_px:
            px = min(px, total - self._absorb_min_px)
        return px

    def _apply(self, px: int) -> None:
        if self._width_widget is not None:
            self._width_widget.setMinimumWidth(px)
            self._width_widget.setMaximumWidth(px)
            return
        sizes = self._sizes()
        if sizes is None:
            return
        total = sum(sizes)
        if not total and self._fallback_total is not None:
            total = int(self._fallback_total(px))
        others = sum(s for i, s in enumerate(sizes) if i not in (self._index, self._absorb))
        sizes[self._index] = px
        sizes[self._absorb] = max(self._absorb_min_px, total - px - others)
        self._splitter.setSizes(sizes)
