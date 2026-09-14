"""ui/pane_animator.py: one splitter pane sliding open and closed, offscreen."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt, QVariantAnimation
from PySide6.QtWidgets import QApplication, QHBoxLayout, QSplitter, QWidget

from logfather.ui.pane_animator import PaneAnimator


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def splitter(app):
    """A laid-out three-pane splitter (left | centre | right), 1000 px wide."""
    s = QSplitter(Qt.Horizontal)
    for _ in range(3):
        s.addWidget(QWidget())
    s.resize(1000, 200)
    s.show()
    app.processEvents()
    s.setSizes([100, 800, 100])
    yield s
    s.close()
    s.deleteLater()


def finish(animator: PaneAnimator) -> None:
    """Drive the running slide to its end by hand (no event loop)."""
    animator.animation.setCurrentTime(animator.animation.duration())


# --- animate_to -----------------------------------------------------------

def test_zero_duration_slide_lands_on_target_and_centre_absorbs(splitter):
    left0, _centre0, _right0 = splitter.sizes()
    anim = PaneAnimator(splitter, 2, duration_ms=0)
    anim.animate_to(300)
    assert not anim.is_running()
    left, centre, right = splitter.sizes()
    assert right == 300
    assert left == left0  # the far pane keeps its share
    assert centre == pytest.approx(600, abs=splitter.handleWidth() * 2)


def test_steps_follow_the_animation_value(splitter):
    right0 = splitter.sizes()[2]
    anim = PaneAnimator(splitter, 2)
    anim.animate_to(400)
    assert anim.is_running()
    assert anim.animation.startValue() == right0
    assert anim.animation.endValue() == 400
    anim.animation.setCurrentTime(anim.animation.duration() // 2)
    mid = splitter.sizes()[2]
    assert right0 < mid < 400
    finish(anim)
    assert splitter.sizes()[2] == 400
    assert not anim.is_running()


def test_left_pane_default_absorb_is_the_centre(splitter):
    right0 = splitter.sizes()[2]
    anim = PaneAnimator(splitter, 0, duration_ms=0)
    anim.animate_to(250)
    left, centre, right = splitter.sizes()
    assert left == 250
    assert right == right0
    assert centre == pytest.approx(650, abs=splitter.handleWidth() * 2)


# --- show / hide of the pane's widget ---------------------------------------

def test_hide_slides_closed_then_hides_the_widget(splitter):
    pane = splitter.widget(2)
    anim = PaneAnimator(splitter, 2, widget=pane)
    anim.hide()
    assert anim.is_running()
    assert pane.isVisible(), "hidden only once the slide has finished"
    finish(anim)
    assert splitter.sizes()[2] == 0
    assert not pane.isVisible()


def test_show_to_shows_the_widget_before_sliding_open(splitter):
    pane = splitter.widget(2)
    anim = PaneAnimator(splitter, 2, widget=pane, duration_ms=0)
    anim.hide()
    assert not pane.isVisible()
    anim.show_to(200)
    assert pane.isVisible()
    assert splitter.sizes()[2] == 200


def test_hide_when_already_closed_hides_immediately_without_a_slide(splitter):
    pane = splitter.widget(2)
    splitter.setSizes([100, 900, 0])
    anim = PaneAnimator(splitter, 2, widget=pane)
    anim.hide()
    assert not anim.is_running()
    assert not pane.isVisible()


def test_stop_mid_slide_does_not_hide(splitter):
    pane = splitter.widget(2)
    anim = PaneAnimator(splitter, 2, widget=pane)
    anim.hide()
    anim.animation.setCurrentTime(40)
    anim.stop()
    assert not anim.is_running()
    assert pane.isVisible()
    assert 0 < splitter.sizes()[2] < 99


# --- a second slide mid-run ---------------------------------------------------

def test_second_slide_mid_run_stops_the_first_and_continues_from_here(splitter):
    pane = splitter.widget(2)
    anim = PaneAnimator(splitter, 2, widget=pane)
    finished = []
    anim.animation.finished.connect(lambda: finished.append(True))
    right0 = splitter.sizes()[2]
    anim.animate_to(400)
    anim.animation.setCurrentTime(60)
    mid = splitter.sizes()[2]
    assert right0 < mid < 400
    anim.hide()  # reverses direction from wherever the pane is now
    assert anim.is_running()
    assert anim.animation.startValue() == mid
    assert anim.animation.endValue() == 0
    assert finished == [], "stopping the first slide is not a finish"
    finish(anim)
    assert splitter.sizes()[2] == 0
    assert not pane.isVisible()
    assert finished == [True]


# --- clamping rules -----------------------------------------------------------

def test_cap_to_total_keeps_the_absorbing_pane_at_its_minimum(app):
    s = QSplitter(Qt.Vertical)
    s.addWidget(QWidget())
    s.addWidget(QWidget())
    s.resize(300, 500)
    s.show()
    app.processEvents()
    s.setSizes([400, 100])
    total = sum(s.sizes())
    anim = PaneAnimator(s, 1, cap_to_total=True, duration_ms=0)
    anim.animate_to(10_000)
    top, bottom = s.sizes()
    assert bottom == total - 1
    assert top == 1
    assert anim.animation.endValue() == total - 1
    s.close()
    s.deleteLater()


def test_fallback_total_is_used_when_the_splitter_has_no_size(app):
    s = QSplitter(Qt.Horizontal)
    for _ in range(3):
        s.addWidget(QWidget())
    assert sum(s.sizes()) == 0  # never laid out
    asked = []

    def fallback(px):
        asked.append(px)
        return 1400

    anim = PaneAnimator(s, 0, fallback_total=fallback, duration_ms=0)
    anim.animate_to(380)
    assert asked == [380]
    s.deleteLater()


def test_missing_pane_is_a_no_op(app):
    s = QSplitter(Qt.Horizontal)
    s.addWidget(QWidget())
    anim = PaneAnimator(s, 2, duration_ms=0)
    anim.animate_to(100)
    assert not anim.is_running()
    s.deleteLater()


# --- for_width: a column in a plain layout ----------------------------------

@pytest.fixture
def column(app):
    host = QWidget()
    layout = QHBoxLayout(host)
    centre, column = QWidget(), QWidget()
    layout.addWidget(centre, 1)
    layout.addWidget(column, 0)
    column.setMinimumWidth(300)
    column.setMaximumWidth(300)
    host.resize(1200, 200)
    host.show()
    app.processEvents()
    yield column
    host.close()
    host.deleteLater()


def test_for_width_pins_min_and_max_width(column):
    anim = PaneAnimator.for_width(column, duration_ms=0)
    assert column.width() == 300
    anim.animate_to(120)
    assert (column.minimumWidth(), column.maximumWidth()) == (120, 120)
    anim.animate_to(0)
    assert (column.minimumWidth(), column.maximumWidth()) == (0, 0)


def test_snap_to_jumps_without_a_slide(column):
    anim = PaneAnimator.for_width(column)
    anim.animate_to(0)
    assert anim.is_running()
    anim.snap_to(600)
    assert not anim.is_running()
    assert (column.minimumWidth(), column.maximumWidth()) == (600, 600)


def test_duration_and_easing_are_applied(app):
    from PySide6.QtCore import QEasingCurve

    anim = PaneAnimator(None, duration_ms=250, easing=QEasingCurve.InOutQuad)
    assert anim.animation.duration() == 250
    assert anim.animation.easingCurve().type() == QEasingCurve.InOutQuad
    assert anim.animation.state() == QVariantAnimation.Stopped
