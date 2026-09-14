"""The PikPak Replay's annotations: the per-clip and the pinned sets, their
JSON files, the drawing tool and colour, undo, and the right-click menu.

Split out of replay_view.py on 2026-09-14 (code review item 9). Three
layers:

- ``AnnotationStore`` - the files under the clip cache's ``annotations``
  folder: ``pinned.json`` (annotations shown on every clip) and one
  ``<cache stem>.json`` per clip. No Qt.
- ``AnnotationSet`` - the pinned / clip lists and the undo history, with
  the edits the menu and the toolbar make (add, undo, delete, pin across
  clips, pin to a frame, clear the clip). No Qt.
- ``ClipAnnotations`` - the QObject the replay talks to: it owns a set
  and a store, the current tool and colour, builds the Video Popout's
  toolbar, shows the context menu and the dialogs, and tells the replay
  what changed through ``changed`` / ``tool_changed`` / ``color_changed``
  / ``clip_status_changed``. The canvases (annotated_video_widget.py)
  stay where they are; the replay pushes the set into them on ``changed``.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QButtonGroup,
    QColorDialog,
    QHBoxLayout,
    QInputDialog,
    QMenu,
    QToolButton,
    QWidget,
)

from logfather.ui import theme

DEFAULT_TOOL = "line"
DEFAULT_COLOR = "#ffcc00"
PINNED_FILENAME = "pinned.json"

# The popout toolbar, in order: (annotation type, button label).
TOOLS: tuple[tuple[str, str], ...] = (
    ("line", "Line"),
    ("arrow", "Arrow"),
    ("text", "Text"),
    ("measure", "Measure"),
    ("timed_line", "Timed Line"),
    ("tray", "Bird's Eye"),
)
# The kinds "Edit annotation" can drag the points of.
EDITABLE_TYPES = ("line", "arrow", "measure", "tray")


# ---------------------------------------------------------------------------
# Files (no Qt)
# ---------------------------------------------------------------------------

def read_annotations(path: Path) -> list[dict]:
    """The dict items under ``"annotations"`` in ``path``; [] for a missing
    or unreadable file."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        items = data.get("annotations", [])
    except Exception:
        return []
    if not isinstance(items, list):
        return []
    return [i for i in items if isinstance(i, dict)]


