"""What one run is, and why it failed if it did.

One widget, shown twice: in Browse beside the run table, and in View mode beside
the cloud. A failure reason belongs here rather than in the status bar, which the
next event overwrites — the run that broke is still selected long after the
message that explained it has gone.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QImageReader, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.core.icons import check_icon, copy_icon
from deepreefmap_gui.core.theme import TEXT_MUTED, TEXT_SECONDARY, WARN_TEXT
from deepreefmap_gui.core.widgets import STATUS_COLORS, section_card
from deepreefmap_gui.profiling.eta import format_duration
from deepreefmap_gui.profiling.system_probe import format_bytes
from deepreefmap_gui.survey import catalogue
from deepreefmap_gui.survey.catalogue import RunEntry

# Orthos are ~2000px wide and a couple of megabytes; the pane shows them at a
# few hundred. QImageReader decodes straight to this width rather than decoding
# full size and throwing most of it away, which is what makes arrowing down the
# run table stay responsive.
_ORTHO_DECODE_WIDTH = 640

# Bounded because the pane holds one run at a time and the cache only exists to
# make going back to the previous few selections instant.
_ORTHO_CACHE_MAX = 12

# Card padding either side of the strip, so it is sized off the pane rather
# than off its own label.
_ORTHO_H_MARGIN = 44

# Frame and scrollbar the full-size dialog spends on its own chrome.
_DIALOG_CHROME = 24


class _ClickableLabel(QLabel):
    """A label that reports clicks, for the ortho thumbnail."""

    clicked = Signal()

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class OrthoDialog(QDialog):
    """The ortho at full resolution, fitted to the window and rescaled with it.

    Fit to width rather than to the whole window: an ortho is a long thin strip
    of transect, so width is the dimension worth spending on and the height
    scrolls.
    """

    def __init__(self, path: Path, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self._source = QPixmap(str(path))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._image = QLabel()
        self._image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._image)
        layout.addWidget(scroll)

        screen = QGuiApplication.primaryScreen()
        bounds = screen.availableSize() if screen is not None else QSize(1280, 800)
        self.resize(
            min(self._source.width(), int(bounds.width() * 0.9)),
            min(self._source.height(), int(bounds.height() * 0.9)),
        )
        self._fit()

    def _fit(self) -> None:
        if self._source.isNull():
            return
        width = min(max(self.width() - _DIALOG_CHROME, 1), self._source.width())
        self._image.setPixmap(
            self._source.scaledToWidth(width, Qt.TransformationMode.SmoothTransformation)
        )

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().resizeEvent(event)
        self._fit()


def _load_ortho(run_dir: Path) -> QPixmap | None:
    """The run's ortho strip, decoded small, or None if it never wrote one."""
    path = run_dir / "ortho.png"
    if not path.is_file():
        return None
    reader = QImageReader(str(path))
    size = reader.size()
    if size.isValid() and size.width() > _ORTHO_DECODE_WIDTH:
        scale = _ORTHO_DECODE_WIDTH / size.width()
        reader.setScaledSize(QSize(_ORTHO_DECODE_WIDTH, max(1, round(size.height() * scale))))
    image = reader.read()
    return QPixmap.fromImage(image) if not image.isNull() else None


