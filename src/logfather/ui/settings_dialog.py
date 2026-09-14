from __future__ import annotations

import base64
from pathlib import Path

from PySide6.QtCore import Qt, QByteArray, QBuffer, QIODevice, Signal
from PySide6.QtGui import QGuiApplication, QImage, QPixmap, QIcon
from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QFormLayout,
    QLineEdit,
    QDialogButtonBox,
    QPushButton,
    QHBoxLayout,
    QLabel,
    QGridLayout,
    QCheckBox,
    QWidget,
    QTabWidget,
    QPlainTextEdit,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QComboBox,
    QScrollArea,
)

from logfather.data.settings_store import Settings, Condition, DEFAULT_COLORS, SystemLayoutEntry
from logfather.ui import theme
from logfather.core.app_version import format_version_label
from logfather.paths import REPO_ROOT, SRC_ROOT


class SettingsPanel(QWidget):
    changed = Signal()
    save_requested = Signal()

    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.settings = settings

        # The CCTV share, Elastic and Grafana fields live in the Data sources
        # dialog (gear menu) since 2026-09-07; this panel keeps the rest.
        self.auto_ocr_sync_checkbox = QCheckBox("Auto-sync logs using OCR")
        self.auto_ocr_sync_checkbox.setChecked(bool(settings.auto_ocr_sync))
        # Single OCR toggle: auto-sync. Auto-open is tied to the same setting.

        form = QFormLayout()
        form.addRow("", self.auto_ocr_sync_checkbox)

        # Conditions grid
        self.condition_name_edits = []
        self.condition_query_edits = []
        cond_container = QWidget()
        cond_grid = QGridLayout(cond_container)
        cond_grid.setContentsMargins(0, 0, 0, 0)
        cond_grid.addWidget(QLabel("Name"), 0, 0)
        cond_grid.addWidget(QLabel("Search string"), 0, 1)
        total_rows = len(settings.conditions) if settings.conditions else 10
        for i in range(total_rows):
            cond = settings.conditions[i] if i < len(settings.conditions) else Condition()
            name_edit = QLineEdit(cond.name)
            query_edit = QLineEdit(cond.query)
            cond_grid.addWidget(name_edit, i + 1, 0)
            cond_grid.addWidget(query_edit, i + 1, 1)
            self.condition_name_edits.append(name_edit)
            self.condition_query_edits.append(query_edit)

        layout = QVBoxLayout()
        layout.addLayout(form)
        layout.addWidget(QLabel("Elastic search conditions (name + search string):"))
        condition_scroll = QScrollArea()
        condition_scroll.setWidgetResizable(True)
        condition_scroll.setFrameShape(QScrollArea.NoFrame)
        condition_scroll.setWidget(cond_container)
        condition_scroll.setMinimumHeight(300)
        layout.addWidget(condition_scroll, 1)
        self.setLayout(layout)
        self.auto_ocr_sync_checkbox.toggled.connect(self.changed.emit)
        for edit in self.condition_name_edits:
            edit.textChanged.connect(self.changed.emit)
        for edit in self.condition_query_edits:
            edit.textChanged.connect(self.changed.emit)

    def reload_from_settings(self):
        # Refresh all editable widgets from self.settings without firing change
        # signals (so we don't trigger an autosave loop).
        widgets = [
            self.auto_ocr_sync_checkbox,
            *self.condition_name_edits,
            *self.condition_query_edits,
        ]
        for w in widgets:
            w.blockSignals(True)
        try:
            self.auto_ocr_sync_checkbox.setChecked(bool(self.settings.auto_ocr_sync))
            for i, (name_edit, query_edit) in enumerate(zip(self.condition_name_edits, self.condition_query_edits)):
                cond = self.settings.conditions[i] if i < len(self.settings.conditions) else Condition()
                name_edit.setText(cond.name)
                query_edit.setText(cond.query)
        finally:
            for w in widgets:
                w.blockSignals(False)

    def apply_to(self, settings: Settings):
        # last_parent / Elastic / Grafana are owned by the Data sources
        # dialog and left untouched here.
        settings.auto_ocr_sync = bool(self.auto_ocr_sync_checkbox.isChecked())
        settings.auto_ocr_open_on_missing = settings.auto_ocr_sync

        new_conditions = []
        for name_edit, query_edit in zip(self.condition_name_edits, self.condition_query_edits):
            idx = len(new_conditions)
            # Keep the slot's colour (the loader enforces the key ones);
            # resetting to the slot default flipped colours on every apply
            # (2026-09-11).
            existing = settings.conditions[idx] if idx < len(settings.conditions) else None
            color = (existing.color if existing is not None and existing.color else "") or DEFAULT_COLORS[idx % len(DEFAULT_COLORS)]
            new_conditions.append(Condition(name=name_edit.text().strip(), query=query_edit.text().strip(), color=color))
        # Ensure length equals defaults
        target_len = len(DEFAULT_COLORS)
        while len(new_conditions) < target_len:
            idx = len(new_conditions)
            color = DEFAULT_COLORS[idx % len(DEFAULT_COLORS)]
            new_conditions.append(Condition(color=color))
        settings.conditions = new_conditions[:target_len]


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.settings = settings

        self.panel = SettingsPanel(settings, self)
        self.system_panel = SystemLayoutPanel(settings, self)
        self.readme_panel = ReadmePanel(self)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout()
        tabs = QTabWidget(self)
        tabs.addTab(self.panel, "Settings")
        tabs.addTab(self.system_panel, "Systems")
        tabs.addTab(self.readme_panel, "Readme")
        layout.addWidget(tabs)
        layout.addWidget(buttons)
        self.setLayout(layout)

    def apply(self):
        self.panel.apply_to(self.settings)
        self.system_panel.apply_to(self.settings)


