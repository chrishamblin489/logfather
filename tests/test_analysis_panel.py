"""ui/analysis_panel.py: the replay's Analysis controls driven offscreen
with synthetic frames (no clip, no window)."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from logfather.ui.analysis_panel import DISPLAYS, MODES, PAIRINGS, AnalysisPanel


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class Frames:
    """Stand-in for the replay's frame buffers: a dark frame and the same
    frame with a bright block moved, so differencing has something to see."""

    def __init__(self):
        base = np.zeros((48, 64, 3), dtype=np.uint8)
        base[10:20, 10:20] = 200
        moved = np.zeros((48, 64, 3), dtype=np.uint8)
        moved[10:20, 30:40] = 200
        self.previous = base
        self.current = moved
        self.current_index = 7
        self.previous_index = 6

    def current_frame(self):
        return self.current, self.current_index

    def previous_frame(self):
        return self.previous, self.previous_index


@pytest.fixture
def frames():
    return Frames()


@pytest.fixture
def panel(app, frames):
    p = AnalysisPanel(frames.current_frame, frames.previous_frame)
    yield p
    p.close_popout()
    p.close()
    p.deleteLater()


# --- defaults --------------------------------------------------------------

def test_controls_and_defaults_are_the_originals(panel):
    assert [panel.mode_combo.itemText(i) for i in range(panel.mode_combo.count())] == list(MODES)
    assert [panel.display_combo.itemText(i) for i in range(panel.display_combo.count())] == list(DISPLAYS)
    assert [panel.pair_combo.itemText(i) for i in range(panel.pair_combo.count())] == list(PAIRINGS)
    assert panel.mode() == "Off" and panel.display() == "Main Overlay"
    assert panel.heatmap_cb.isChecked() and not panel.overlay_cb.isChecked()
    assert not panel.arrows_cb.isChecked() and panel.hide_zero_flow_cb.isChecked()
    assert (panel.gain_slider.minimum(), panel.gain_slider.maximum(), panel.gain_slider.value()) == (1, 30, 6)
    assert (panel.thresh_slider.minimum(), panel.thresh_slider.maximum(), panel.thresh_slider.value()) == (0, 255, 15)
    assert (panel.alpha_slider.minimum(), panel.alpha_slider.maximum(), panel.alpha_slider.value()) == (0, 100, 60)
    assert (panel.scale_slider.minimum(), panel.scale_slider.maximum(), panel.scale_slider.value()) == (25, 100, 100)
    assert (panel.arrow_step_slider.minimum(), panel.arrow_step_slider.maximum(), panel.arrow_step_slider.value()) == (8, 60, 20)
    assert (panel.arrow_scale_slider.minimum(), panel.arrow_scale_slider.maximum(), panel.arrow_scale_slider.value()) == (5, 50, 15)
    assert (panel.zero_flow_slider.minimum(), panel.zero_flow_slider.maximum(), panel.zero_flow_slider.value()) == (0, 100, 1)
    assert panel.zero_flow_label.text() == "Min flow: 0.05"
    assert panel.gain_label.text() == "Gain: 6x"
    assert panel.thresh_label.text() == "Threshold / Min motion: 15"
    assert panel.alpha_label.text() == "Overlay alpha: 0.60"
    assert panel.scale_label.text() == "Compute scale: 100%"
    assert panel.arrow_step_label.text() == "Arrow step: 20 px"
    assert panel.arrow_scale_label.text() == "Arrow length scale: 1.5x"
    assert panel.main_alpha_label.text() == "Overlay: 0.60"
    assert panel.main_alpha_slider.value() == 60 and panel.main_alpha() == 0.6
    assert panel.maximumWidth() == 330
    assert panel.view_label.text() == "Analysis view" and panel.view_label.isHidden()


def test_off_mode_disables_display_and_hides_main_alpha(panel):
    # As before the split: the display combo is only disabled once the mode
    # has changed to Off, not at construction.
    assert panel.display_combo.isEnabled()
    panel.mode_combo.setCurrentText("Frame Diff")
    assert panel.display_combo.isEnabled()
    assert not panel.main_alpha_label.isHidden() and not panel.main_alpha_slider.isHidden()
    panel.mode_combo.setCurrentText("Off")
    assert not panel.display_combo.isEnabled()
    assert panel.main_alpha_label.isHidden() and panel.main_alpha_slider.isHidden()
    assert not panel.is_active()


def test_slider_labels_follow_values(panel):
    panel.gain_slider.setValue(12)
    panel.thresh_slider.setValue(40)
    panel.alpha_slider.setValue(25)
    panel.scale_slider.setValue(50)
    panel.arrow_step_slider.setValue(30)
    panel.arrow_scale_slider.setValue(22)
    panel.zero_flow_slider.setValue(40)
    panel.main_alpha_slider.setValue(80)
    assert panel.gain_label.text() == "Gain: 12x"
    assert panel.thresh_label.text() == "Threshold / Min motion: 40"
    assert panel.alpha_label.text() == "Overlay alpha: 0.25"
    assert panel.scale_label.text() == "Compute scale: 50%"
    assert panel.arrow_step_label.text() == "Arrow step: 30 px"
    assert panel.arrow_scale_label.text() == "Arrow length scale: 2.2x"
    assert panel.zero_flow_label.text() == "Min flow: 2.00"
    assert panel.main_alpha_label.text() == "Overlay: 0.80"


def test_flow_only_controls_follow_mode_and_arrows(panel):
    panel.mode_combo.setCurrentText("Frame Diff")
    assert not panel.arrows_cb.isEnabled() and not panel.scale_slider.isEnabled()
    panel.mode_combo.setCurrentText("Optical Flow")
    assert panel.arrows_cb.isEnabled() and panel.scale_slider.isEnabled()
    assert not panel.arrow_step_slider.isEnabled()  # arrows off
    panel.arrows_cb.setChecked(True)
    assert panel.arrow_step_slider.isEnabled() and panel.hide_zero_flow_cb.isEnabled()
    assert panel.zero_flow_slider.isEnabled()  # hide-zero-flow is on by default
    panel.hide_zero_flow_cb.setChecked(False)
    assert not panel.zero_flow_slider.isEnabled()


def test_main_overlay_display_disables_the_view_overlay_controls(panel):
    panel.mode_combo.setCurrentText("Frame Diff")
    assert panel.display() == "Main Overlay"
    assert not panel.overlay_cb.isEnabled() and not panel.alpha_slider.isEnabled()
    # As before the split, a display change alone does not re-evaluate the
    # enabled states; the next mode change does.
    panel.display_combo.setCurrentText("Main Side-by-side")
    assert not panel.overlay_cb.isEnabled()
    panel.mode_combo.setCurrentText("Optical Flow")
    assert panel.overlay_cb.isEnabled() and panel.alpha_slider.isEnabled()


# --- computing -------------------------------------------------------------

def test_off_computes_nothing(panel):
    assert panel.compute_output() == (None, "")
    assert panel.main_overlay_image() is None


def test_reference_pairing_needs_a_reference(panel):
    panel.mode_combo.setCurrentText("Frame Diff")
    out, why = panel.compute_output()
    assert out is None and why == "Analysis view (Set a reference frame first.)"
    panel.set_reference()
    assert panel.ref_frame_index == 7
    out, tooltip = panel.compute_output()
    assert out is not None and out.shape == (48, 64, 3)
    assert tooltip == "Frame Diff\nReference frame: 7\nCurrent frame: 7"
    panel.clear_reference()
    assert panel.ref_frame_rgb is None and panel.compute_output()[0] is None


def test_previous_pairing_differences_the_frame_pair(panel, frames):
    panel.mode_combo.setCurrentText("Frame Diff")
    panel.pair_combo.setCurrentText("Previous -> Current")
    panel.heatmap_cb.setChecked(False)  # grey: zero difference is black
    out, tooltip = panel.compute_output()
    assert out is not None and out.shape == (48, 64, 3)
    assert tooltip == "Frame Diff\nPrevious frame: 6\nCurrent frame: 7"
    # The moved block shows up as difference in both its old and new place.
    assert out[15, 15].any() and out[15, 35].any()
    assert not out[40, 60].any()


def test_no_previous_frame_reports_why(panel, frames):
    frames.previous = None
    panel.mode_combo.setCurrentText("Frame Diff")
    panel.pair_combo.setCurrentText("Previous -> Current")
    assert panel.compute_output() == (None, "Analysis view (No previous frame yet (scrub at least once).)")


def test_no_current_frame_reports_why(panel, frames):
    frames.current = None
    panel.mode_combo.setCurrentText("Frame Diff")
    assert panel.compute_output() == (None, "Analysis view (no frame)")


def test_mismatched_reference_is_resized_and_said_so(panel, frames):
    panel.mode_combo.setCurrentText("Frame Diff")
    panel.set_reference()
    frames.current = np.zeros((24, 32, 3), dtype=np.uint8)
    out, tooltip = panel.compute_output()
    assert out.shape == (24, 32, 3)
    assert "Reference frame: 7 (resized)" in tooltip


def test_optical_flow_runs_with_arrows(panel):
    panel.mode_combo.setCurrentText("Optical Flow")
    panel.pair_combo.setCurrentText("Previous -> Current")
    panel.arrows_cb.setChecked(True)
    panel.scale_slider.setValue(50)
    out, tooltip = panel.compute_output()
    assert out is not None and out.shape == (48, 64, 3)
    assert tooltip.startswith("Optical Flow\nPrevious frame: 6")


# --- the views ---------------------------------------------------------------

def test_main_overlay_blends_at_the_main_alpha(panel):
    panel.mode_combo.setCurrentText("Frame Diff")
    panel.pair_combo.setCurrentText("Previous -> Current")
    assert panel.main_overlay_active()
    image = panel.main_overlay_image()
    assert image is not None and (image.width(), image.height()) == (64, 48)
    panel.display_combo.setCurrentText("Popout")
    assert panel.main_overlay_image() is None


def test_side_by_side_shows_the_view_label_and_signals_layout(panel):
    seen = []
    panel.layout_changed.connect(lambda: seen.append(True))
    panel.mode_combo.setCurrentText("Frame Diff")
    panel.pair_combo.setCurrentText("Previous -> Current")
    panel.display_combo.setCurrentText("Main Side-by-side")
    assert panel.side_by_side_active() and not panel.view_label.isHidden()
    assert seen
    assert panel.view_label.toolTip().startswith("Frame Diff")
    panel.clear_view()
    assert panel.view_label.text() == "Analysis view" and panel.view_label.toolTip() == ""


def test_popout_display_opens_the_analysis_view_window(panel):
    assert panel.popout_window is None
    panel.mode_combo.setCurrentText("Frame Diff")
    panel.display_combo.setCurrentText("Popout")
    assert panel.popout_window is not None
    assert panel.popout_window.windowTitle() == "Analysis View"
    assert panel.view_label.isHidden()
    panel.display_combo.setCurrentText("Main Side-by-side")
    assert panel.popout_window.isHidden()  # hidden, not destroyed


def test_main_alpha_slider_asks_for_a_redraw(panel):
    seen = []
    panel.redraw_requested.connect(lambda: seen.append(True))
    panel.main_alpha_slider.setValue(30)
    assert seen and panel.main_alpha() == 0.3