def write_annotations(path: Path, items: list[dict]) -> bool:
    """Write ``{"annotations": items}``; False (and nothing raised) when
    the file cannot be written."""
    try:
        path.write_text(json.dumps({"annotations": items}, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def parse_distance(text: str) -> float | None:
    """The metres typed into "Set distance (m)": None when what is left
    after stripping stray characters is not a number. (The character
    class is the one the replay always used.)"""
    cleaned = re.sub(r"[^0-9.+-eE]", "", text)
    try:
        return float(cleaned)
    except ValueError:
        return None


class AnnotationStore:
    """The annotation files. ``annotations_dir`` is asked for the folder
    on every access (the clip cache creates it on demand)."""

    def __init__(self, annotations_dir: Callable[[], Path]):
        self._annotations_dir = annotations_dir

    @property
    def pinned_path(self) -> Path:
        return self._annotations_dir() / PINNED_FILENAME

    def clip_path(self, cache_path: Path) -> Path:
        """The clip's file, named after its cache copy so the share path
        and the cache copy share one entry."""
        return self._annotations_dir() / f"{cache_path.stem}.json"

    def load_pinned(self) -> list[dict]:
        return read_annotations(self.pinned_path)

    def save_pinned(self, items: list[dict]) -> bool:
        return write_annotations(self.pinned_path, items)

    def load_clip(self, cache_path: Path) -> list[dict]:
        return read_annotations(self.clip_path(cache_path))

    def save_clip(self, cache_path: Path, items: list[dict]) -> bool:
        return write_annotations(self.clip_path(cache_path), items)


# ---------------------------------------------------------------------------
# The set (no Qt)
# ---------------------------------------------------------------------------

@dataclass
class AnnotationSet:
    """Pinned annotations show on every clip and come first in ``all()``;
    clip annotations belong to the open clip. ``history`` is the undo
    stack: what was added this session (or everything loaded with the
    clip), most recent last."""

    pinned: list[dict] = field(default_factory=list)
    clip: list[dict] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)

    def all(self) -> list[dict]:
        return list(self.pinned) + list(self.clip)

    def set_clip(self, items: list[dict]) -> None:
        """A clip's annotations loaded: everything showing becomes undoable."""
        self.clip = list(items)
        self.history = self.all()

    def reset_clip(self) -> None:
        """No clip file: the clip set and the undo stack start empty."""
        self.clip = []
        self.history = []

    def add(self, ann: dict) -> None:
        (self.pinned if ann.get("pinned") else self.clip).append(ann)
        self.history.append(ann)

    def undo(self) -> dict | None:
        """Take back the most recent addition; the annotation, or None."""
        if not self.history:
            return None
        ann = self.history.pop()
        target = self.pinned if ann.get("pinned") else self.clip
        if ann in target:
            target.remove(ann)
        return ann

    def delete(self, ann: dict) -> None:
        if ann in self.pinned:
            self.pinned.remove(ann)
        if ann in self.clip:
            self.clip.remove(ann)
        while ann in self.history:
            self.history.remove(ann)

    def clear_clip(self) -> bool:
        """Drop the clip's annotations (pinned ones stay); False when there
        were none."""
        if not self.clip:
            return False
        self.clip = []
        self.history = [a for a in self.history if a.get("pinned")]
        return True

    def toggle_pin(self, ann: dict) -> None:
        """Move ``ann`` between the clip set and the pinned set."""
        if ann.get("pinned"):
            ann["pinned"] = False
            if ann in self.pinned:
                self.pinned.remove(ann)
            if ann not in self.clip:
                self.clip.append(ann)
        else:
            ann["pinned"] = True
            if ann in self.clip:
                self.clip.remove(ann)
            if ann not in self.pinned:
                self.pinned.append(ann)

    @staticmethod
    def toggle_frame_pin(ann: dict, frame_index: int) -> None:
        """Pin ``ann`` to ``frame_index`` (shown on that frame only), or
        unpin it when it is pinned there already."""
        if ann.get("frame_index") == frame_index:
            ann.pop("frame_index", None)
        else:
            ann["frame_index"] = frame_index

    @staticmethod
    def set_distance(ann: dict, text: str) -> None:
        """"Set distance (m)" for a timed line: empty text removes it, a
        number sets it, anything else stores None."""
        if text.strip():
            ann["distance_m"] = parse_distance(text)
        else:
            ann.pop("distance_m", None)


# ---------------------------------------------------------------------------
# Qt glue
# ---------------------------------------------------------------------------

@dataclass
class AnnotationToolbar:
    layout: QHBoxLayout
    color_button: QToolButton
    tool_group: QButtonGroup


class ClipAnnotations(QObject):
    changed = Signal()                          # the canvases must re-read annotations()
    tool_changed = Signal(str)
    color_changed = Signal(QColor)
    clip_status_changed = Signal(object, bool)  # (clip share path, has clip annotations)

    def __init__(
        self,
        store: AnnotationStore,
        *,
        dialog_parent: QWidget | None,
        current_frame: Callable[[], int],
        popout_label: Callable[[], object | None],
        parent: QObject | None = None,
    ):
        """``dialog_parent`` owns the menu and dialogs; ``current_frame``
        is the replay's playhead frame (for "pin to current frame");
        ``popout_label`` is the Video Popout's canvas when it is open (for
        "Edit annotation")."""
        super().__init__(parent)
        self.store = store
        self.items = AnnotationSet()
        self.tool = DEFAULT_TOOL
        self.color = QColor(DEFAULT_COLOR)
        self._dialog_parent = dialog_parent
        self._current_frame = current_frame
        self._popout_label = popout_label
        self._base_path: Path | None = None    # the clip's share path (for the status signal)
        self._cache_path: Path | None = None   # names the clip's file
        self._toolbar: AnnotationToolbar | None = None

    # ---- what is showing -------------------------------------------------------

    def annotations(self) -> list[dict]:
        return self.items.all()

    def has_clip_annotations(self) -> bool:
        return bool(self.items.clip)

    # ---- files -----------------------------------------------------------------

    def load_pinned(self) -> None:
        self.items.pinned = self.store.load_pinned()

    def load_clip(self, base_path: Path | None, cache_path: Path | None) -> None:
        """A clip opened: read its file (named after ``cache_path``) and
        show it. ``base_path`` is the share path the status signal names.
        With no path, or no file yet, the clip set starts empty."""
        self._base_path = base_path
        self._cache_path = cache_path
        if cache_path is None or not self.store.clip_path(cache_path).exists():
            self.items.reset_clip()
        else:
            self.items.set_clip(self.store.load_clip(cache_path))
        self.changed.emit()
        self._emit_clip_status()

    def clear_for_new_clip(self) -> None:
        """The replay is loading another clip: nothing to save to until
        ``load_clip`` names the new one."""
        self._base_path = None
        self._cache_path = None
        self.items.reset_clip()
        self.changed.emit()

    def save_clip(self) -> None:
        if self._cache_path is None:
            return
        self.store.save_clip(self._cache_path, self.items.clip)
        self._emit_clip_status()

    def save_pinned(self) -> None:
        self.store.save_pinned(self.items.pinned)

    def save(self) -> None:
        self.save_clip()
        self.save_pinned()

    def _emit_clip_status(self) -> None:
        if self._base_path is None:
            return
        self.clip_status_changed.emit(self._base_path, self.has_clip_annotations())

    def _save_and_refresh(self) -> None:
        self.save()
        self.changed.emit()

    # ---- edits -----------------------------------------------------------------

    def add(self, ann: dict) -> None:
        self.items.add(ann)
        self._save_and_refresh()

    def on_updated(self, _idx: int, _ann: dict) -> None:
        """The canvas moved an annotation's points."""
        self._save_and_refresh()

    def undo(self) -> None:
        if self.items.undo() is None:
            return
        self._save_and_refresh()

    def clear_clip(self) -> None:
        if not self.items.clear_clip():
            return
        self.save_clip()
        self.changed.emit()

    # ---- tool and colour ---------------------------------------------------------

    def set_tool(self, tool: str) -> None:
        self.tool = tool
        self.tool_changed.emit(tool)

    def set_color(self, color: QColor) -> None:
        self.color = QColor(color)
        if self._toolbar is not None:
            style_color_button(self._toolbar.color_button, self.color)
        self.color_changed.emit(self.color)

    def pick_color(self) -> None:
        color = QColorDialog.getColor(self.color, self._dialog_parent, "Select annotation color")
        if color.isValid():
            self.set_color(color)

    # ---- the popout toolbar --------------------------------------------------------

    def build_toolbar(self, parent: QWidget) -> AnnotationToolbar:
        """The tool buttons, Color, Undo and Clear Clip for the Video
        Popout; the colour button follows ``set_color`` until
        ``drop_toolbar``."""
        toolbar = QHBoxLayout()
        tool_group = QButtonGroup(parent)
        tool_group.setExclusive(True)
        for tool_key, label_text in TOOLS:
            btn = QToolButton()
            btn.setText(label_text)
            btn.setCheckable(True)
            btn.setChecked(self.tool == tool_key)
            btn.clicked.connect(lambda _checked, t=tool_key: self.set_tool(t))
            tool_group.addButton(btn)
            toolbar.addWidget(btn)
        color_btn = QToolButton()
        color_btn.setText("Color")
        color_btn.clicked.connect(self.pick_color)
        style_color_button(color_btn, self.color)
        toolbar.addWidget(color_btn)
        undo_btn = QToolButton()
        undo_btn.setText("Undo")
        undo_btn.clicked.connect(self.undo)
        toolbar.addWidget(undo_btn)
        clear_btn = QToolButton()
        clear_btn.setText("Clear Clip")
        clear_btn.clicked.connect(self.clear_clip)
        toolbar.addWidget(clear_btn)
        toolbar.addStretch(1)
        self._toolbar = AnnotationToolbar(toolbar, color_btn, tool_group)
        return self._toolbar

    def drop_toolbar(self) -> None:
        """The popout was destroyed with its toolbar."""
        self._toolbar = None

    # ---- the context menu ------------------------------------------------------------

    def show_context_menu(self, idx: int, global_pos) -> None:
        annotations = self.annotations()
        if idx < 0 or idx >= len(annotations):
            return
        ann = annotations[idx]
        menu = QMenu(self._dialog_parent)
        edit_action = menu.addAction("Edit annotation")
        pin_action = menu.addAction("Toggle pin across clips")
        frame_action = menu.addAction("Toggle pin to current frame")
        distance_action = None
        if ann.get("type") == "timed_line":
            distance_action = menu.addAction("Set distance (m)")
        delete_action = menu.addAction("Delete annotation")
        chosen = menu.exec(global_pos.toPoint())
        if chosen == edit_action:
            if ann.get("type") in EDITABLE_TYPES:
                label = self._popout_label()
                if label is not None:
                    current = label.get_edit_index()
                    label.set_edit_index(None if current == idx else idx)
        elif chosen == pin_action:
            self.items.toggle_pin(ann)
            self._save_and_refresh()
        elif chosen == frame_action:
            self.items.toggle_frame_pin(ann, self._current_frame())
            self._save_and_refresh()
        elif distance_action is not None and chosen == distance_action:
            current = ann.get("distance_m")
            text, ok = QInputDialog.getText(
                self._dialog_parent,
                "Set distance (m)",
                "Distance in meters:",
                text="" if current is None else str(current),
            )
            if ok:
                self.items.set_distance(ann, text)
            self._save_and_refresh()
        elif chosen == delete_action:
            self.items.delete(ann)
            label = self._popout_label()
            if label is not None:
                label.set_edit_index(None)
            self._save_and_refresh()


def style_color_button(button: QToolButton, color: QColor) -> None:
    button.setStyleSheet(theme.solid_button(color.name()))
