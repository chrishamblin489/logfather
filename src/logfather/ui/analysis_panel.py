"""The PikPak Replay's Analysis controls: frame differencing and optical
flow over the current picture, shown as a side-by-side view, blended over
the main picture, or in a popout window.

Split out of replay_view.py on 2026-09-14 (code review item 9). The maths
lives in ``core/frame_analysis.py``; this widget owns the controls (mode,
display, pairing, the reference frame, the gain / threshold / alpha /
scale / arrow sliders), the side-by-side ``view_label`` the replay places
in its video row, the ``main_alpha_label`` / ``main_alpha_slider`` pair the
replay places in its Overlay strip, and the "Analysis View" popout. The
widget itself is the controls column the Video Popout shows next to the
canvas.

Frames come from the replay through two callbacks (the replay converts
its BGR frames to RGB lazily, so nothing is converted until a view needs
it). What the replay must do in return arrives as signals:
``redraw_requested`` when the main picture must be repainted (the main
alpha slider moved) and ``layout_changed`` when the side-by-side view
appeared or went (the replay re-places the Additional CCTV picture).
``main_overlay_image()`` is the synchronous hook the replay's frame
painter calls for the blended picture when "Main Overlay" is on, so the
per-frame timing is unchanged.
"""
from __future__ import annotations

from typing import Callable

import cv2
import numpy as np
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from logfather.core.frame_analysis import compute_optical_flow_view, compute_pixel_diff_view
from logfather.ui.viewer_widgets import VideoFrameLabel

MODES = ("Off", "Frame Diff", "Optical Flow")
DISPLAYS = ("Main Overlay", "Main Side-by-side", "Popout")
PAIRINGS = ("Reference -> Current", "Previous -> Current")

# The replay hands over (RGB frame or None, frame index) for the current
# picture and (RGB frame or None, frame index or None) for the previous one.
FrameProvider = Callable[[], tuple[np.ndarray | None, int | None]]


def rgb_to_qimage(rgb: np.ndarray) -> QImage:
    h, w, _ch = rgb.shape
    return QImage(rgb.data, w, h, rgb.strides[0], QImage.Format_RGB888).copy()