class RunDetailPanel(QWidget):
    """A titled card describing the selected run."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card, layout = section_card()

        self.title = QLabel("")
        self.title.setWordWrap(True)
        self.title.setStyleSheet("font-weight: bold;")
        layout.addWidget(self.title)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.facts = QLabel("")
        self.facts.setWordWrap(True)
        self.facts.setTextFormat(Qt.TextFormat.RichText)
        self.facts.setStyleSheet(f"color: {TEXT_SECONDARY};")
        self.facts.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
        )
        layout.addWidget(self.facts)

        # What the run actually produced, which no amount of metadata conveys.
        # Below the facts rather than above them: the facts identify the run, the
        # ortho is what you look at once you know you have the right one.
        self.ortho = _ClickableLabel("")
        self.ortho.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        # Ignored horizontally: a pixmap's size hint is its own width, so the
        # thumbnail would otherwise widen the pane to fit itself and shove the
        # run table aside every time a selection changed.
        self.ortho.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed
        )
        self.ortho.setCursor(Qt.CursorShape.PointingHandCursor)
        self.ortho.setToolTip("Click to open the ortho at full size")
        self.ortho.clicked.connect(self._open_ortho)
        self.ortho.setVisible(False)
        layout.addWidget(self.ortho)
        layout.addStretch(1)
        self._ortho_pixmaps: dict[Path, QPixmap | None] = {}
        self._ortho_source: QPixmap | None = None
        self._ortho_run_dir: Path | None = None
        self._ortho_title = ""

        self.error = QLabel("")
        self.error.setWordWrap(True)
        self.error.setStyleSheet(f"color: {WARN_TEXT};")
        self.error.setVisible(False)
        layout.addWidget(self.error)

        # What this run was, as a terminal command. Below everything else because
        # it is for taking away — reproducing the run elsewhere or filing it in a
        # bug report — not for reading in place.
        self.copy_command_btn = QPushButton("Copy CLI command")
        self.copy_command_btn.setIcon(copy_icon(14))
        self.copy_command_btn.setToolTip(
            "Copy the command that reproduces this run in a terminal"
        )
        self.copy_command_btn.clicked.connect(self._copy_command)
        self.copy_command_btn.setVisible(False)
        layout.addWidget(self.copy_command_btn)
        self._entry: RunEntry | None = None

        outer.addWidget(card)

    def _copy_command(self) -> None:
        from deepreefmap_gui.runs.run_command import command_from_manifest

        if self._entry is None:
            return
        text = command_from_manifest(self._entry.manifest, self._entry.run_dir)
        QGuiApplication.clipboard().setText(text)
        QToolTip.showText(
            self.copy_command_btn.mapToGlobal(self.copy_command_btn.rect().topRight()),
            "Copied to clipboard",
            self.copy_command_btn,
        )
        self.copy_command_btn.setIcon(check_icon(14))
        QTimer.singleShot(1200, lambda: self.copy_command_btn.setIcon(copy_icon(14)))

    def show_entry(self, entry: RunEntry) -> None:
        # Imported here rather than at module scope: simple.batch reaches back
        # into the window, and importing it eagerly closes an import cycle.
        from deepreefmap_gui.simple.batch import _diagnose_failure

        status = catalogue.entry_status(entry)
        colour = STATUS_COLORS.get(status, TEXT_MUTED)
        self.title.setText(entry.display_name)
        self.status.setText(
            f'<span style="color:{colour}; font-weight:600;">{status.capitalize()}</span>'
        )
        rows = [
            ("Folder", entry.dir_name),
            ("Transect", entry.transect_name or "Not assigned yet"),
            ("Video", entry.video_name or "—"),
        ]
        if entry.duration_s:
            rows.append(("Runtime", format_duration(entry.duration_s)))
        if entry.points:
            rows.append(("Points", f"{entry.points:,}"))
        if entry.size_bytes is not None:
            rows.append(("On disk", format_bytes(entry.size_bytes)))
        self.facts.setText(
            "<br>".join(f"<b>{label}</b>  {value}" for label, value in rows)
        )
        error = entry.db_run.error if entry.db_run is not None else ""
        if entry.incomplete:
            self.error.setText(
                _diagnose_failure(error)
                if error
                else "This run did not finish and wrote no manifest."
            )
        self.error.setVisible(bool(entry.incomplete))
        self._entry = entry
        # An incomplete run still has a command worth copying — the run_command.sh
        # it wrote before it failed is exactly what a diagnosis starts from.
        self.copy_command_btn.setVisible(True)
        self._show_ortho(entry.run_dir, entry.display_name)

    def _show_ortho(self, run_dir: Path, title: str) -> None:
        if run_dir not in self._ortho_pixmaps:
            if len(self._ortho_pixmaps) >= _ORTHO_CACHE_MAX:
                self._ortho_pixmaps.pop(next(iter(self._ortho_pixmaps)))
            self._ortho_pixmaps[run_dir] = _load_ortho(run_dir)
        self._ortho_source = self._ortho_pixmaps[run_dir]
        self._ortho_run_dir = run_dir
        self._ortho_title = title
        self._rescale_ortho()

    def _rescale_ortho(self) -> None:
        """Fit the strip to the pane, never blown up past what was decoded.

        Driven off this panel's width rather than the label's: the label is
        width-Ignored so it can never widen the pane, which also means its own
        width lags a resize by a layout pass.
        """
        if self._ortho_source is None:
            self.ortho.clear()
            self.ortho.setVisible(False)
            return
        available = max(1, self.width() - _ORTHO_H_MARGIN)
        width = min(available, self._ortho_source.width())
        scaled = self._ortho_source.scaledToWidth(
            width, Qt.TransformationMode.SmoothTransformation
        )
        self.ortho.setPixmap(scaled)
        # Fixed, so the row the strip occupies is exactly as tall as the strip
        # and the facts above it do not shuffle as the pane is dragged.
        if self.ortho.height() != scaled.height():
            self.ortho.setFixedHeight(scaled.height())
        self.ortho.setVisible(True)

    def _open_ortho(self) -> None:
        if self._ortho_run_dir is None or self._ortho_source is None:
            return
        path = self._ortho_run_dir / "ortho.png"
        if not path.is_file():
            return
        OrthoDialog(path, self._ortho_title or path.parent.name, self).exec()

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().resizeEvent(event)
        self._rescale_ortho()

    def clear(self) -> None:
        self.title.setText("")
        self.status.setText("")
        self.facts.setText("")
        self.error.setVisible(False)
        self._entry = None
        self.copy_command_btn.setVisible(False)
        self._ortho_source = None
        self._ortho_run_dir = None
        self._rescale_ortho()