class SystemLayoutPanel(QWidget):
    changed = Signal()

    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.customer_table = QTableWidget(0, 3, self)
        self.customer_table.setHorizontalHeaderLabels(["Customer", "Start Collapsed", "Logo"])
        self.customer_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.customer_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.customer_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.customer_table.verticalHeader().setVisible(False)
        self.customer_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.system_table = QTableWidget(0, 3, self)
        self.system_table.setHorizontalHeaderLabels(["PikPak", "Customer", "Production Line"])
        self.system_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.system_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.system_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.system_table.verticalHeader().setVisible(False)
        add_customer_btn = QPushButton("Add Customer")
        add_customer_btn.clicked.connect(self.add_customer_row)
        remove_customer_btn = QPushButton("Remove Customer")
        remove_customer_btn.clicked.connect(self.remove_selected_customer_row)
        paste_logo_btn = QPushButton("Paste Logo From Clipboard")
        paste_logo_btn.clicked.connect(self.paste_logo_for_selected_customer)
        clear_logo_btn = QPushButton("Clear Logo")
        clear_logo_btn.clicked.connect(self.clear_logo_for_selected_customer)
        refresh_btn = QPushButton("Reload Systems From Parent")
        refresh_btn.clicked.connect(self.populate_system_rows)
        self.logo_preview = QLabel("No logo selected")
        self.logo_preview.setAlignment(Qt.AlignCenter)
        self.logo_preview.setMinimumHeight(72)
        self.logo_preview.setStyleSheet(theme.LOGO_PREVIEW)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Customers"))
        customer_buttons = QHBoxLayout()
        customer_buttons.addWidget(add_customer_btn)
        customer_buttons.addWidget(remove_customer_btn)
        customer_buttons.addWidget(paste_logo_btn)
        customer_buttons.addWidget(clear_logo_btn)
        customer_buttons.addStretch(1)
        layout.addLayout(customer_buttons)
        layout.addWidget(self.customer_table)
        layout.addWidget(self.logo_preview)
        layout.addWidget(refresh_btn)
        layout.addWidget(self.system_table, 1)
        self.populate_customer_rows()
        self.populate_system_rows()
        self.customer_table.itemChanged.connect(lambda *_args: self._refresh_customer_dropdowns())
        self.customer_table.itemChanged.connect(lambda *_args: self.changed.emit())
        self.customer_table.currentCellChanged.connect(lambda *_args: self._update_logo_preview())
        self.system_table.itemChanged.connect(lambda *_args: self.changed.emit())

    def _known_system_names(self) -> list[str]:
        names = {entry.system_name.strip() for entry in self.settings.system_layouts if entry.system_name.strip()}
        parent = Path(self.settings.last_parent) if self.settings.last_parent else None
        if parent and parent.exists():
            try:
                for child in parent.iterdir():
                    if child.is_dir():
                        names.add(child.name)
            except Exception:
                pass
        return sorted(names, key=lambda value: value.lower())

    def _customer_names(self) -> list[str]:
        names = []
        for row in range(self.customer_table.rowCount()):
            item = self.customer_table.item(row, 0)
            if item is None:
                continue
            value = item.text().strip()
            if value and value not in names:
                names.append(value)
        return names

    def populate_customer_rows(self):
        self.customer_table.blockSignals(True)
        names = list(self.settings.customers)
        logos = dict(getattr(self.settings, "customer_logos", {}) or {})
        collapsed = dict(getattr(self.settings, "customer_start_collapsed", {}) or {})
        for name in logos.keys():
            if name not in names:
                names.append(name)
        for name in collapsed.keys():
            if name not in names:
                names.append(name)
        names.sort(key=lambda value: value.lower())
        self.customer_table.setRowCount(len(names))
        for row, name in enumerate(names):
            self.customer_table.setItem(row, 0, QTableWidgetItem(name))
            collapsed_item = QTableWidgetItem("")
            collapsed_item.setFlags((collapsed_item.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsEnabled) & ~Qt.ItemIsEditable)
            collapsed_item.setCheckState(Qt.Checked if collapsed.get(name, False) else Qt.Unchecked)
            self.customer_table.setItem(row, 1, collapsed_item)
            self._set_logo_data(row, logos.get(name, ""))
        self.customer_table.blockSignals(False)
        self._refresh_customer_dropdowns()

    def add_customer_row(self):
        row = self.customer_table.rowCount()
        self.customer_table.insertRow(row)
        self.customer_table.setItem(row, 0, QTableWidgetItem(""))
        collapsed_item = QTableWidgetItem("")
        collapsed_item.setFlags((collapsed_item.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsEnabled) & ~Qt.ItemIsEditable)
        collapsed_item.setCheckState(Qt.Unchecked)
        self.customer_table.setItem(row, 1, collapsed_item)
        self._set_logo_data(row, "")
        self.customer_table.setCurrentCell(row, 0)
        self._refresh_customer_dropdowns()
        self.changed.emit()

    def remove_selected_customer_row(self):
        row = self.customer_table.currentRow()
        if row < 0:
            return
        self.customer_table.removeRow(row)
        self._refresh_customer_dropdowns()
        self.changed.emit()

    def _selected_customer_name(self) -> str:
        row = self.customer_table.currentRow()
        if row < 0:
            return ""
        item = self.customer_table.item(row, 0)
        return item.text().strip() if item else ""

    def paste_logo_for_selected_customer(self):
        row = self.customer_table.currentRow()
        if row < 0:
            return
        clipboard = QGuiApplication.clipboard()
        image = clipboard.image()
        if image.isNull():
            pixmap = clipboard.pixmap()
            if not pixmap.isNull():
                image = pixmap.toImage()
        if image.isNull():
            return
        byte_array = QByteArray()
        buffer = QBuffer(byte_array)
        if not buffer.open(QIODevice.WriteOnly):
            return
        ok = image.save(buffer, "PNG")
        buffer.close()
        if not ok:
            return
        encoded = base64.b64encode(bytes(byte_array)).decode("ascii")
        self._set_logo_data(row, encoded)
        self._update_logo_preview()
        self.changed.emit()

    def clear_logo_for_selected_customer(self):
        row = self.customer_table.currentRow()
        if row < 0:
            return
        self._set_logo_data(row, "")
        self._update_logo_preview()
        self.changed.emit()

    def _logo_png_base64(self, row: int) -> str:
        item = self.customer_table.item(row, 2)
        if item is None:
            return ""
        data = item.data(Qt.UserRole)
        if isinstance(data, str):
            return data
        return ""

    def _set_logo_data(self, row: int, encoded_png: str):
        item = self.customer_table.item(row, 2)
        if item is None:
            item = QTableWidgetItem("")
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            self.customer_table.setItem(row, 2, item)
        else:
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
        item.setData(Qt.UserRole, encoded_png)
        item.setText("Logo" if encoded_png else "")
        item.setIcon(QIcon())
        if encoded_png:
            image = QImage.fromData(base64.b64decode(encoded_png), "PNG")
            if not image.isNull():
                pixmap = QPixmap.fromImage(image).scaled(48, 20, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                item.setIcon(QIcon(pixmap))

    def _update_logo_preview(self):
        row = self.customer_table.currentRow()
        if row < 0:
            self.logo_preview.setPixmap(QPixmap())
            self.logo_preview.setText("No logo selected")
            return
        encoded = self._logo_png_base64(row)
        if not encoded:
            self.logo_preview.setPixmap(QPixmap())
            self.logo_preview.setText("No logo selected")
            return
        image = QImage.fromData(base64.b64decode(encoded), "PNG")
        if image.isNull():
            self.logo_preview.setPixmap(QPixmap())
            self.logo_preview.setText("Invalid logo")
            return
        pixmap = QPixmap.fromImage(image).scaled(220, 64, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.logo_preview.setText("")
        self.logo_preview.setPixmap(pixmap)

    def _refresh_customer_dropdowns(self):
        customer_names = self._customer_names()
        for row in range(self.system_table.rowCount()):
            combo = self.system_table.cellWidget(row, 1)
            current_value = combo.currentText().strip() if isinstance(combo, QComboBox) else ""
            if not isinstance(combo, QComboBox):
                combo = QComboBox(self.system_table)
                combo.setEditable(False)
                combo.currentIndexChanged.connect(lambda *_args: self.changed.emit())
                self.system_table.setCellWidget(row, 1, combo)
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("")
            for name in customer_names:
                combo.addItem(name)
            if current_value:
                idx = combo.findText(current_value)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
            combo.blockSignals(False)

    def populate_system_rows(self):
        names = self._known_system_names()
        layout_by_name = {
            entry.system_name.strip().lower(): entry
            for entry in self.settings.system_layouts
            if entry.system_name.strip()
        }
        self.system_table.setRowCount(len(names))
        for row, system_name in enumerate(names):
            layout = layout_by_name.get(system_name.lower(), SystemLayoutEntry(system_name=system_name))
            name_item = QTableWidgetItem(system_name)
            name_item.setFlags(name_item.flags() & ~Qt.ItemIsEditable)
            self.system_table.setItem(row, 0, name_item)
            combo = QComboBox(self.system_table)
            combo.addItem("")
            for customer_name in self._customer_names():
                combo.addItem(customer_name)
            if layout.customer:
                idx = combo.findText(layout.customer)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
            combo.currentIndexChanged.connect(lambda *_args: self.changed.emit())
            self.system_table.setCellWidget(row, 1, combo)
            self.system_table.setItem(row, 2, QTableWidgetItem(layout.production_line))

    def apply_to(self, settings: Settings):
        customers = []
        customer_logos: dict[str, str] = {}
        customer_start_collapsed: dict[str, bool] = {}
        for row in range(self.customer_table.rowCount()):
            item = self.customer_table.item(row, 0)
            value = item.text().strip() if item else ""
            if value and value not in customers:
                customers.append(value)
            collapsed_item = self.customer_table.item(row, 1)
            if value and collapsed_item is not None:
                customer_start_collapsed[value] = collapsed_item.checkState() == Qt.Checked
            encoded_logo = self._logo_png_base64(row)
            if value and encoded_logo:
                customer_logos[value] = encoded_logo
        system_layouts = []
        for row in range(self.system_table.rowCount()):
            system_item = self.system_table.item(row, 0)
            if system_item is None:
                continue
            system_name = system_item.text().strip()
            if not system_name:
                continue
            combo = self.system_table.cellWidget(row, 1)
            customer = combo.currentText().strip() if isinstance(combo, QComboBox) else ""
            production_line = self.system_table.item(row, 2).text().strip() if self.system_table.item(row, 2) else ""
            if customer and customer not in customers:
                customers.append(customer)
            system_layouts.append(
                SystemLayoutEntry(
                    system_name=system_name,
                    customer=customer,
                    production_line=production_line,
                )
            )
        settings.customers = customers
        settings.customer_logos = customer_logos
        settings.customer_start_collapsed = customer_start_collapsed
        settings.system_layouts = system_layouts


class ReadmePanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        self.version_label = QLabel(f"Build: {format_version_label()}")
        self.version_label.setStyleSheet(theme.MUTED_LABEL)
        layout.addWidget(self.version_label)
        self.readme_view = QPlainTextEdit()
        self.readme_view.setReadOnly(True)
        self.readme_view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.readme_view.setPlaceholderText("README not available.")
        layout.addWidget(self.readme_view)
        self.setLayout(layout)
        self._load_readme()

    def _load_readme(self):
        candidates = []
        candidates.append(SRC_ROOT / "README.md")
        candidates.append(REPO_ROOT / "README.md")
        try:
            import sys
            if hasattr(sys, "_MEIPASS"):
                candidates.append(Path(sys._MEIPASS) / "README.md")
            candidates.append(Path(sys.executable).resolve().parent / "README.md")
        except Exception:
            pass
        text = ""
        for path in candidates:
            try:
                if path.exists():
                    text = path.read_text(encoding="utf-8")
                    break
            except Exception:
                continue
        if text:
            self.readme_view.setPlainText(text)
