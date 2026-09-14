from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QRectF, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QButtonGroup, QComboBox, QFrame, QGridLayout, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMessageBox, QPushButton, QRadioButton, QScrollArea,
    QSizePolicy, QStackedWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from logfather.data.elastic_loader import fetch_fleetwide_search_histogram
from logfather.data.elastic_schema import robot_id_from_folder
from logfather.ui import theme
from logfather.ui.qt_worker import JobSlot
from logfather.data.settings_store import (
    FleetwideSearchDefinition, Settings, display_customer_name, display_line_name,
    system_group_sort_key,
)


RANGE_BUCKET_SECONDS = {1: 3600, 7: 6 * 3600, 30: 24 * 3600, 90: 3 * 24 * 3600}


def _robot_id_for_folder(path: Path) -> str:
    return robot_id_from_folder(path.name) or ""


class FleetwideSearchSettingsPage(QWidget):
    saved = Signal()
    back_requested = Signal()

    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        heading = QLabel("Fleetwide Search Settings")
        font = QFont(self.font())
        font.setPointSize(font.pointSize() + 4)
        font.setBold(True)
        heading.setFont(font)
        intro = QLabel(
            "Configure the named searches shown as buttons on the fleetwide dashboard. "
            "Each search can be an Elastic query string or a JSON query clause."
        )
        intro.setWordWrap(True)
        self.table = QTableWidget(0, 2, self)
        self.table.setHorizontalHeaderLabels(["Button name", "Elastic query string or JSON query clause"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        add_btn = QPushButton("Add Search")
        add_btn.clicked.connect(self._add_row)
        remove_btn = QPushButton("Remove Selected")
        remove_btn.clicked.connect(self._remove_selected)
        save_btn = QPushButton("Save")
        save_btn.clicked.connect(self._save)
        back_btn = QPushButton("Back to Dashboard")
        back_btn.clicked.connect(self.back_requested.emit)
        actions = QHBoxLayout()
        actions.addWidget(add_btn)
        actions.addWidget(remove_btn)
        actions.addStretch(1)
        actions.addWidget(back_btn)
        actions.addWidget(save_btn)
        layout = QVBoxLayout(self)
        layout.addWidget(heading)
        layout.addWidget(intro)
        layout.addWidget(self.table, 1)
        layout.addLayout(actions)
        self.reload()

    def set_settings(self, settings: Settings):
        self.settings = settings
        self.reload()

    def reload(self):
        searches = list(getattr(self.settings, "fleetwide_searches", []) or [])
        self.table.setRowCount(len(searches))
        for row, search in enumerate(searches):
            self.table.setItem(row, 0, QTableWidgetItem(search.name))
            self.table.setItem(row, 1, QTableWidgetItem(search.query))

    def _add_row(self):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem("New search"))
        self.table.setItem(row, 1, QTableWidgetItem(""))
        self.table.setCurrentCell(row, 0)
        self.table.editItem(self.table.item(row, 0))

    def _remove_selected(self):
        row = self.table.currentRow()
        if row >= 0:
            self.table.removeRow(row)

    def _save(self):
        searches = []
        for row in range(self.table.rowCount()):
            name_item = self.table.item(row, 0)
            query_item = self.table.item(row, 1)
            name = name_item.text().strip() if name_item else ""
            query = query_item.text().strip() if query_item else ""
            if not name and not query:
                continue
            if not name or not query:
                QMessageBox.warning(self, "Incomplete search", f"Row {row + 1} needs both a name and a query.")
                return
            searches.append(FleetwideSearchDefinition(name=name, query=query))
        if not searches:
            QMessageBox.warning(self, "No searches", "Add at least one fleetwide search.")
            return
        self.settings.fleetwide_searches = searches
        self.settings.save()
        self.saved.emit()


class FleetwideGraph(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._buckets: list[dict] = []
        self._hovered_bucket_index: int | None = None
        self.setMinimumHeight(145)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMouseTracking(True)

    def set_buckets(self, buckets: list[dict]):
        self._buckets = list(buckets or [])
        self._hovered_bucket_index = None
        self.update()

    def _plot_rect(self) -> QRectF:
        return QRectF(48, 10, max(1, self.width() - 64), max(1, self.height() - 40))

    def mouseMoveEvent(self, event):
        plot = self._plot_rect()
        position = event.position()
        hovered_index = None
        if self._buckets and plot.contains(position):
            ratio = (position.x() - plot.left()) / max(1.0, plot.width())
            hovered_index = int(ratio * len(self._buckets))
            hovered_index = max(0, min(len(self._buckets) - 1, hovered_index))
        if hovered_index != self._hovered_bucket_index:
            self._hovered_bucket_index = hovered_index
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        if self._hovered_bucket_index is not None:
            self._hovered_bucket_index = None
            self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        plot = self._plot_rect()
        painter.fillRect(plot, QColor("#111820"))
        operating_counts = [
            max(0, int(bucket.get("operational_count", bucket.get("count", 0)) or 0))
            for bucket in self._buckets
        ]
        non_operating_counts = [
            max(0, int(bucket.get("non_operational_count", 0) or 0))
            for bucket in self._buckets
        ]
        counts = [operating + non_operating for operating, non_operating in zip(operating_counts, non_operating_counts)]
        scale_max = max(1, max(counts, default=0))
        painter.setPen(QPen(QColor("#34414c"), 1))
        for step in range(5):
            y = plot.bottom() - (plot.height() * step / 4)
            painter.drawLine(plot.left(), y, plot.right(), y)
        if counts:
            for boundary_index in range(len(counts) + 1):
                x = plot.left() + plot.width() * boundary_index / len(counts)
                painter.drawLine(x, plot.top(), x, plot.bottom())
        painter.setPen(QColor("#8f9aa3"))
        painter.drawText(QRectF(0, plot.top() - 3, 43, 20), Qt.AlignRight | Qt.AlignVCenter, str(scale_max))
        painter.drawText(QRectF(0, plot.bottom() - 10, 43, 20), Qt.AlignRight | Qt.AlignVCenter, "0")
        if counts:
            slot_width = plot.width() / len(counts)
            bar_width = max(2.0, slot_width * 0.68)
            for index, (operating, non_operating) in enumerate(zip(operating_counts, non_operating_counts)):
                center_x = plot.left() + slot_width * (index + 0.5)
                operating_height = plot.height() * operating / scale_max
                non_operating_height = plot.height() * non_operating / scale_max
                if operating_height > 0:
                    painter.fillRect(
                        QRectF(center_x - bar_width / 2, plot.bottom() - operating_height, bar_width, operating_height),
                        QColor(theme.LEGEND_OPERATION),
                    )
                if non_operating_height > 0:
                    painter.fillRect(
                        QRectF(
                            center_x - bar_width / 2,
                            plot.bottom() - operating_height - non_operating_height,
                            bar_width,
                            non_operating_height,
                        ),
                        QColor("#f39c12"),
                    )
            hovered_index = self._hovered_bucket_index
            if hovered_index is not None and 0 <= hovered_index < len(counts):
                cursor_x = plot.left() + slot_width * (hovered_index + 0.5)
                point_y = plot.bottom() - plot.height() * counts[hovered_index] / scale_max
                painter.setPen(QPen(QColor("#f1c40f"), 1))
                painter.drawLine(cursor_x, plot.top(), cursor_x, plot.bottom())
                painter.setPen(QPen(QColor("#f1c40f"), 2))
                painter.setBrush(Qt.NoBrush)
                painter.drawRect(
                    QRectF(
                        cursor_x - bar_width / 2,
                        point_y,
                        bar_width,
                        max(1.0, plot.bottom() - point_y),
                    )
                )

                bucket = self._buckets[hovered_index]
                timestamp = bucket.get("timestamp")
                bucket_end = bucket.get("end")
                count = counts[hovered_index]
                operating_count = operating_counts[hovered_index]
                non_operating_count = non_operating_counts[hovered_index]
                if isinstance(timestamp, datetime):
                    local_start = timestamp.astimezone()
                    if isinstance(bucket_end, datetime):
                        local_end = bucket_end.astimezone()
                        if local_start.date() == local_end.date():
                            date_text = (
                                f"{local_start:%d %b %Y}  "
                                f"{local_start:%H:%M}–{local_end:%H:%M}"
                            )
                        else:
                            date_text = f"{local_start:%d %b %H:%M} – {local_end:%d %b %H:%M}"
                    else:
                        date_text = local_start.strftime("%d %b %Y  %H:%M")
                else:
                    date_text = "Unknown time"
                count_text = f"{count:,} occurrence" if count == 1 else f"{count:,} occurrences"
                details_text = f"{operating_count:,} operation  •  {non_operating_count:,} startup/stopped"
                tooltip_text = f"{date_text}\n{count_text}\n{details_text}"
                metrics = painter.fontMetrics()
                tooltip_width = max(
                    metrics.horizontalAdvance(date_text),
                    metrics.horizontalAdvance(count_text),
                    metrics.horizontalAdvance(details_text),
                ) + 18
                tooltip_height = metrics.height() * 3 + 14
                tooltip_x = cursor_x + 10
                if tooltip_x + tooltip_width > self.width() - 4:
                    tooltip_x = cursor_x - tooltip_width - 10
                tooltip_y = max(4.0, min(point_y - tooltip_height - 8, self.height() - tooltip_height - 4))
                tooltip_rect = QRectF(tooltip_x, tooltip_y, tooltip_width, tooltip_height)
                painter.setPen(QPen(QColor("#66727d"), 1))
                painter.setBrush(QColor("#202a33"))
                painter.drawRoundedRect(tooltip_rect, 5, 5)
                painter.setPen(QColor("#f3f5f7"))
                painter.drawText(tooltip_rect.adjusted(9, 5, -9, -5), Qt.AlignLeft | Qt.AlignVCenter, tooltip_text)
        if self._buckets:
            first = self._buckets[0].get("timestamp")
            last = self._buckets[-1].get("timestamp")
            painter.setPen(QColor("#8f9aa3"))
            if isinstance(first, datetime):
                painter.drawText(QRectF(plot.left(), plot.bottom() + 5, 100, 20), Qt.AlignLeft, first.astimezone().strftime("%d %b"))
            if isinstance(last, datetime):
                painter.drawText(QRectF(plot.right() - 100, plot.bottom() + 5, 100, 20), Qt.AlignRight, last.astimezone().strftime("%d %b"))
        painter.end()


class FleetwideSystemCard(QFrame):
    def __init__(self, system_root: Path, settings: Settings, parent=None):
        super().__init__(parent)
        self.system_root = system_root
        self.setObjectName("fleetwideSystemCard")
        self.setStyleSheet(theme.FLEETWIDE_CARD)
        self.setMinimumHeight(205)
        customer = display_customer_name(settings, system_root.name)
        line = display_line_name(settings, system_root.name)
        robot_id = _robot_id_for_folder(system_root)
        title = QLabel(system_root.name)
        title_font = QFont(self.font())
        title_font.setPointSize(title_font.pointSize() + 2)
        title_font.setBold(True)
        title.setFont(title_font)
        detail_parts = [customer]
        if line:
            detail_parts.append(line)
        if robot_id:
            detail_parts.append(robot_id)
        detail = QLabel("  •  ".join(detail_parts))
        detail.setStyleSheet(theme.FLEETWIDE_MUTED)
        self.count_label = QLabel("—")
        count_font = QFont(self.font())
        count_font.setPointSize(count_font.pointSize() + 12)
        count_font.setBold(True)
        self.count_label.setFont(count_font)
        self.count_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.count_caption = QLabel("occurrences")
        self.count_caption.setAlignment(Qt.AlignRight)
        self.count_caption.setStyleSheet(theme.FLEETWIDE_MUTED)
        heading = QGridLayout()
        heading.addWidget(title, 0, 0)
        heading.addWidget(detail, 1, 0)
        heading.addWidget(self.count_label, 0, 1, 2, 1)
        heading.addWidget(self.count_caption, 2, 1)
        heading.setColumnStretch(0, 1)
        self.graph = FleetwideGraph(self)
        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet(theme.ERROR_LABEL)
        self.error_label.setVisible(False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.addLayout(heading)
        layout.addWidget(self.graph)
        layout.addWidget(self.error_label)

    def set_result(self, result: dict | None, error: str = "", operation_mode: str = "all"):
        if error:
            self.count_label.setText("Error")
            self.graph.set_buckets([])
            self.error_label.setText(error)
            self.error_label.setVisible(True)
            return
        self.error_label.setVisible(False)
        if result is None:
            self.count_label.setText("—")
            self.count_label.setToolTip("")
            self.graph.set_buckets([])
            return
        if operation_mode == "operating":
            total_key = "operational_total"
            self.count_caption.setText("during operation")
        elif operation_mode == "not_operating":
            total_key = "non_operational_total"
            self.count_caption.setText("during startup / stopped")
        else:
            total_key = "total"
            self.count_caption.setText("occurrences")
        self.count_label.setText(f"{int(result.get(total_key, 0)):,}")
        suppressed_count = int(result.get("suppressed_count", 0) or 0)
        cooldown_seconds = int(result.get("cooldown_seconds", 30) or 30)
        if suppressed_count:
            self.count_label.setToolTip(
                f"{suppressed_count:,} duplicate servo record"
                f"{'s' if suppressed_count != 1 else ''} suppressed by the "
                f"{cooldown_seconds}-second cooldown."
            )
        else:
            self.count_label.setToolTip(f"No duplicates suppressed by the {cooldown_seconds}-second cooldown.")
        displayed_buckets = []
        for bucket in result.get("buckets", []):
            operating_count = int(bucket.get("operational_count", 0) or 0)
            non_operating_count = int(bucket.get("non_operational_count", 0) or 0)
            if operation_mode == "operating":
                non_operating_count = 0
            elif operation_mode == "not_operating":
                operating_count = 0
            displayed_buckets.append(
                {
                    "timestamp": bucket.get("timestamp"),
                    "end": bucket.get("end"),
                    "count": operating_count + non_operating_count,
                    "operational_count": operating_count,
                    "non_operational_count": non_operating_count,
                }
            )
        self.graph.set_buckets(displayed_buckets)


def _run_fleetwide_search(job, settings: Settings, systems: list[Path], queries: list[str], days: int):
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=days)
    bucket_seconds = RANGE_BUCKET_SECONDS[days]
    results = {}
    errors = {}
    with ThreadPoolExecutor(max_workers=min(4, max(1, len(systems)))) as executor:
        futures = {
            executor.submit(fetch_fleetwide_search_histogram, settings, system, queries,
                            start_dt, end_dt, bucket_seconds): system
            for system in systems
        }
        completed = 0
        for future in as_completed(futures):
            system = futures[future]
            if job.interrupted():
                break
            try:
                results[system.name] = future.result()
            except Exception as exc:
                errors[system.name] = str(exc)[:600]
            completed += 1
            job.emit_progress((completed, len(systems)))
    return results, errors


class FleetwideElasticSearchWidget(QWidget):
    settings_saved = Signal()
    TWO_COLUMN_MIN_WIDTH = 1500

    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.parent_dir: Path | None = None
        self._active = False
        self._search_slot = JobSlot(self)
        self._results: dict[str, dict] = {}
        self._errors: dict[str, str] = {}
        self._has_loaded_results = False
        self._search_pending = True
        self._card_column_count = 1
        self.page_stack = QStackedWidget(self)
        self.dashboard = QWidget(self.page_stack)
        self.settings_page = FleetwideSearchSettingsPage(settings, self.page_stack)
        self.page_stack.addWidget(self.dashboard)
        self.page_stack.addWidget(self.settings_page)
        self.settings_page.back_requested.connect(lambda: self.page_stack.setCurrentWidget(self.dashboard))
        self.settings_page.saved.connect(self._on_search_settings_saved)
        self._build_dashboard()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.page_stack)

    def _build_dashboard(self):
        title = QLabel("Fleetwide Elastic Search")
        title_font = QFont(self.font())
        title_font.setPointSize(title_font.pointSize() + 5)
        title_font.setBold(True)
        title.setFont(title_font)
        settings_btn = QPushButton("Search Settings")
        settings_btn.clicked.connect(self._show_settings)
        title_row = QHBoxLayout()
        title_row.addWidget(title)
        title_row.addStretch(1)
        title_row.addWidget(settings_btn)
        self.search_button_host = QWidget()
        self.search_button_layout = QHBoxLayout(self.search_button_host)
        self.search_button_layout.setContentsMargins(0, 0, 0, 0)
        self.search_button_layout.setSpacing(6)
        self.search_button_group = QButtonGroup(self)
        range_row = QHBoxLayout()
        range_row.addWidget(QLabel("Range:"))
        self.range_group = QButtonGroup(self)
        self.range_buttons = {}
        for days in (1, 7, 30, 90):
            button = QRadioButton(f"{days} day" if days == 1 else f"{days} days")
            button.toggled.connect(lambda checked: checked and self._mark_search_pending())
            self.range_group.addButton(button)
            self.range_buttons[days] = button
            range_row.addWidget(button)
        self.range_buttons[7].setChecked(True)
        range_row.addStretch(1)
        operation_row = QHBoxLayout()
        operation_row.addWidget(QLabel("Occurrences:"))
        self.operation_group = QButtonGroup(self)
        self.operation_buttons = {}
        for label, mode in (
            ("All", "all"),
            ("During operation", "operating"),
            ("Startup / stopped", "not_operating"),
        ):
            button = QRadioButton(label)
            button.setProperty("operation_mode", mode)
            button.toggled.connect(
                lambda checked: checked and hasattr(self, "card_layout") and self._apply_filters()
            )
            self.operation_group.addButton(button)
            self.operation_buttons[mode] = button
            operation_row.addWidget(button)
        self.operation_buttons["operating"].setChecked(True)
        operation_note = QLabel("Operation begins 60 seconds after start_pnp and ends on stop, manual, caution or shutdown.")
        operation_note.setStyleSheet(theme.FLEETWIDE_NOTE)
        operation_row.addWidget(operation_note)
        operation_row.addStretch(1)
        legend = QLabel(
            f'<span style="color:{theme.LEGEND_OPERATION};">■</span> Operation&nbsp;&nbsp;'
            f'<span style="color:{theme.LEGEND_STARTUP};">■</span> Startup / stopped'
        )
        operation_row.addWidget(legend)
        self.serial_filter = QLineEdit()
        self.serial_filter.setPlaceholderText("Filter by serial number…")
        self.serial_filter.setClearButtonEnabled(True)
        self.serial_filter.textChanged.connect(self._apply_filters)
        self.customer_filter = QComboBox()
        self.customer_filter.addItem("All customers")
        self.customer_filter.currentIndexChanged.connect(self._apply_filters)
        refresh_btn = QPushButton("Run Search")
        refresh_btn.setToolTip("Run the selected searches across the fleet")
        refresh_btn.clicked.connect(self._refresh)
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Systems:"))
        filter_row.addWidget(self.serial_filter, 1)
        filter_row.addWidget(self.customer_filter)
        filter_row.addWidget(refresh_btn)
        self.status_label = QLabel("Choose a parent folder to load fleet systems.")
        self.status_label.setStyleSheet(theme.FLEETWIDE_MUTED)
        self.card_container = QWidget()
        self.card_layout = QGridLayout(self.card_container)
        self.card_layout.setContentsMargins(4, 4, 4, 4)
        self.card_layout.setSpacing(10)
        self.card_layout.setAlignment(Qt.AlignTop)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setWidget(self.card_container)
        dashboard_layout = QVBoxLayout(self.dashboard)
        dashboard_layout.addLayout(title_row)
        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("Search (select one or more):"))
        search_row.addWidget(self.search_button_host, 1)
        dashboard_layout.addLayout(search_row)
        dashboard_layout.addLayout(range_row)
        dashboard_layout.addLayout(operation_row)
        dashboard_layout.addLayout(filter_row)
        dashboard_layout.addWidget(self.status_label)
        dashboard_layout.addWidget(self.scroll, 1)
        self._rebuild_search_buttons()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        QTimer.singleShot(0, self._update_card_columns)

    def _desired_card_column_count(self) -> int:
        return 2 if self.width() >= self.TWO_COLUMN_MIN_WIDTH else 1

    def _update_card_columns(self):
        desired = self._desired_card_column_count()
        if desired == self._card_column_count or not hasattr(self, "card_layout"):
            return
        self._card_column_count = desired
        self._apply_filters()

    def set_settings(self, settings: Settings):
        self.settings = settings
        self.settings_page.set_settings(settings)
        self._rebuild_search_buttons()
        self._refresh_customer_filter()
        self._apply_filters()

    def set_parent_dir(self, path: Path | None):
        normalized = Path(path) if path else None
        if normalized == self.parent_dir:
            return
        self.parent_dir = normalized
        self._results = {}
        self._errors = {}
        self._has_loaded_results = False
        self._search_pending = True
        self._refresh_customer_filter()
        self._apply_filters()
        if self._active:
            self.status_label.setText("System folder changed. Click Run Search when ready.")

    def activate(self, active: bool):
        self._active = bool(active)
        if self._active and not self._results and not self._search_slot.is_running():
            self.status_label.setText("Select the searches and range, then click Run Search.")

    def _show_settings(self):
        self.settings_page.reload()
        self.page_stack.setCurrentWidget(self.settings_page)

    def _on_search_settings_saved(self):
        self._rebuild_search_buttons()
        self.page_stack.setCurrentWidget(self.dashboard)
        self.settings_saved.emit()
        self._mark_search_pending("Search settings saved. Click Run Search to use them.")

    def _rebuild_search_buttons(self):
        while self.search_button_layout.count():
            item = self.search_button_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.search_button_group = QButtonGroup(self)
        self.search_button_group.setExclusive(False)
        searches = list(getattr(self.settings, "fleetwide_searches", []) or [])
        for index, search in enumerate(searches):
            button = QPushButton(search.name)
            button.setCheckable(True)
            button.setProperty("query", search.query)
            button.toggled.connect(lambda _checked: self._mark_search_pending())
            self.search_button_group.addButton(button)
            self.search_button_layout.addWidget(button)
            if index == 0:
                button.setChecked(True)
        self.search_button_layout.addStretch(1)

    def _system_roots(self) -> list[Path]:
        if self.parent_dir is None or not self.parent_dir.exists():
            return []
        try:
            systems = [path for path in self.parent_dir.iterdir() if path.is_dir()]
        except Exception:
            return []
        return sorted(systems, key=lambda path: system_group_sort_key(self.settings, path.name))

    def _refresh_customer_filter(self):
        selected = self.customer_filter.currentText() if hasattr(self, "customer_filter") else "All customers"
        customers = sorted({display_customer_name(self.settings, path.name) for path in self._system_roots()}, key=str.lower)
        self.customer_filter.blockSignals(True)
        self.customer_filter.clear()
        self.customer_filter.addItem("All customers")
        self.customer_filter.addItems(customers)
        index = self.customer_filter.findText(selected)
        self.customer_filter.setCurrentIndex(max(0, index))
        self.customer_filter.blockSignals(False)

    def _selected_searches(self) -> tuple[str, list[str]] | None:
        buttons = [button for button in self.search_button_group.buttons() if button.isChecked()]
        if not buttons:
            return None
        names = [button.text() for button in buttons]
        queries = [str(button.property("query") or "") for button in buttons]
        display_name = names[0] if len(names) == 1 else f"{len(names)} selected searches"
        return display_name, queries

    def _selected_days(self) -> int:
        for days, button in self.range_buttons.items():
            if button.isChecked():
                return days
        return 7

    def _selected_operation_mode(self) -> str:
        button = self.operation_group.checkedButton()
        if button is None:
            return "all"
        return str(button.property("operation_mode") or "all")

    def _mark_search_pending(self, message: str | None = None):
        self._search_pending = True
        if hasattr(self, "status_label"):
            self.status_label.setText(
                message or "Search selection changed. Click Run Search to update the results."
            )

    @staticmethod
    def _result_total_for_mode(result: dict, operation_mode: str) -> int:
        if operation_mode == "operating":
            key = "operational_total"
        elif operation_mode == "not_operating":
            key = "non_operational_total"
        else:
            key = "total"
        return int(result.get(key, 0) or 0)

    def _refresh(self):
        if not self._active:
            return
        selected = self._selected_searches()
        systems = self._system_roots()
        if selected is None:
            self.status_label.setText("Configure at least one fleetwide search.")
            return
        if not systems:
            self.status_label.setText("Choose a parent folder containing system folders.")
            self._apply_filters()
            return
        name, queries = selected
        days = self._selected_days()
        self._results = {}
        self._errors = {}
        self._has_loaded_results = False
        self._search_pending = False
        self.status_label.setText(f"Loading {name} across {len(systems)} systems…")
        self._apply_filters()
        settings = self.settings
        run_systems = list(systems)
        run_queries = list(queries)
        self._search_slot.start(
            lambda job: _run_fleetwide_search(job, settings, run_systems, run_queries, days),
            on_result=self._on_results,
            on_progress=self._on_progress,
        )

    def _on_progress(self, payload):
        completed, total = payload
        self.status_label.setText(f"Loading systems… {completed}/{total}")

    def _on_results(self, payload):
        results, errors = payload
        self._results = dict(results)
        self._errors = dict(errors)
        self._has_loaded_results = True
        self._apply_filters()

    def shutdown_workers(self):
        """Stop background searches. Called by MainWindow.closeEvent —
        panel closeEvents never fire inside the app."""
        self._search_slot.shutdown(1000)

    def closeEvent(self, event):
        self.shutdown_workers()
        super().closeEvent(event)

    def _apply_filters(self):
        while self.card_layout.count():
            item = self.card_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        serial_filter = self.serial_filter.text().strip().lower() if hasattr(self, "serial_filter") else ""
        customer_filter = self.customer_filter.currentText() if hasattr(self, "customer_filter") else "All customers"
        operation_mode = self._selected_operation_mode()
        column_count = self._desired_card_column_count()
        self._card_column_count = column_count
        visible_systems = []
        for system in self._system_roots():
            customer = display_customer_name(self.settings, system.name)
            serial_text = f"{system.name} {_robot_id_for_folder(system)}".lower()
            if serial_filter and serial_filter not in serial_text:
                continue
            if customer_filter != "All customers" and customer != customer_filter:
                continue
            visible_systems.append(system)
        shown_count = 0
        zero_hidden = 0
        error_hidden = 0
        for system in visible_systems:
            result = self._results.get(system.name)
            error = self._errors.get(system.name, "")
            if self._has_loaded_results:
                if error:
                    error_hidden += 1
                    continue
                if result is None or self._result_total_for_mode(result, operation_mode) == 0:
                    zero_hidden += 1
                    continue
            card = FleetwideSystemCard(system, self.settings, self.card_container)
            card.set_result(
                result,
                error,
                operation_mode,
            )
            row = shown_count // column_count
            column = shown_count % column_count
            self.card_layout.addWidget(card, row, column)
            shown_count += 1
        if self.card_layout.count() == 0:
            if visible_systems and self._has_loaded_results:
                message = "No matching systems returned occurrences. Systems with zero are hidden."
            else:
                message = "No systems match the current filters."
            empty = QLabel(message)
            empty.setAlignment(Qt.AlignCenter)
            empty.setStyleSheet(theme.FLEETWIDE_EMPTY)
            self.card_layout.addWidget(empty, 0, 0, 1, column_count)
        self.card_layout.setColumnStretch(0, 1)
        self.card_layout.setColumnStretch(1, 1 if column_count == 2 else 0)
        if self._has_loaded_results:
            mode_label = {
                "all": "all occurrences",
                "operating": "during operation",
                "not_operating": "startup / stopped",
            }.get(operation_mode, "selected occurrences")
            hidden_parts = []
            if zero_hidden:
                hidden_parts.append(f"{zero_hidden} zero")
            if error_hidden:
                hidden_parts.append(f"{error_hidden} error")
            hidden_text = f"; {', '.join(hidden_parts)} hidden" if hidden_parts else ""
            pending_text = (
                " Search/range selection changed; click Run Search to update."
                if self._search_pending else ""
            )
            self.status_label.setText(
                f"{shown_count} systems shown for {mode_label}{hidden_text}.{pending_text}"
            )