class AnalysisPanel(QWidget):
    redraw_requested = Signal()   # the main picture must be repainted
    layout_changed = Signal()     # the side-by-side view was shown or hidden

    def __init__(
        self,
        current_frame: FrameProvider,
        previous_frame: FrameProvider,
        *,
        scrub_callback: Callable[[int], None] | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._current_frame = current_frame
        self._previous_frame = previous_frame
        # The reference frame persists across clips.
        self.ref_frame_rgb: np.ndarray | None = None
        self.ref_frame_index: int | None = None
        self._popout: QWidget | None = None
        self._popout_label: VideoFrameLabel | None = None
        self._build_controls(scrub_callback)
        self._update_controls_state()
        self._update_output()

    # ---- construction ---------------------------------------------------------

    def _build_controls(self, scrub_callback) -> None:
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(list(MODES))
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)

        self.display_combo = QComboBox()
        self.display_combo.addItems(list(DISPLAYS))
        self.display_combo.currentIndexChanged.connect(self._on_display_changed)

        self.pair_combo = QComboBox()
        self.pair_combo.addItems(list(PAIRINGS))
        self.pair_combo.currentIndexChanged.connect(self.refresh_view)

        self.set_ref_btn = QPushButton("Set Reference")
        self.set_ref_btn.clicked.connect(self.set_reference)
        self.clear_ref_btn = QPushButton("Clear Reference")
        self.clear_ref_btn.clicked.connect(self.clear_reference)

        self.heatmap_cb = QCheckBox("Heatmap")
        self.heatmap_cb.setChecked(True)
        self.heatmap_cb.stateChanged.connect(self.refresh_view)
        self.overlay_cb = QCheckBox("Overlay")
        self.overlay_cb.setChecked(False)
        self.overlay_cb.stateChanged.connect(self.refresh_view)
        self.arrows_cb = QCheckBox("Flow arrows")
        self.arrows_cb.setChecked(False)
        self.arrows_cb.stateChanged.connect(self.refresh_view)
        self.arrows_cb.stateChanged.connect(self._update_controls_state)
        self.hide_zero_flow_cb = QCheckBox("Hide zero flow")
        self.hide_zero_flow_cb.setChecked(True)
        self.hide_zero_flow_cb.stateChanged.connect(self.refresh_view)
        self.hide_zero_flow_cb.stateChanged.connect(self._update_controls_state)
        self.zero_flow_label = QLabel("Min flow: 0.00")
        self.zero_flow_slider = QSlider(Qt.Horizontal)
        self.zero_flow_slider.setRange(0, 100)
        self.zero_flow_slider.setValue(1)
        self.zero_flow_slider.setFixedWidth(140)
        self.zero_flow_slider.valueChanged.connect(self._on_zero_flow_changed)
        self._update_zero_flow_label()

        self.gain_label = QLabel("Gain: 6x")
        self.gain_slider = QSlider(Qt.Horizontal)
        self.gain_slider.setRange(1, 30)
        self.gain_slider.setValue(6)
        self.gain_slider.valueChanged.connect(self._on_gain_changed)

        self.thresh_label = QLabel("Threshold / Min motion: 15")
        self.thresh_slider = QSlider(Qt.Horizontal)
        self.thresh_slider.setRange(0, 255)
        self.thresh_slider.setValue(15)
        self.thresh_slider.valueChanged.connect(self._on_thresh_changed)

        self.alpha_label = QLabel("Overlay alpha: 0.60")
        self.alpha_slider = QSlider(Qt.Horizontal)
        self.alpha_slider.setRange(0, 100)
        self.alpha_slider.setValue(60)
        self.alpha_slider.valueChanged.connect(self._on_alpha_changed)

        self.scale_label = QLabel("Compute scale: 100%")
        self.scale_slider = QSlider(Qt.Horizontal)
        self.scale_slider.setRange(25, 100)
        self.scale_slider.setValue(100)
        self.scale_slider.valueChanged.connect(self._on_scale_changed)

        self.arrow_step_label = QLabel("Arrow step: 20 px")
        self.arrow_step_slider = QSlider(Qt.Horizontal)
        self.arrow_step_slider.setRange(8, 60)
        self.arrow_step_slider.setValue(20)
        self.arrow_step_slider.valueChanged.connect(self._on_arrow_step_changed)

        self.arrow_scale_label = QLabel("Arrow length scale: 1.5x")
        self.arrow_scale_slider = QSlider(Qt.Horizontal)
        self.arrow_scale_slider.setRange(5, 50)
        self.arrow_scale_slider.setValue(15)
        self.arrow_scale_slider.valueChanged.connect(self._on_arrow_scale_changed)

        # The main-overlay alpha lives on the replay's Overlay strip.
        self.main_alpha_label = QLabel("Overlay: 0.60")
        self.main_alpha_slider = QSlider(Qt.Horizontal)
        self.main_alpha_slider.setRange(0, 100)
        self.main_alpha_slider.setValue(60)
        self.main_alpha_slider.setFixedWidth(150)
        self.main_alpha_slider.valueChanged.connect(self._on_main_alpha_changed)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Analysis:"))
        row1.addWidget(self.mode_combo)
        row1.addSpacing(8)
        row1.addWidget(QLabel("Display:"))
        row1.addWidget(self.display_combo)
        row1.addStretch(1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Pairing:"))
        row2.addWidget(self.pair_combo)
        row2.addSpacing(8)
        row2.addWidget(self.set_ref_btn)
        row2.addWidget(self.clear_ref_btn)
        row2.addStretch(1)

        row3 = QHBoxLayout()
        row3.addWidget(self.heatmap_cb)
        row3.addWidget(self.overlay_cb)
        row3.addWidget(self.arrows_cb)
        row3.addWidget(self.hide_zero_flow_cb)
        row3.addWidget(self.zero_flow_label)
        row3.addWidget(self.zero_flow_slider)
        row3.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addLayout(row1)
        layout.addLayout(row2)
        layout.addLayout(row3)
        for label, slider in (
            (self.gain_label, self.gain_slider),
            (self.thresh_label, self.thresh_slider),
            (self.alpha_label, self.alpha_slider),
            (self.scale_label, self.scale_slider),
            (self.arrow_step_label, self.arrow_step_slider),
            (self.arrow_scale_label, self.arrow_scale_slider),
        ):
            slider.setFixedWidth(210)
            row = QHBoxLayout()
            row.addWidget(label)
            row.addWidget(slider)
            layout.addLayout(row)
        layout.addStretch(1)
        self.setMaximumWidth(330)

        # The side-by-side view, placed by the replay next to the pictures.
        self.view_label = VideoFrameLabel("Analysis view")
        self.view_label.setAlignment(Qt.AlignCenter)
        self.view_label.setMinimumSize(480, 220)
        self.view_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        if scrub_callback is not None:
            self.view_label.set_scrub_callback(scrub_callback)
        self.view_label.setVisible(False)

    # ---- state queries ----------------------------------------------------------

    @property
    def popout_window(self) -> QWidget | None:
        return self._popout

    def mode(self) -> str:
        return self.mode_combo.currentText()

    def display(self) -> str:
        return self.display_combo.currentText()

    def is_active(self) -> bool:
        return self.mode() != "Off"

    def side_by_side_active(self) -> bool:
        return self.is_active() and self.display() == "Main Side-by-side"

    def main_overlay_active(self) -> bool:
        return self.is_active() and self.display() == "Main Overlay"

    def main_alpha(self) -> float:
        return self.main_alpha_slider.value() / 100.0

    def zero_flow_value(self) -> float:
        return self.zero_flow_slider.value() / 20.0

    # ---- control slots ----------------------------------------------------------

    def _on_mode_changed(self, _index: int | None = None) -> None:
        enabled = self.is_active()
        self.display_combo.setEnabled(enabled)
        self.main_alpha_label.setVisible(enabled)
        self.main_alpha_slider.setVisible(enabled)
        self._update_controls_state()
        self._update_output()
        self.refresh_view()

    def _on_display_changed(self, _index: int | None = None) -> None:
        self._update_output()
        self.refresh_view()

    def _update_output(self) -> None:
        """Show the view the display mode asks for: the side-by-side label,
        the popout, or neither (Main Overlay paints on the main picture)."""
        if not self.is_active():
            self._hide_popout()
            self.view_label.setVisible(False)
            self.main_alpha_label.setVisible(False)
            self.main_alpha_slider.setVisible(False)
            self.layout_changed.emit()
            return
        display = self.display()
        show_main_overlay = display == "Main Overlay"
        self.main_alpha_label.setVisible(show_main_overlay)
        self.main_alpha_slider.setVisible(show_main_overlay)
        if display == "Popout":
            self._show_popout()
            self.view_label.setVisible(False)
        elif display == "Main Side-by-side":
            self._hide_popout()
            self.view_label.setVisible(True)
        else:  # Main Overlay
            self._hide_popout()
            self.view_label.setVisible(False)
        self.layout_changed.emit()

    def _update_controls_state(self, _state: int | None = None) -> None:
        is_flow = self.mode() == "Optical Flow"
        is_main_overlay = self.display() == "Main Overlay"
        arrows = self.arrows_cb.isChecked()
        self.arrows_cb.setEnabled(is_flow)
        self.hide_zero_flow_cb.setEnabled(is_flow and arrows)
        zero_flow_enabled = is_flow and arrows and self.hide_zero_flow_cb.isChecked()
        self.zero_flow_label.setEnabled(zero_flow_enabled)
        self.zero_flow_slider.setEnabled(zero_flow_enabled)
        self.arrow_step_slider.setEnabled(is_flow and arrows)
        self.arrow_scale_slider.setEnabled(is_flow and arrows)
        self.scale_slider.setEnabled(is_flow)
        self.arrow_step_label.setEnabled(is_flow)
        self.arrow_scale_label.setEnabled(is_flow)
        self.scale_label.setEnabled(is_flow)
        self.overlay_cb.setEnabled(not is_main_overlay)
        self.alpha_slider.setEnabled(not is_main_overlay)
        self.alpha_label.setEnabled(not is_main_overlay)

    def _on_main_alpha_changed(self, v: int) -> None:
        self.main_alpha_label.setText(f"Overlay: {v / 100.0:.2f}")
        self.redraw_requested.emit()

    def set_reference(self) -> None:
        rgb, index = self._current_frame()
        if rgb is None:
            return
        # Safe to hold by reference: frame buffers are never mutated in place.
        self.ref_frame_rgb = rgb
        self.ref_frame_index = int(index) if index is not None else None
        self.refresh_view()

    def clear_reference(self) -> None:
        self.ref_frame_rgb = None
        self.ref_frame_index = None
        self.refresh_view()

    def _on_gain_changed(self, v: int) -> None:
        self.gain_label.setText(f"Gain: {v}x")
        self.refresh_view()

    def _on_thresh_changed(self, v: int) -> None:
        self.thresh_label.setText(f"Threshold / Min motion: {v}")
        self.refresh_view()

    def _on_alpha_changed(self, v: int) -> None:
        self.alpha_label.setText(f"Overlay alpha: {v / 100.0:.2f}")
        self.refresh_view()

    def _on_scale_changed(self, v: int) -> None:
        self.scale_label.setText(f"Compute scale: {v}%")
        self.refresh_view()

    def _on_arrow_step_changed(self, v: int) -> None:
        self.arrow_step_label.setText(f"Arrow step: {v} px")
        self.refresh_view()

    def _on_arrow_scale_changed(self, v: int) -> None:
        self.arrow_scale_label.setText(f"Arrow length scale: {v / 10.0:.1f}x")
        self.refresh_view()

    def _update_zero_flow_label(self) -> None:
        self.zero_flow_label.setText(f"Min flow: {self.zero_flow_value():.2f}")

    def _on_zero_flow_changed(self, _v: int) -> None:
        self._update_zero_flow_label()
        self.refresh_view()

    # ---- the popout -------------------------------------------------------------

    def _show_popout(self) -> None:
        if self._popout is None:
            win = QWidget(self, Qt.Window)
            win.setWindowTitle("Analysis View")
            win.resize(800, 450)
            layout = QVBoxLayout(win)
            layout.setContentsMargins(6, 6, 6, 6)
            label = VideoFrameLabel("Analysis view")
            label.setAlignment(Qt.AlignCenter)
            label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            layout.addWidget(label, 1)
            win.setLayout(layout)
            win.destroyed.connect(lambda _=None: self._clear_popout())
            self._popout = win
            self._popout_label = label
        self._popout.show()

    def _hide_popout(self) -> None:
        if self._popout is not None:
            self._popout.hide()

    def _clear_popout(self) -> None:
        self._popout = None
        self._popout_label = None

    def close_popout(self) -> None:
        if self._popout is not None:
            self._popout.close()

    # ---- computing and painting ---------------------------------------------------

    def _base_frame(self) -> tuple[np.ndarray | None, str]:
        if self.pair_combo.currentText().startswith("Reference"):
            if self.ref_frame_rgb is None:
                return None, "Set a reference frame first."
            label = "Reference frame"
            if self.ref_frame_index is not None:
                label += f": {self.ref_frame_index}"
            return self.ref_frame_rgb, label
        prev_rgb, prev_index = self._previous_frame()
        if prev_rgb is None:
            return None, "No previous frame yet (scrub at least once)."
        label = "Previous frame"
        if prev_index is not None:
            label += f": {prev_index}"
        return prev_rgb, label

    def compute_output(self) -> tuple[np.ndarray | None, str]:
        """The analysis picture for the current frame and its tooltip, or
        (None, why-not) when there is nothing to show."""
        mode = self.mode()
        if mode == "Off":
            return None, ""
        frame_rgb, frame_index = self._current_frame()
        if frame_rgb is None:
            return None, "Analysis view (no frame)"
        base_rgb, base_info = self._base_frame()
        if base_rgb is None:
            return None, f"Analysis view ({base_info})"
        if base_rgb.shape != frame_rgb.shape:
            h, w = frame_rgb.shape[:2]
            base_rgb = cv2.resize(base_rgb, (w, h), interpolation=cv2.INTER_AREA)
            base_info = f"{base_info} (resized)"

        gain = float(self.gain_slider.value())
        thresh = int(self.thresh_slider.value())
        heatmap = self.heatmap_cb.isChecked()
        overlay = self.overlay_cb.isChecked()
        if self.display() == "Main Overlay":
            overlay = False
        alpha = self.alpha_slider.value() / 100.0

        if mode == "Frame Diff":
            out_rgb = compute_pixel_diff_view(
                frame_rgb=frame_rgb,
                base_rgb=base_rgb,
                gain=gain,
                threshold=thresh,
                heatmap=heatmap,
                overlay=overlay,
                alpha=alpha,
            )
        else:
            out_rgb = compute_optical_flow_view(
                frame_rgb=frame_rgb,
                base_rgb=base_rgb,
                gain=gain,
                min_motion=thresh,
                heatmap=heatmap,
                overlay=overlay,
                alpha=alpha,
                arrows=self.arrows_cb.isChecked(),
                arrow_step=int(self.arrow_step_slider.value()),
                arrow_scale=float(self.arrow_scale_slider.value()) / 10.0,
                compute_scale=self.scale_slider.value() / 100.0,
                arrow_min_mag=self.zero_flow_value() if self.hide_zero_flow_cb.isChecked() else None,
            )
        tooltip = f"{mode}\n{base_info}\nCurrent frame: {frame_index}"
        return out_rgb, tooltip

    def main_overlay_image(self) -> QImage | None:
        """The current frame with the analysis blended over it at the main
        alpha, for the replay to paint instead of the plain frame; None
        when "Main Overlay" is off or there is nothing to blend."""
        if not self.main_overlay_active():
            return None
        out_rgb, _tooltip = self.compute_output()
        cur_rgb, _index = self._current_frame()
        if out_rgb is None or cur_rgb is None:
            return None
        alpha = self.main_alpha()
        try:
            return rgb_to_qimage(cv2.addWeighted(cur_rgb, 1.0 - alpha, out_rgb, alpha, 0.0))
        except Exception:
            return None

    @staticmethod
    def _show_in(
        label: VideoFrameLabel | None,
        image: QImage | None,
        text: str | None = None,
        tooltip: str | None = None,
    ) -> None:
        """Paint ``image`` on ``label`` (None clears it); ``text`` and
        ``tooltip`` are set only when given."""
        if label is None:
            return
        if text is not None:
            label.setText(text)
        if tooltip is not None:
            label.setToolTip(tooltip)
        label.set_frame(image)

    def clear_view(self) -> None:
        """Back to the empty "Analysis view" label (a new clip is loading)."""
        self._show_in(self.view_label, None, "Analysis view", "")

    def refresh_view(self, _state: int | None = None) -> None:
        """Recompute and paint the side-by-side view and the popout."""
        if not self.is_active():
            self._show_in(self.view_label, None, "Analysis view", "")
            self._show_in(self._popout_label, None, "Analysis view")
            return
        out_rgb, tooltip = self.compute_output()
        if out_rgb is None:
            msg = tooltip or "Analysis view"
            self._show_in(self.view_label, None, msg, "")
            self._show_in(self._popout_label, None, msg, "")
            return
        qimg = rgb_to_qimage(out_rgb)
        if self.display() == "Popout":
            self._show_in(self.view_label, None, tooltip="")
            self._show_in(self._popout_label, qimg, tooltip=tooltip)
        else:
            self._show_in(self.view_label, qimg, tooltip=tooltip)
            self._show_in(self._popout_label, None)
