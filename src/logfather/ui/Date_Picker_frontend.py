import sys
import calendar
from datetime import date
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QDate
from PySide6.QtGui import QTextCharFormat, QBrush, QColor, QFont, QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QPushButton,
    QLabel, QCalendarWidget, QMessageBox, QHBoxLayout, QScrollArea, QGridLayout, QButtonGroup,
    QSizePolicy
)
from logfather.ui import theme
from logfather.data.ui_state_store import (
    customer_collapsed_map,
    set_customer_collapsed,
)
from logfather.ui.qt_worker import JobSlot
from logfather.data.settings_store import (
    Settings,
    customer_logo_bytes,
    customer_starts_collapsed,
    customer_sort_key,
    display_customer_name,
    display_line_name,
    system_group_sort_key,
)


class DatePicker(QWidget):
    # Emits (pikpak_root: Path | None, day: date | None)
    date_selected = Signal(object, object)
    system_id_selected = Signal(object)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("CCTV Date Picker")

        self.top_dir: Path | None = None
        self.parent_dir: Path | None = None
        self.available_dates: set[date] = set()
        self.active_pikpak_name: str | None = None
        self._scan_slot = JobSlot(self)
        self.current_scan_path: Path | None = None
        self.active_day: date | None = None
        self.settings = Settings()
        self._collapsed_customers: set[str] = set()
        self._folder_action_buttons: list[QPushButton] = []

        self.status = QLabel("")
        self.status.setWordWrap(False)

        self.parent_label = None

        self.pikpak_container = QWidget()
        self.pikpak_layout = QVBoxLayout(self.pikpak_container)
        self.pikpak_layout.setContentsMargins(4, 4, 4, 4)
        self.pikpak_layout.setSpacing(2)
        self.pikpak_layout.setAlignment(Qt.AlignTop)
        self.pikpak_group = QButtonGroup(self)
        self.pikpak_group.setExclusive(True)
        self.pikpak_buttons: dict[str, QPushButton] = {}
        self.sim_button: QPushButton | None = None

        self.pikpak_scroll = QScrollArea()
        self.pikpak_scroll.setWidgetResizable(True)
        self.pikpak_scroll.setWidget(self.pikpak_container)
        self.pikpak_scroll.setMinimumHeight(140)

        self.calendar = QCalendarWidget()
        self.calendar.setGridVisible(True)
        self.calendar.currentPageChanged.connect(self.refresh_highlights)
        self.calendar.selectionChanged.connect(self.on_selection_changed)
        self.calendar.clicked.connect(self.on_calendar_clicked)
        self._selection_from_click = False
        self.calendar.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.calendar.setMinimumHeight(200)
        self.calendar.setMaximumHeight(330)

        layout = QVBoxLayout()
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)
        header_row = QHBoxLayout()
        header_row.addWidget(self.status, 1)
        layout.addLayout(header_row)
        layout.addWidget(self.pikpak_scroll, 3)
        layout.addWidget(self.calendar, 0)
        self.setLayout(layout)

        # Precompute normal + highlight formats
        self.normal_fmt = self.calendar.weekdayTextFormat(Qt.Monday)
        self.highlight_fmt = QTextCharFormat(self.normal_fmt)
        self.highlight_fmt.setBackground(QBrush(QColor(theme.ACCENT_DIM)))
        self.highlight_fmt.setForeground(QBrush(QColor(theme.TEXT_BRIGHT)))
        self.highlight_fmt.setFontWeight(QFont.Bold)
        self.selected_fmt = QTextCharFormat(self.normal_fmt)
        self.selected_fmt.setBackground(QBrush(QColor(theme.ACCENT)))
        self.selected_fmt.setForeground(QBrush(QColor("#081018")))
        self.selected_fmt.setFontWeight(QFont.Bold)

    def set_parent_dir(self, path: Path):
        if not path.exists():
            QMessageBox.warning(self, "Folder not found", str(path))
            return
        self.stop_scan_thread()
        self.parent_dir = path
        self.top_dir = None
        self.available_dates = set()
        self.active_pikpak_name = None
        self.status.setText("")
        self._sync_collapsed_customers(reset=True)
        self.populate_pikpak_buttons()
        self.refresh_highlights()

    def set_system_layout_settings(self, settings: Settings):
        self.settings = settings
        self._sync_collapsed_customers(reset=True)
        if self.parent_dir is not None:
            self.populate_pikpak_buttons()

    def _sync_collapsed_customers(self, customers: list[str] | set[str] | tuple[str, ...] | None = None, reset: bool = False):
        if customers is None:
            if self.parent_dir is None:
                known_customers: set[str] = set()
            else:
                try:
                    known_customers = {
                        display_customer_name(self.settings, path.name)
                        for path in self.parent_dir.iterdir()
                        if path.is_dir()
                    }
                except Exception:
                    known_customers = set()
        else:
            known_customers = {str(name or "").strip() for name in customers if str(name or "").strip()}
        # The user's own collapse choices (persisted per Windows user in
        # ui_state_store) win over the configured start-collapsed default;
        # toggles write through, so recomputing the whole set is safe.
        stored = customer_collapsed_map()
        self._collapsed_customers = {
            name
            for name in known_customers
            if stored.get(name, customer_starts_collapsed(self.settings, name))
        }

    def populate_pikpak_buttons(self):
        while self.pikpak_layout.count():
            item = self.pikpak_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        # Reset group and mapping
        self.pikpak_group = QButtonGroup(self)
        self.pikpak_group.setExclusive(True)
        self.pikpak_buttons.clear()
        self.sim_button = None
        self._folder_action_buttons = []

        if self.parent_dir is None:
            self.pikpak_layout.addWidget(QLabel("Choose a parent to list folders."), 0, 0)
            return

        subdirs = [p for p in self.parent_dir.iterdir() if p.is_dir()]
        subdirs.sort(key=lambda p: system_group_sort_key(self.settings, p.name))

        if not subdirs:
            self.pikpak_layout.addWidget(QLabel("No subfolders found in parent."), 0, 0)
            return

        cols = 2

        sim_btn = QPushButton("SIM Logs")
        sim_btn.setFixedHeight(24)
        sim_btn.setCheckable(True)
        sim_btn.setMinimumWidth(90)
        sim_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        sim_btn.setStyleSheet(theme.SIM_BUTTON)
        sim_btn.clicked.connect(self.use_sim_mode)
        self.pikpak_group.addButton(sim_btn)
        self.pikpak_buttons["__SIM__"] = sim_btn
        self.sim_button = sim_btn
        self._folder_action_buttons.append(sim_btn)
        sim_row = QWidget()
        sim_layout = QHBoxLayout(sim_row)
        sim_layout.setContentsMargins(0, 0, 0, 0)
        sim_layout.setSpacing(2)
        sim_layout.addWidget(sim_btn)
        sim_layout.addStretch(1)
        self.pikpak_layout.addWidget(sim_row)
        grouped: dict[str, list[Path]] = {}
        for path in subdirs:
            grouped.setdefault(display_customer_name(self.settings, path.name), []).append(path)
        ordered_customers = sorted(grouped.keys(), key=lambda name: customer_sort_key(self.settings, name))
        for customer_name in ordered_customers:
            header_row = QWidget()
            header_layout = QHBoxLayout(header_row)
            header_layout.setContentsMargins(0, 6, 0, 0)
            header_layout.setSpacing(6)
            marker = "▼" if customer_name in self._collapsed_customers else "▲"
            header_btn = QPushButton(f"{marker}  {customer_name}")
            header_btn.setCursor(Qt.PointingHandCursor)
            header_btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            header_btn.setStyleSheet(theme.CUSTOMER_HEADER_BUTTON)
            header_btn.clicked.connect(lambda _checked=False, name=customer_name: self.toggle_customer_collapsed(name))
            header_layout.addWidget(header_btn, 1)
            logo_bytes = customer_logo_bytes(self.settings, customer_name)
            if logo_bytes:
                logo_image = QImage.fromData(logo_bytes, "PNG")
                if not logo_image.isNull():
                    logo_label = QLabel()
                    logo_pixmap = QPixmap.fromImage(logo_image).scaled(64, 22, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                    logo_label.setPixmap(logo_pixmap)
                    logo_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                    header_layout.addWidget(logo_label, 0)
            self.pikpak_layout.addWidget(header_row)

            group_widget = QWidget()
            group_layout = QGridLayout(group_widget)
            group_layout.setContentsMargins(0, 2, 0, 0)
            group_layout.setHorizontalSpacing(4)
            group_layout.setVerticalSpacing(2)
            for idx, path in enumerate(grouped[customer_name]):
                line_name = display_line_name(self.settings, path.name)
                button_text = f"{line_name}\n{path.name}" if line_name else f"\n{path.name}"
                item_widget = QWidget()
                item_layout = QHBoxLayout(item_widget)
                item_layout.setContentsMargins(0, 0, 0, 0)
                item_layout.setSpacing(0)

                btn = QPushButton(button_text)
                btn.setFixedHeight(44)
                btn.setCheckable(True)
                btn.setMinimumWidth(90)
                btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
                btn.setStyleSheet(theme.SYSTEM_BUTTON)
                btn.clicked.connect(lambda _checked=False, p=path: self.use_pikpak_folder(p))
                self.pikpak_group.addButton(btn)
                self.pikpak_buttons[path.name] = btn
                self._folder_action_buttons.append(btn)

                today_btn = QPushButton("")
                today_btn.setFixedSize(24, 44)
                today_btn.setToolTip(f"Load {path.name} for today")
                today_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
                today_btn.setStyleSheet(theme.TODAY_BUTTON)
                today_btn.clicked.connect(lambda _checked=False, p=path: self.select_pikpak_folder_and_day(p, date.today()))
                self._folder_action_buttons.append(today_btn)

                item_layout.addWidget(btn, 1)
                item_layout.addWidget(today_btn, 0)
                group_layout.addWidget(item_widget, idx // cols, idx % cols)

            group_widget.setVisible(customer_name not in self._collapsed_customers)
            self.pikpak_layout.addWidget(group_widget)

        self.pikpak_layout.addStretch(1)
        self.update_pikpak_selection()

    def use_pikpak_folder(self, pikpak_path: Path):
        if not pikpak_path.exists():
            QMessageBox.warning(self, "Folder not found", str(pikpak_path))
            return
        self.system_id_selected.emit(None)
        self.top_dir = pikpak_path
        self.active_pikpak_name = pikpak_path.name
        self.active_day = None
        self.update_pikpak_selection()
        self.start_scan_thread(pikpak_path)
        # Clear time picker until scan completes
        self.emit_date_selected()

    def select_pikpak_folder_and_day(self, pikpak_path: Path, selected_day: date):
        if self.parent_dir is None or self.parent_dir != pikpak_path.parent:
            self.set_parent_dir(pikpak_path.parent)
        self.use_pikpak_folder(pikpak_path)
        qd = QDate(selected_day.year, selected_day.month, selected_day.day)
        self.calendar.setCurrentPage(selected_day.year, selected_day.month)
        self.calendar.setSelectedDate(qd)
        self._apply_active_day(qd)

    def select_day(self, selected_day: date):
        """Pick a day for the current system without re-listing it (the
        viewer's Choose date popup, Chris 2026-09-05)."""
        qd = QDate(selected_day.year, selected_day.month, selected_day.day)
        self.calendar.setCurrentPage(selected_day.year, selected_day.month)
        self._selection_from_click = True
        try:
            self.calendar.setSelectedDate(qd)
        finally:
            self._selection_from_click = False
        self._apply_active_day(qd)

    def update_pikpak_selection(self):
        for name, btn in self.pikpak_buttons.items():
            btn.setChecked(self.active_pikpak_name == name)

    def toggle_customer_collapsed(self, customer_name: str):
        key = str(customer_name or "").strip()
        if not key:
            return
        if key in self._collapsed_customers:
            self._collapsed_customers.discard(key)
        else:
            self._collapsed_customers.add(key)
        set_customer_collapsed(key, key in self._collapsed_customers)
        self.populate_pikpak_buttons()

    def use_sim_mode(self):
        self.stop_scan_thread()
        self.top_dir = None
        self.active_pikpak_name = "__SIM__"
        self.available_dates = set()
        self.update_pikpak_selection()
        self.status.setText("SIM logs mode (35-2300-SIM). Pick any date.")
        self.system_id_selected.emit("35-2300-SIM")
        self.refresh_highlights()
        self.emit_date_selected()

    def update_status_after_scan(self, folder_name: str):
        if not self.available_dates:
            self.status.setText(f"No dates found under {folder_name}.")
        else:
            self.status.setText(
                f"{folder_name}: found {len(self.available_dates)} dates. Switch months to see highlights."
            )

    def jump_to_latest_month(self):
        if not self.available_dates:
            return
        latest = max(self.available_dates)
        self.calendar.setCurrentPage(latest.year, latest.month)

    def scan_dates(self, top: Path) -> set[date]:
        found: set[date] = set()
        for year_dir in top.iterdir():
            if not year_dir.is_dir() or len(year_dir.name) != 4 or not year_dir.name.isdigit():
                continue
            year_val = int(year_dir.name)

            for month_dir in year_dir.iterdir():
                if not month_dir.is_dir() or len(month_dir.name) != 2 or not month_dir.name.isdigit():
                    continue
                month_val = int(month_dir.name)
                if month_val < 1 or month_val > 12:
                    continue

                for day_dir in month_dir.iterdir():
                    if not day_dir.is_dir() or len(day_dir.name) != 2 or not day_dir.name.isdigit():
                        continue
                    day_val = int(day_dir.name)
                    try:
                        found.add(date(year_val, month_val, day_val))
                    except ValueError:
                        pass
        return found

    def refresh_highlights(self):
        year = self.calendar.yearShown()
        month = self.calendar.monthShown()

        prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
        next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
        visible_months = {(prev_year, prev_month), (year, month), (next_year, next_month)}

        # Clear formats for the months that are visible in the grid (prev/current/next).
        for y, m in visible_months:
            days_in_month = calendar.monthrange(y, m)[1]
            for day in range(1, days_in_month + 1):
                self.calendar.setDateTextFormat(date(y, m, day), self.normal_fmt)

        # Re-apply highlights for any visible dates.
        for dt in self.available_dates:
            if (dt.year, dt.month) in visible_months:
                self.calendar.setDateTextFormat(dt, self.highlight_fmt)

        # Apply selected-day emphasis (overrides availability highlight).
        if self.active_day and (self.active_day.year, self.active_day.month) in visible_months:
            self.calendar.setDateTextFormat(self.active_day, self.selected_fmt)

    def start_scan_thread(self, path: Path):
        self.current_scan_path = path
        self.set_scanning_state(True, f"Scanning {path.name} for dates...")
        self._scan_slot.start(
            lambda job, p=path: self.scan_dates(p),
            on_result=self.on_scan_finished,
            on_error=self.on_scan_error,
            on_finished=self.on_scan_thread_done,
        )

    def on_scan_finished(self, dates):
        self.available_dates = set(dates) if dates else set()
        if self.current_scan_path:
            self.update_status_after_scan(self.current_scan_path.name)
        if self.available_dates:
            self.jump_to_latest_month()

    def on_scan_error(self, message: str):
        QMessageBox.warning(self, "Scan failed", message)
        self.available_dates = set()
        self.status.setText("Scan failed.")

    def on_scan_thread_done(self):
        self.set_scanning_state(False)
        self.refresh_highlights()
        self.current_scan_path = None

    def set_scanning_state(self, scanning: bool, message: str | None = None):
        if message:
            self.status.setText(message)
        for btn in self._folder_action_buttons:
            btn.setEnabled(not scanning)

    def stop_scan_thread(self):
        was_running = self._scan_slot.is_running()
        self._scan_slot.retire()
        if was_running:
            self.set_scanning_state(False)
        self.current_scan_path = None

    def _apply_active_day(self, qd: QDate):
        self.active_day = date(qd.year(), qd.month(), qd.day())
        self.refresh_highlights()
        self.emit_date_selected()

    def on_selection_changed(self):
        if self._selection_from_click:
            return
        self._apply_active_day(self.calendar.selectedDate())

    def on_calendar_clicked(self, qd: QDate):
        self._selection_from_click = True
        try:
            self._apply_active_day(qd)
        finally:
            self._selection_from_click = False

    def emit_date_selected(self):
        self.date_selected.emit(self.top_dir if self.top_dir else None, self.active_day)


def main():
    app = QApplication(sys.argv)
    win = DatePicker()
    win.resize(500, 450)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
