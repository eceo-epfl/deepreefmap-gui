"""Analysis tab: compare repeated passes of a transect."""

from __future__ import annotations

from deepreefmap_gui.core.window_protocol import MixinBase

import logging
import uuid
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.core.theme import GUTTER, TEXT_MUTED
from deepreefmap_gui.core.widgets import EmptyState, section_card

from PySide6.QtGui import QColor

from deepreefmap_gui.cover import COVER_LEVELS
from deepreefmap_gui.map.overlays import OverlayTransect
from deepreefmap_gui.map.widget import SlippyMapWidget
from deepreefmap_gui.simple.charts import GroupedBarChart, pass_color
from deepreefmap_gui.survey.analysis import (
    PooledCover,
    assemble_transect_covers,
    collate_long_format,
    cover_labels,
    latest_run_per_pass,
    pooled_transect_cover,
    repeatability_stats,
    reproducibility_groups,
)
from deepreefmap_gui.survey.models.exporters import save_long_format_csv, save_repeatability_csv

logger = logging.getLogger(__name__)

# Below this cover fraction a class is noise in the chart; the CSV keeps everything.
_CHART_MIN_FRACTION = 0.005


def transect_status_color(statuses: list[str]) -> QColor:
    """Grey none, red any failure, green all succeeded, amber in between."""
    if not statuses:
        return QColor(128, 128, 128)
    if any(status == "failed" for status in statuses):
        return QColor(200, 70, 60)
    if all(status == "succeeded" for status in statuses):
        return QColor(70, 170, 90)
    return QColor(220, 160, 40)


