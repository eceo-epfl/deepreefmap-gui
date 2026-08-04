"""What one run is, and why it failed if it did.

One widget, shown twice: in Browse beside the run table, and in View mode beside
the cloud. A failure reason belongs here rather than in the status bar, which the
next event overwrites: the run that broke is still selected long after the
message that explained it has gone.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QGuiApplication, QImageReader, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.core.icons import ICON_SM, check_icon, copy_icon
from deepreefmap_gui.core.image_view import ClickableLabel, ImageDialog
from deepreefmap_gui.core.theme import (
    FONT_LG_PT,
    SPACE_SM,
    TEXT_MUTED,
    WARN_TEXT,
    WEIGHT_SEMIBOLD,
)
from deepreefmap_gui.core.widgets import STATUS_COLORS, KeyValueList, section_card
from deepreefmap_gui.profiling.eta import format_duration
from deepreefmap_gui.profiling.system_probe import format_bytes
from deepreefmap_gui.runs.run_cards import points_label
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


class OrthoDialog(ImageDialog):
    """The ortho at full resolution, fitted to the window and zoomable.

    Fit to width rather than to the whole window: an ortho is a long thin strip
    of transect, so width is the dimension worth spending on and the height
    scrolls. The wheel zooms in about the cursor from there.
    """

    def __init__(self, path: Path, title: str, parent: QWidget | None = None) -> None:
        super().__init__(QPixmap(str(path)), title, parent)


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

    open_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._open_action_allowed = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card, layout = section_card()

        self.title = QLabel("")
        self.title.setWordWrap(True)
        title_font = self.title.font()
        title_font.setWeight(QFont.Weight.DemiBold)
        title_font.setPointSize(FONT_LG_PT)
        self.title.setFont(title_font)
        layout.addWidget(self.title)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.facts = KeyValueList()
        layout.addWidget(self.facts)

        # What the run actually produced, which no amount of metadata conveys.
        # Below the facts rather than above them: the facts identify the run, the
        # ortho is what you look at once you know you have the right one.
        self.ortho = ClickableLabel("")
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

        # Opening the run is what this pane is for; copying its command line is
        # for taking away, so it sits beside as a quiet action rather than
        # holding the only full-width button in the pane, which is what it did.
        actions = QHBoxLayout()
        actions.setSpacing(SPACE_SM)
        self.open_btn = QPushButton("Open run")
        self.open_btn.setProperty("cta", "true")
        self.open_btn.setToolTip("Load this run into the 3D viewer")
        self.open_btn.clicked.connect(self.open_requested)
        self.open_btn.setVisible(False)
        actions.addWidget(self.open_btn)
        actions.addStretch(1)

        self.copy_command_btn = QPushButton("Copy command")
        self.copy_command_btn.setProperty("quiet", "true")
        self.copy_command_btn.setIcon(copy_icon(ICON_SM))
        self.copy_command_btn.setToolTip(
            "Copy the command that reproduces this run in a terminal"
        )
        self.copy_command_btn.clicked.connect(self._copy_command)
        self.copy_command_btn.setVisible(False)
        actions.addWidget(self.copy_command_btn)
        layout.addLayout(actions)
        self._entry: RunEntry | None = None

        outer.addWidget(card)

    def set_open_action_visible(self, visible: bool) -> None:
        """Hide the opener where opening means nothing: View mode is already in it."""
        self._open_action_allowed = visible
        self.open_btn.setVisible(visible and self._entry is not None and not self._entry.incomplete)

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
        self.copy_command_btn.setIcon(check_icon(ICON_SM))
        QTimer.singleShot(1200, lambda: self.copy_command_btn.setIcon(copy_icon(ICON_SM)))

    def show_entry(self, entry: RunEntry) -> None:
        # Imported here rather than at module scope: simple.batch reaches back
        # into the window, and importing it eagerly closes an import cycle.
        from deepreefmap_gui.simple.batch import _diagnose_failure

        status = catalogue.entry_status(entry)
        colour = STATUS_COLORS.get(status, TEXT_MUTED)
        self.title.setText(entry.display_name)
        self.status.setText(
            f'<span style="color:{colour}; font-weight:{WEIGHT_SEMIBOLD};">'
            f"{status.capitalize()}</span>"
        )
        rows = [
            ("Folder", entry.dir_name),
            ("Transect", entry.transect_name or "Not assigned yet"),
            ("Video", entry.video_name or "—"),
        ]
        if entry.duration_s:
            rows.append(("Runtime", format_duration(entry.duration_s)))
        if entry.points:
            # Same abbreviation the run table uses. Spelling it 1,200,000 here
            # and 1.2M there made one number look like two.
            rows.append(("Points", points_label(entry.points)))
        if entry.size_bytes is not None:
            rows.append(("On disk", format_bytes(entry.size_bytes)))
        self.facts.set_rows(rows)
        error = entry.db_run.error if entry.db_run is not None else ""
        if entry.incomplete:
            self.error.setText(
                _diagnose_failure(error)
                if error
                else "This run did not finish and wrote no manifest."
            )
        self.error.setVisible(bool(entry.incomplete))
        self._entry = entry
        # An incomplete run still has a command worth copying: the run_command.sh
        # it wrote before it failed is exactly what a diagnosis starts from.
        self.copy_command_btn.setVisible(True)
        self.open_btn.setVisible(self._open_action_allowed and not entry.incomplete)
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
        self.facts.clear()
        self.error.setVisible(False)
        self._entry = None
        self.copy_command_btn.setVisible(False)
        self.open_btn.setVisible(False)
        self._ortho_source = None
        self._ortho_run_dir = None
        self._rescale_ortho()