class SimpleAnalysisMixin(MixinBase):
    """DeepReefMapWindow methods for the survey analysis tab."""

    def _build_analysis_page(self) -> QWidget:
        """Full-page Analyse section: transect map beside the cover chart, with
        repeatability stats and the run list below."""
        self._analysis_covers = []
        self._analysis_all_covers = []

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(GUTTER)

        top = QSplitter(Qt.Orientation.Horizontal)
        top.setHandleWidth(GUTTER)
        map_card, map_layout = section_card("Where")
        self._analysis_map = SlippyMapWidget()
        self._analysis_map.setMinimumHeight(200)
        self._analysis_map.transect_clicked.connect(self._on_analysis_map_transect_clicked)
        map_layout.addWidget(self._analysis_map, 1)
        top.addWidget(map_card)

        chart_card, chart_layout = section_card()
        selector = QHBoxLayout()
        selector.setSpacing(6)
        selector.addWidget(QLabel("Transect"))
        self._analysis_transect_combo = QComboBox()
        self._analysis_transect_combo.currentIndexChanged.connect(
            lambda *_: self._on_analysis_transect_changed()
        )
        selector.addWidget(self._analysis_transect_combo, 1)
        # "Detail" rather than "Level": the combo picks how finely the classes
        # are grouped, which the old label said nothing about.
        detail_label = QLabel("Detail")
        detail_tip = (
            "How finely classes are grouped in the chart and the stats: "
            "fine is every class on its own, coarse is broad groups."
        )
        detail_label.setToolTip(detail_tip)
        selector.addWidget(detail_label)
        self._analysis_level_combo = QComboBox()
        self._analysis_level_combo.addItems(list(COVER_LEVELS))
        self._analysis_level_combo.setCurrentText("intermediate")
        self._analysis_level_combo.setToolTip(detail_tip)
        self._analysis_level_combo.currentTextChanged.connect(
            lambda *_: self._refresh_survey_analysis()
        )
        selector.addWidget(self._analysis_level_combo)
        chart_layout.addLayout(selector)
        # The defensible headline: the count-weighted pool and how many passes
        # it rests on. The per-pass bars below are the spread, not the estimate.
        self._analysis_estimate_label = QLabel("")
        self._analysis_estimate_label.setWordWrap(True)
        self._analysis_estimate_label.setStyleSheet(f"color: {TEXT_MUTED};")
        chart_layout.addWidget(self._analysis_estimate_label)
        self._analysis_chart = GroupedBarChart()
        chart_layout.addWidget(self._analysis_chart, 1)
        top.addWidget(chart_card)
        top.setStretchFactor(0, 1)
        top.setStretchFactor(1, 2)
        # Explicit sizes: left to itself the splitter gives the map whatever the
        # chart's size hint leaves over, which is a sliver too narrow to read.
        map_card.setMinimumWidth(280)
        top.setSizes([420, 840])
        layout.addWidget(top, 3)

        bottom = QSplitter(Qt.Orientation.Horizontal)
        bottom.setHandleWidth(GUTTER)
        stats_card, stats_layout = section_card("Cover estimate and repeatability by class")
        self._analysis_stats_table = QTableWidget(0, 6)
        # "Cover" is the count-weighted transect estimate. "Mean of passes" is
        # the unweighted average of per-pass fractions, kept only as a spread
        # reference so it is never read as the cover figure.
        self._analysis_stats_table.setHorizontalHeaderLabels(
            ["Class", "Cover", "Mean of passes", "Std", "CV", "Range"]
        )
        self._analysis_stats_table.verticalHeader().setVisible(False)
        self._analysis_stats_table.setShowGrid(False)
        self._analysis_stats_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._analysis_stats_stack = QStackedWidget()
        self._analysis_stats_stack.addWidget(self._analysis_stats_table)
        self._analysis_stats_stack.addWidget(
            EmptyState(
                "No repeatability yet",
                "Process at least two passes of this transect to compare them.",
            )
        )
        stats_layout.addWidget(self._analysis_stats_stack, 1)

        self._analysis_repro_label = QLabel("")
        self._analysis_repro_label.setWordWrap(True)
        self._analysis_repro_label.setStyleSheet(f"color: {TEXT_MUTED};")
        self._analysis_repro_label.setVisible(False)
        stats_layout.addWidget(self._analysis_repro_label)
        bottom.addWidget(stats_card)

        runs_card, runs_layout = section_card("Runs")
        self._analysis_runs_list = QListWidget()
        self._analysis_runs_list.setToolTip("Double-click a run to open it in the viewer.")
        self._analysis_runs_list.itemDoubleClicked.connect(self._on_analysis_run_opened)
        self._analysis_runs_stack = QStackedWidget()
        self._analysis_runs_stack.addWidget(self._analysis_runs_list)
        self._analysis_runs_stack.addWidget(
            EmptyState("No runs for this transect", "Process a pass on the Run step.")
        )
        runs_layout.addWidget(self._analysis_runs_stack, 1)
        bottom.addWidget(runs_card)
        bottom.setStretchFactor(0, 2)
        bottom.setStretchFactor(1, 1)
        layout.addWidget(bottom, 2)

        export_row = QHBoxLayout()
        self._analysis_export_btn = QPushButton("Export repeatability CSV")
        self._analysis_export_btn.clicked.connect(self._on_analysis_export_csv)
        export_row.addWidget(self._analysis_export_btn)
        self._analysis_collated_btn = QPushButton("Export collated cover CSV")
        self._analysis_collated_btn.clicked.connect(self._on_analysis_export_collated)
        export_row.addWidget(self._analysis_collated_btn)
        export_row.addStretch(1)
        layout.addLayout(export_row)
        self._update_analysis_export_button()
        return page

    def _update_analysis_export_button(self) -> None:
        """Say up front that there is nothing to export, rather than after the click."""
        ready = bool(self._analysis_covers)
        self._analysis_export_btn.setEnabled(ready)
        self._analysis_export_btn.setToolTip(
            "Write mean, standard deviation, CV and range per class to a CSV."
            if ready
            else "Nothing to export yet: this transect has no completed passes."
        )
        self._analysis_collated_btn.setEnabled(ready)
        self._analysis_collated_btn.setToolTip(
            "Write one long-format row per transect/pass/class/level, plus the "
            "count-weighted pooled estimate, with GUI and taxonomy provenance."
            if ready
            else "Nothing to export yet: no completed passes."
        )

    def _analysis_transect_id(self) -> uuid.UUID | None:
        data = self._analysis_transect_combo.currentData()
        return uuid.UUID(data) if data else None

    def _on_analysis_transect_changed(self) -> None:
        """Changing the transect here also moves the browser above it."""
        self._refresh_survey_analysis()
        self._set_scope_transect(self._analysis_transect_id())

    def _refresh_survey_analysis(self) -> None:
        store = self._survey_store()
        combo = self._analysis_transect_combo
        selected = combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        for transect in store.list_transects():
            combo.addItem(transect.name, str(transect.id))
            if selected is not None and str(transect.id) == selected:
                combo.setCurrentIndex(combo.count() - 1)
        combo.blockSignals(False)

        transect_id = self._analysis_transect_id()
        self._refresh_analysis_map(store, transect_id)
        if transect_id is None:
            self._analysis_covers = []
            self._analysis_all_covers = []
            self._analysis_chart.set_data([], [])
            self._analysis_stats_table.setRowCount(0)
            self._analysis_repro_label.setText("")
            self._analysis_estimate_label.setText("")
            self._analysis_runs_list.clear()
            self._refresh_analysis_empty_states()
            return

        out_root = Path(self._out_root_input.text()).expanduser()
        # Read every succeeded run once, then split: the deduped set (one latest
        # run per pass) drives the estimate and the chart, the full set feeds
        # reproducibility, whose whole point is the reruns dedupe removes.
        all_covers = assemble_transect_covers(
            store,
            out_root,
            transect_id,
            self._classes_config,
            level=self._analysis_level_combo.currentText(),
            dedupe=False,
        )
        covers = latest_run_per_pass(all_covers)
        self._analysis_all_covers = all_covers
        self._analysis_covers = covers
        expected = len(store.list_passes(transect_id=transect_id))
        pooled = pooled_transect_cover(covers, expected_passes=expected)
        series = [
            (
                f"{index} {'fwd' if c.direction == 'forward' else 'rev'}",
                c.cover,
                pass_color(c.direction, index - 1),
            )
            for index, c in enumerate(covers, start=1)
        ]
        self._analysis_chart.set_data(cover_labels(covers, _CHART_MIN_FRACTION), series)
        self._update_analysis_estimate_label(pooled)
        self._fill_analysis_stats(covers, pooled)
        self._fill_analysis_repro(all_covers)
        self._fill_analysis_runs(store, out_root, transect_id)
        self._refresh_analysis_empty_states()

    def _update_analysis_estimate_label(self, pooled: PooledCover) -> None:
        """State the estimator and how many passes back the number, up front."""
        if not pooled.counts:
            self._analysis_estimate_label.setText("")
            return
        passes = (
            f"{pooled.contributing_passes} of {pooled.expected_passes} "
            f"pass{'es' if pooled.expected_passes != 1 else ''}"
        )
        self._analysis_estimate_label.setText(
            f"Transect cover estimate: count-weighted pool of {passes}. "
            "Bars below show each pass, not the estimate."
        )

    def _refresh_analysis_empty_states(self) -> None:
        """Show each pane's placeholder while it has nothing to say."""
        self._analysis_stats_stack.setCurrentIndex(
            0 if self._analysis_stats_table.rowCount() else 1
        )
        self._analysis_runs_stack.setCurrentIndex(
            0 if self._analysis_runs_list.count() else 1
        )
        self._update_analysis_export_button()

    def _refresh_analysis_map(self, store, selected_id: uuid.UUID | None) -> None:
        overlays = []
        for transect in store.list_transects():
            runs = store.runs_for_transect(transect.id)
            statuses = [run.status for run in runs]
            overlays.append(OverlayTransect(
                id=str(transect.id),
                start=(transect.start_lat, transect.start_lon),
                end=(transect.end_lat, transect.end_lon),
                color=transect_status_color(statuses),
                selected=transect.id == selected_id,
                label=transect.name,
                tooltip=self._transect_tooltip(store, transect, runs),
            ))
        self._analysis_map.set_transects(overlays)
        self._analysis_map.fit_transects()

    def _transect_tooltip(self, store, transect, runs: list) -> str:
        """What has actually been surveyed here, without opening the transect."""
        passes = store.list_passes(transect_id=transect.id)
        videos = {video_id for p in passes for video_id in p.video_ids()}
        done = sum(1 for run in runs if run.status == "succeeded")
        failed = sum(1 for run in runs if run.status == "failed")
        lines = [f"<b>{transect.name}</b>"]
        lines.append(f"{len(videos)} video{'s' if len(videos) != 1 else ''}"
                     f" · {len(passes)} pass{'es' if len(passes) != 1 else ''}")
        if runs:
            summary = f"{done} of {len(runs)} run{'s' if len(runs) != 1 else ''} succeeded"
            if failed:
                summary += f", {failed} failed"
            lines.append(summary)
            last = max((run.started_at or run.created_at for run in runs), default="")
            if last:
                lines.append(f"Last run {last[:10]}")
        else:
            lines.append("Not processed yet")
        if transect.length_m:
            lines.append(f"{transect.length_m:g} m tape")
        return "<br>".join(lines)

    def _on_analysis_map_transect_clicked(self, transect_id: str) -> None:
        combo = self._analysis_transect_combo
        for index in range(combo.count()):
            if combo.itemData(index) == transect_id:
                combo.setCurrentIndex(index)
                return

    def _fill_analysis_stats(self, covers: list, pooled: PooledCover) -> None:
        stats = repeatability_stats(covers)
        labels = cover_labels(covers)
        table = self._analysis_stats_table
        table.setRowCount(len(labels))
        for row, label in enumerate(labels):
            entry = stats[label]
            cells = [
                label,
                f"{pooled.cover.get(label, 0.0) * 100:.1f}%",
                f"{entry['mean'] * 100:.1f}%",
                f"{entry['std'] * 100:.1f}%",
                f"{entry['cv']:.2f}",
                f"{entry['range'] * 100:.1f}%",
            ]
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if column > 0:
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                table.setItem(row, column, item)

    def _fill_analysis_repro(self, covers: list) -> None:
        groups = reproducibility_groups(covers)
        self._analysis_repro_label.setVisible(bool(groups))
        if not groups:
            self._analysis_repro_label.setText("")
            return
        lines = []
        for group in groups:
            stats = repeatability_stats(group)
            worst = max((entry["range"] for entry in stats.values()), default=0.0)
            first = group[0]
            lines.append(
                f"#{(first.video_hash or '')[:8]} {first.begin_s:.0f}-{first.end_s:.0f}s: "
                f"{len(group)} runs, max class spread {worst * 100:.1f}%"
            )
        self._analysis_repro_label.setText(
            "Reproducibility (identical footage and trim):\n" + "\n".join(lines)
        )

    def _fill_analysis_runs(self, store, out_root: Path, transect_id: uuid.UUID) -> None:
        self._analysis_runs_list.clear()
        for run in store.runs_for_transect(transect_id):
            item = QListWidgetItem(f"{run.run_dir_name}  [{run.status}]")
            if run.status == "succeeded":
                item.setData(Qt.ItemDataRole.UserRole, str(out_root / run.run_dir_name))
            self._analysis_runs_list.addItem(item)

    def _on_analysis_run_opened(self, item: QListWidgetItem) -> None:
        run_dir = item.data(Qt.ItemDataRole.UserRole)
        if run_dir:
            self._auto_load_run(Path(run_dir))

    def _on_analysis_export_csv(self) -> None:
        covers = self._analysis_covers
        if not covers:
            return
        name = self._analysis_transect_combo.currentText() or "transect"
        path_str, _ = QFileDialog.getSaveFileName(
            self,
            "Export repeatability CSV",
            str(Path(self._out_root_input.text()) / f"{name}_repeatability.csv"),
            "CSV files (*.csv)",
        )
        if not path_str:
            return
        save_repeatability_csv(
            Path(path_str), cover_labels(covers), repeatability_stats(covers), covers
        )
        self._status_label.setText(f"Exported repeatability CSV for {name}.")

    def _on_analysis_export_collated(self) -> None:
        """Collate every transect into one long-format CSV with provenance."""
        store = self._survey_store()
        out_root = Path(self._out_root_input.text()).expanduser()
        path_str, _ = QFileDialog.getSaveFileName(
            self,
            "Export collated cover CSV",
            str(out_root / "survey_cover_long.csv"),
            "CSV files (*.csv)",
        )
        if not path_str:
            return
        rows = collate_long_format(store, out_root, self._classes_config)
        save_long_format_csv(Path(path_str), rows)
        transects = len({row.transect_id for row in rows})
        self._status_label.setText(
            f"Exported collated cover CSV: {len(rows)} rows across {transects} transects."
        )
