"""The rows the Videos page is a list of: one clip each, and what was cut from it.

Five widgets, innermost first: a strip painting a clip's sections along its
length, a row describing one clip, a row describing one section, the scrolling
list of date groups those rows sit in, and the header row naming the clip
columns and sorting them. A clip's section rows sit directly under it and are
shown by its disclosure chevron, so the three levels the app has (clip,
section, run) read as the nesting they are.

The list builds a widget per clip, which suits a field season and would not suit
tens of thousands of them. The escape hatch is a QListView over a model with a
delegate: the strip's paint code moves across unchanged, since it already draws
from a rect and a list of spans rather than from its own children.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from PySide6.QtCore import QPointF, QRectF, Qt, Signal, SignalInstance
from PySide6.QtGui import (
    QColor,
    QContextMenuEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMenu,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.core.icons import (
    ICON_SM,
    broken_link_icon,
    folder_icon,
    link_icon,
    play_icon,
    status_dot_icon,
)
from deepreefmap_gui.core.theme import (
    BAR_HEIGHT,
    BORDER,
    CARD_BG,
    CONTROL_HEIGHT,
    GROOVE,
    HEADER_PAD_V,
    RADIUS_SM,
    SELECTION_BG,
    SPACE_SM,
    SPACE_XS,
    SUCCESS,
    TEXT_DIM,
    TEXT_MUTED,
    WINDOW_TEXT,
)
from deepreefmap_gui.core.widgets import (
    PILL_PROGRESS_ALPHA,
    PILL_TINT_ALPHA,
    STATUS_COLORS,
    SectionHeader,
    muted_label,
    secondary_label,
)
from deepreefmap_gui.profiling.system_probe import format_bytes
from deepreefmap_gui.survey import statuses
from deepreefmap_gui.survey.catalogue import LINK_LINKED, LINK_MISSING, VideoLibraryEntry
from deepreefmap_gui.survey.models.run_record import RunRecord
from deepreefmap_gui.survey.models.transect_pass import TransectPass
from deepreefmap_gui.survey.models.video_asset import VideoAsset
from deepreefmap_gui.survey.video_groups import (
    DEFAULT_SORT_COLUMN,
    DEFAULT_SORT_DESCENDING,
    SORT_LENGTH,
    SORT_NAME,
    SORT_RECORDED,
    SORT_SIZE,
    DateGroup,
    Span,
    capture_moment,
    pass_status,
    timeline_spans,
)
from deepreefmap_gui.survey.video_probe import NO, SOURCE_CONTAINER, YES

# The strip is one of the app's bars, so it takes bar_qss's geometry: the same
# height, and the round ends MeterBar paints at half that height.
STRIP_BAR_HEIGHT = BAR_HEIGHT
# Rerun ticks sit above the bar rather than inside it, so the widget is taller.
TICK_HEIGHT = SPACE_XS
STRIP_HEIGHT = STRIP_BAR_HEIGHT + TICK_HEIGHT

# A hairline, the weight MeterBar's hatching is drawn at.
LINE_WIDTH = 2.0
# One tick's own width between ticks, so a run of them still reads as separate.
TICK_PITCH = LINE_WIDTH * 2

# A ten-second section of an hour-long clip is under a pixel wide. Painted at
# least this wide so it can be seen and hit.
MIN_SPAN_WIDTH = SPACE_SM

# Pitch of the hatching over a clip whose length is unknown.
HATCH_PITCH = SPACE_SM

# Column widths in characters rather than pixels, so a column follows the user's
# font size instead of holding a measure that no longer fits it. Each column is
# wide enough for its header label and a sort arrow as well as its values.
NAME_CHARS = 30  # a GoPro file name, with room for the ones that are not
RECORDED_CHARS = 10  # "~14:32", "Recorded ▼"
LENGTH_CHARS = 9  # "12m 03s", "Length ▼"
SIZE_CHARS = 9  # "1015 MB", "Size ▼"
GRAVITY_CHARS = 10  # "Gravity", plus its dot
WINDOW_CHARS = 14  # "0:00–11:51"
TRANSECT_CHARS = 22  # a transect name, or "Unassigned"
DIRECTION_CHARS = 9  # "Forward"
RUNS_CHARS = 9  # "12 runs"

# One row: the play button plus a hair, so a card of footage fits on a screen.
ROW_HEIGHT = CONTROL_HEIGHT + SPACE_XS

# The disclosure column keeps its width on a clip with nothing to disclose, so
# the file names stay in one column all the way down the list.
DISCLOSURE_WIDTH = ICON_SM + SPACE_XS

# A section row starts where its clip's name does. That alignment is what makes
# the nesting read without drawing a connecting line for it.
SECTION_INDENT = SPACE_SM + DISCLOSURE_WIDTH + SPACE_SM + ICON_SM

UNASSIGNED_NAME = "Unassigned"

# The list takes a drop, and nothing else in the app says so.
DROP_HINT = "Drop clips here to import them"

IN_CART_NOTE = "In cart"
IN_CART_TOOLTIP = "This section is already in the cart."

# The section menu, named once so the page and the tests read the same words.
MENU_ADD_TO_CART = "Add to cart"
MENU_RETRIM = "Adjust trim…"
MENU_REASSIGN = "Change transect…"
MENU_DELETE = "Delete section"
DELETE_BLOCKED_TOOLTIP = (
    "This section has runs. Delete those in Browse first, and the section can go with them."
)

# A row's overflow menu, and the clip row's "cut a new section". Both are single
# glyphs rather than icons: the meaning is the character, and drawing either one
# would leave the icon layer with a shape it has no other use for.
MENU_GLYPH = "⋯"
NEW_SECTION_GLYPH = "+"

UNKNOWN_LENGTH_TOOLTIP = (
    "Length unknown, so there is nowhere to draw this clip's sections along it."
)
GRAVITY_UNKNOWN_TOOLTIP = "Gravity not read yet."
ESTIMATED_DATE_NOTE = (
    "The recording date is the file's own timestamp: the clip carries none of "
    "its own, which is what re-encoding or trimming leaves behind."
)
MISSING_FILE_NOTE = "Not found. Relocate… points the clip at the file's new home."


def _clock(seconds: float) -> str:
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


def length_label(duration_s: float | None) -> str:
    """A clip's length as "12m 03s", or nothing when it was never read."""
    if not duration_s or duration_s <= 0:
        return ""
    total = int(round(duration_s))
    return f"{total // 60}m {total % 60:02d}s"


def capture_label(video: VideoAsset) -> str:
    """The time of day the clip was shot, marked "~" when it is the file's own.

    A re-encoded clip carries no recording time, so its mtime stands in. That is
    a worse answer rather than a wrong one, and a diver reading a day's footage
    in order needs to know which of the two they are looking at.
    """
    stamp = capture_moment(video)
    if stamp is None:
        return ""
    shown = stamp.astimezone().strftime("%H:%M")
    return shown if video.captured_source == SOURCE_CONTAINER else f"~{shown}"


def size_label(size_bytes: int | None) -> str:
    """A clip's size on disk, or nothing when it was never read."""
    return format_bytes(size_bytes) if size_bytes else ""


def window_label(pass_: TransectPass) -> str:
    """A section's time window, which is what tells two sections of one clip apart."""
    return f"{_clock(pass_.begin_s)}–{_clock(pass_.end_s)}"


def run_label(count: int) -> str:
    """"3 runs", and nothing at all until a section has been processed once."""
    if count < 1:
        return ""
    return f"{count} run" if count == 1 else f"{count} runs"


def section_facts(entry: VideoLibraryEntry) -> list[tuple[TransectPass, str, int]]:
    """Each section of a clip in time order, with its status and its run count.

    The status comes from ``video_groups``, which is where that rule lives: the
    strip and the rows describe the same section, and a second copy of the rule
    is how the two come to disagree. A pass whose clip has no readable length
    gets no span, and still gets a row.
    """
    spans = {span.pass_id: span for span in timeline_spans(entry)}
    runs: dict[object, list[RunRecord]] = {}
    for run in entry.runs:
        runs.setdefault(run.pass_id, []).append(run)
    facts = []
    for pass_ in sorted(entry.passes, key=lambda p: (p.begin_s, p.end_s)):
        span = spans.get(str(pass_.id))
        if span is not None:
            facts.append((pass_, span.status, span.run_count))
            continue
        mine = runs.get(pass_.id, [])
        facts.append((pass_, pass_status(mine, held=pass_.held), len(mine)))
    return facts


def _fixed_width(label: QLabel, chars: int) -> None:
    label.setFixedWidth(label.fontMetrics().averageCharWidth() * chars)


def _selectable(widget: QWidget, name: str) -> None:
    """Give a row the list's selection fill, keyed off a ``selected`` property."""
    widget.setObjectName(name)
    # A bare QWidget takes its background from the palette and ignores the
    # stylesheet's, which leaves the selection fill invisible.
    widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    widget.setStyleSheet(
        f'QWidget#{name}[selected="true"] {{ background-color: {SELECTION_BG};'
        f" border-radius: {RADIUS_SM}px; }}"
    )
    widget.setProperty("selected", False)


def _set_selected(widget: QWidget, chosen: bool) -> None:
    if widget.property("selected") == chosen:
        return
    widget.setProperty("selected", chosen)
    # A property a stylesheet selects on is only re-read on a repolish.
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)


def _quiet_button(glyph: str, name: str, tooltip: str) -> QToolButton:
    """One of a row's trailing single-glyph buttons."""
    button = QToolButton()
    button.setText(glyph)
    button.setAccessibleName(name)
    button.setToolTip(tooltip)
    button.setProperty("quiet", "true")
    return button


class SectionStrip(QWidget):
    """Where a clip's sections sit along its length, as one of the app's bars."""

    span_clicked = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(STRIP_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        self._spans: list[Span] = []
        self._duration = 0.0
        self._names: dict[str, str] = {}

    def set_spans(
        self,
        spans: Sequence[Span],
        duration_s: float | None = None,
        names: Mapping[str, str] | None = None,
    ) -> None:
        """The sections, and the clip length they were normalised against.

        Without a length there is no scale to place them on, so the strip says
        so rather than spreading the sections it was given over a guess.
        """
        self._duration = float(duration_s or 0.0)
        self._spans = list(spans) if self._duration > 0 else []
        self._names = dict(names or {})
        self.setToolTip(UNKNOWN_LENGTH_TOOLTIP if self._duration <= 0 else "")
        self.update()

    @property
    def spans(self) -> list[Span]:
        return list(self._spans)

    def span_at(self, x: float) -> str | None:
        """The pass whose section covers ``x``, or None over bare groove."""
        for span, rect in self._span_rects():
            if rect.left() <= x <= rect.right():
                return span.pass_id
        return None

    def _bar_rect(self) -> QRectF:
        return QRectF(0.0, float(TICK_HEIGHT), float(self.width()), float(STRIP_BAR_HEIGHT))

    def _span_rects(self) -> list[tuple[Span, QRectF]]:
        track = self._bar_rect()
        rects = []
        for span in self._spans:
            left = track.left() + track.width() * span.begin
            width = max(MIN_SPAN_WIDTH, track.width() * (span.end - span.begin))
            width = min(width, max(MIN_SPAN_WIDTH, track.right() - left))
            rects.append((span, QRectF(left, track.top(), width, track.height())))
        return rects

    def _span_colour(self, span: Span) -> QColor:
        return QColor(STATUS_COLORS.get(span.status, TEXT_MUTED))

    def _span_tooltip(self, span: Span) -> str:
        name = self._names.get(span.pass_id) or UNASSIGNED_NAME
        window = f"{_clock(span.begin * self._duration)}–{_clock(span.end * self._duration)}"
        return f"{name}  ·  {window}  ·  {statuses.status_label(span.status)}"

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        pass_id = self.span_at(event.position().x())
        span = next((s for s in self._spans if s.pass_id == pass_id), None)
        if span is not None:
            self.setToolTip(self._span_tooltip(span))
        elif self._duration > 0:
            self.setToolTip("")
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            pass_id = self.span_at(event.position().x())
            if pass_id is not None:
                self.span_clicked.emit(pass_id)
        super().mousePressEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        track = self._bar_rect()
        radius = track.height() / 2.0
        groove = QPainterPath()
        groove.addRoundedRect(track, radius, radius)
        painter.fillPath(groove, QColor(GROOVE))
        if self._duration <= 0:
            self._paint_unknown(painter, groove, track)
            painter.end()
            return
        for span, rect in self._span_rects():
            self._paint_span(painter, span, rect, radius)
        painter.end()

    def _paint_span(
        self, painter: QPainter, span: Span, rect: QRectF, radius: float
    ) -> None:
        colour = self._span_colour(span)
        fill = QColor(colour)
        # A section not started yet is a claim about the future, so it is tinted
        # at the weight a chip is rather than the weight a filled bar is.
        idle = statuses.status_tone(span.status) == statuses.TONE_IDLE
        fill.setAlpha(PILL_TINT_ALPHA if idle else PILL_PROGRESS_ALPHA)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        painter.drawRoundedRect(rect, radius, radius)
        pen = QPen(colour)
        pen.setWidthF(LINE_WIDTH)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect, radius, radius)
        self._paint_ticks(painter, span, rect, colour)

    def _paint_ticks(
        self, painter: QPainter, span: Span, rect: QRectF, colour: QColor
    ) -> None:
        """One tick per run, above a section that has been run more than once.

        A single tick over every processed section would be decoration; the mark
        is there to say a section was done again, which is the thing worth
        finding when two runs of one swim disagree.
        """
        if span.run_count < 2:
            return
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(colour)
        x = rect.left()
        for _ in range(span.run_count):
            if x + LINE_WIDTH > rect.right():
                break
            painter.drawRect(QRectF(x, 0.0, LINE_WIDTH, float(TICK_HEIGHT)))
            x += TICK_PITCH

    def _paint_unknown(self, painter: QPainter, groove: QPainterPath, track: QRectF) -> None:
        """Hatching, as MeterBar draws a figure it cannot report.

        In TEXT_DIM rather than MeterBar's ERROR: a length nobody has read is
        not a fault, it is an answer the library does not have yet.
        """
        painter.setClipPath(groove)
        pen = QPen(QColor(TEXT_DIM))
        pen.setWidthF(LINE_WIDTH)
        painter.setPen(pen)
        x = track.left() - track.height()
        while x < track.right() + track.height():
            painter.drawLine(
                QPointF(x, track.bottom()), QPointF(x + track.height(), track.top())
            )
            x += HATCH_PITCH


class VideoRow(QWidget):
    """One clip: where it is, when it was shot, and what has been cut from it."""

    play_requested = Signal(str)
    reveal_requested = Signal(str)
    new_section_requested = Signal(str)
    expand_toggled = Signal(str, bool)
    activated = Signal(str)
    span_clicked = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        _selectable(self, "videoRow")
        self.setFixedHeight(ROW_HEIGHT)

        row = QHBoxLayout(self)
        row.setContentsMargins(SPACE_SM, 0, SPACE_SM, 0)
        row.setSpacing(SPACE_SM)

        self.chevron = QToolButton()
        self.chevron.setCheckable(True)
        self.chevron.setFixedWidth(DISCLOSURE_WIDTH)
        self.chevron.setAccessibleName("Sections")
        self.chevron.setToolTip("Show the sections cut from this clip.")
        self.chevron.setProperty("quiet", "true")
        self.chevron.setProperty("pad", "none")
        self.chevron.toggled.connect(self._on_chevron)
        row.addWidget(self.chevron)

        self._link = QLabel()
        self._link.setFixedWidth(ICON_SM)
        row.addWidget(self._link)

        self._name = secondary_label()
        _fixed_width(self._name, NAME_CHARS)
        row.addWidget(self._name)

        self._recorded = muted_label()
        _fixed_width(self._recorded, RECORDED_CHARS)
        row.addWidget(self._recorded)

        # Length and size are figures, so they right-align under their header
        # cells and their digits line up down the list.
        self._length = muted_label()
        self._length.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        _fixed_width(self._length, LENGTH_CHARS)
        row.addWidget(self._length)

        self._size = muted_label()
        self._size.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        _fixed_width(self._size, SIZE_CHARS)
        row.addWidget(self._size)

        gravity = QWidget()
        gravity_row = QHBoxLayout(gravity)
        gravity_row.setContentsMargins(0, 0, 0, 0)
        gravity_row.setSpacing(SPACE_XS)
        self._gravity_dot = QLabel()
        self._gravity_dot.setFixedWidth(ICON_SM)
        gravity_row.addWidget(self._gravity_dot)
        self._gravity_text = muted_label()
        gravity_row.addWidget(self._gravity_text)
        gravity_row.addStretch(1)
        _fixed_width(self._gravity_text, GRAVITY_CHARS)
        self._gravity = gravity
        row.addWidget(gravity)

        self.strip = SectionStrip()
        self.strip.span_clicked.connect(self.span_clicked)
        row.addWidget(self.strip, 1)

        self.play_btn = QToolButton()
        self.play_btn.setIcon(play_icon())
        self.play_btn.setAccessibleName("Play")
        self.play_btn.setToolTip("Play")
        self.play_btn.setProperty("quiet", "true")
        self.play_btn.clicked.connect(lambda: self._emit(self.play_requested))
        row.addWidget(self.play_btn)

        self.reveal_btn = QToolButton()
        self.reveal_btn.setIcon(folder_icon())
        self.reveal_btn.setAccessibleName("Show in folder")
        self.reveal_btn.setToolTip("Show in folder")
        self.reveal_btn.setProperty("quiet", "true")
        self.reveal_btn.clicked.connect(lambda: self._emit(self.reveal_requested))
        row.addWidget(self.reveal_btn)

        self.new_section_btn = _quiet_button(
            NEW_SECTION_GLYPH, "Cut a new section", "Cut a new section"
        )
        self.new_section_btn.clicked.connect(lambda: self._emit(self.new_section_requested))
        row.addWidget(self.new_section_btn)

        self._entry: VideoLibraryEntry | None = None
        self._sync_chevron()

    @property
    def entry(self) -> VideoLibraryEntry | None:
        return self._entry

    @property
    def video_id(self) -> str:
        return "" if self._entry is None else str(self._entry.video.id)

    @property
    def expanded(self) -> bool:
        return self.chevron.isChecked()

    def set_expanded(self, expanded: bool) -> None:
        """Open or close the clip without reporting it back as a user's click."""
        blocked = self.chevron.blockSignals(True)
        self.chevron.setChecked(expanded and self._has_sections())
        self.chevron.blockSignals(blocked)
        self._sync_chevron()

    def _has_sections(self) -> bool:
        return self._entry is not None and bool(self._entry.passes)

    def _sync_chevron(self) -> None:
        """Blank and dead on a clip with no sections, so the column stays aligned."""
        has_sections = self._has_sections()
        self.chevron.setEnabled(has_sections)
        if not has_sections:
            self.chevron.setArrowType(Qt.ArrowType.NoArrow)
        elif self.chevron.isChecked():
            self.chevron.setArrowType(Qt.ArrowType.DownArrow)
        else:
            self.chevron.setArrowType(Qt.ArrowType.RightArrow)

    def _on_chevron(self, expanded: bool) -> None:
        self._sync_chevron()
        if self._entry is not None:
            self.expand_toggled.emit(self.video_id, expanded)

    def _emit(self, signal: SignalInstance) -> None:
        if self._entry is not None:
            signal.emit(self.video_id)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._entry is not None:
            self.activated.emit(self.video_id)
        super().mousePressEvent(event)

    def set_entry(
        self, entry: VideoLibraryEntry, transect_name: Callable[[Any], str | None]
    ) -> None:
        """Describe one clip. ``transect_name`` resolves a pass's transect id."""
        self._entry = entry
        video = entry.video
        self._set_link(entry)
        self._name.setText(
            self._name.fontMetrics().elidedText(
                video.file_name, Qt.TextElideMode.ElideMiddle, self._name.width()
            )
        )
        self._recorded.setText(capture_label(video))
        self._length.setText(length_label(video.duration_s))
        self._size.setText(size_label(video.size_bytes))
        self._set_gravity(video)
        self.strip.set_spans(
            timeline_spans(entry),
            video.duration_s,
            {
                str(pass_.id): transect_name(pass_.transect_id) or UNASSIGNED_NAME
                for pass_ in entry.passes
            },
        )
        # Playing and cutting both decode the file, so they need the file, and
        # an unknown link state is not yet a yes. Revealing stays live: the
        # folder is where you go to find out what became of a missing clip.
        self.play_btn.setEnabled(entry.link_state == LINK_LINKED)
        self.new_section_btn.setEnabled(entry.link_state == LINK_LINKED)
        self.set_expanded(self.expanded)
        self.setToolTip(self._row_tooltip(entry))

    def _row_tooltip(self, entry: VideoLibraryEntry) -> str:
        lines = [entry.video.path]
        if entry.link_state == LINK_MISSING:
            lines.append(MISSING_FILE_NOTE)
        if capture_label(entry.video).startswith("~"):
            lines.append(ESTIMATED_DATE_NOTE)
        return "\n".join(lines)

    def _set_link(self, entry: VideoLibraryEntry) -> None:
        """The link icon, and nothing at all while the state is unknown."""
        if entry.link_state == LINK_LINKED:
            self._link.setPixmap(link_icon().pixmap(ICON_SM))
        elif entry.link_state == LINK_MISSING:
            self._link.setPixmap(broken_link_icon().pixmap(ICON_SM))
        else:
            self._link.clear()

    def _set_gravity(self, video: VideoAsset) -> None:
        """Whether the camera recorded a gravity vector, and silence when unread.

        Same honesty rule the link icon follows: an unread clip gets no mark,
        because "no gravity" is a fact about the footage and this is not it.
        """
        if video.gravity == YES:
            self._gravity_dot.setPixmap(_dot(SUCCESS))
            self._gravity_text.setText("Gravity")
            self._gravity.setToolTip("The camera recorded a gravity vector.")
        elif video.gravity == NO:
            self._gravity_dot.setPixmap(_dot(TEXT_MUTED))
            self._gravity_text.setText("None")
            self._gravity.setToolTip("No gravity vector in this clip's telemetry.")
        else:
            self._gravity_dot.clear()
            self._gravity_text.setText("")
            self._gravity.setToolTip(GRAVITY_UNKNOWN_TOOLTIP)

    @property
    def gravity_text(self) -> str:
        return self._gravity_text.text()

    @property
    def gravity_tooltip(self) -> str:
        return self._gravity.toolTip()


def _dot(colour: str) -> QPixmap:
    return status_dot_icon(colour).pixmap(ICON_SM)


class SectionRow(QWidget):
    """One section of a clip: its window, where it is filed, and what came of it.

    Indented under the clip it was cut from, because a section only means
    anything as part of that clip.
    """

    activated = Signal(str)
    add_to_cart_requested = Signal(str)
    retrim_requested = Signal(str)
    reassign_requested = Signal(str)
    delete_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        _selectable(self, "sectionRow")
        self.setFixedHeight(ROW_HEIGHT)

        row = QHBoxLayout(self)
        row.setContentsMargins(SECTION_INDENT, 0, SPACE_SM, 0)
        row.setSpacing(SPACE_SM)

        self._dot_label = QLabel()
        self._dot_label.setFixedWidth(ICON_SM)
        row.addWidget(self._dot_label)

        # The window leads: it is the section's identity, and the only thing
        # that tells two sections of one clip apart before they are filed.
        self._window = secondary_label()
        _fixed_width(self._window, WINDOW_CHARS)
        row.addWidget(self._window)

        self._transect = muted_label()
        _fixed_width(self._transect, TRANSECT_CHARS)
        row.addWidget(self._transect)

        self._direction = muted_label()
        _fixed_width(self._direction, DIRECTION_CHARS)
        row.addWidget(self._direction)

        self._runs = muted_label()
        _fixed_width(self._runs, RUNS_CHARS)
        row.addWidget(self._runs)

        self._cart = muted_label(IN_CART_NOTE)
        self._cart.setToolTip(IN_CART_TOOLTIP)
        self._cart.setVisible(False)
        row.addWidget(self._cart)

        row.addStretch(1)

        self.menu_btn = _quiet_button(
            MENU_GLYPH, "Section actions", "What can be done with this section."
        )
        self.menu_btn.clicked.connect(self._on_menu_button)
        row.addWidget(self.menu_btn)

        self._pass: TransectPass | None = None
        self._run_count = 0
        self._in_cart = False

    @property
    def pass_id(self) -> str:
        return "" if self._pass is None else str(self._pass.id)

    @property
    def section(self) -> TransectPass | None:
        return self._pass

    def set_section(
        self,
        pass_: TransectPass,
        *,
        transect_name: str | None,
        status: str,
        run_count: int = 0,
        in_cart: bool = False,
    ) -> None:
        """Describe one section. ``status`` comes from ``section_facts``."""
        self._pass = pass_
        self._run_count = run_count
        self._in_cart = in_cart
        self._dot_label.setPixmap(_dot(STATUS_COLORS.get(status, TEXT_MUTED)))
        self._window.setText(window_label(pass_))
        self._transect.setText(transect_name or UNASSIGNED_NAME)
        self._transect.setToolTip(transect_name or "")
        self._direction.setText(pass_.direction.capitalize())
        self._runs.setText(run_label(run_count))
        self._cart.setVisible(in_cart)
        self.setToolTip(
            f"{window_label(pass_)}  ·  {transect_name or UNASSIGNED_NAME}"
            f"  ·  {statuses.status_label(status)}"
        )

    def menu(self) -> QMenu:
        """The section's actions, for both the ⋯ button and the right click.

        Built fresh each time rather than kept: what a section allows depends on
        runs and on the cart, both of which move while the row sits there.
        """
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        cart = menu.addAction(MENU_ADD_TO_CART)
        cart.setEnabled(not self._in_cart)
        if self._in_cart:
            cart.setToolTip(IN_CART_TOOLTIP)
        cart.triggered.connect(lambda *_: self.add_to_cart_requested.emit(self.pass_id))
        menu.addAction(MENU_RETRIM).triggered.connect(
            lambda *_: self.retrim_requested.emit(self.pass_id)
        )
        menu.addAction(MENU_REASSIGN).triggered.connect(
            lambda *_: self.reassign_requested.emit(self.pass_id)
        )
        menu.addSeparator()
        delete = menu.addAction(MENU_DELETE)
        # A section with runs stands for something that happened. Letting it
        # vanish would leave those runs describing a swim nothing records.
        delete.setEnabled(self._run_count == 0)
        if self._run_count:
            delete.setToolTip(DELETE_BLOCKED_TOOLTIP)
        delete.triggered.connect(lambda *_: self.delete_requested.emit(self.pass_id))
        return menu

    def _on_menu_button(self) -> None:
        if self._pass is None:
            return
        corner = self.menu_btn.mapToGlobal(self.menu_btn.rect().bottomLeft())
        self.menu().exec(corner)

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:  # noqa: N802
        if self._pass is not None:
            self.menu().exec(event.globalPos())

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._pass is not None:
            self.activated.emit(self.pass_id)
        super().mousePressEvent(event)


# The direction arrows the legend's hand-rolled sort headers already show.
SORT_ASC_GLYPH, SORT_DESC_GLYPH = "▲", "▼"


class _HeaderCell(QLabel):
    """One column heading. A sortable one takes a click and shows the hand."""

    clicked = Signal()

    def __init__(self, title: str, *, sortable: bool, parent: QWidget | None = None) -> None:
        super().__init__(title, parent)
        self._sortable = sortable
        if sortable:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setProperty("sortable", "true")

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._sortable:
            self.clicked.emit()
        super().mousePressEvent(event)


class VideoListHeader(QWidget):
    """The clip columns over the list, sorting whichever one is clicked.

    Not a QHeaderView: the rows under it are widgets in a scroll area rather
    than cells of a view, so this restates ``QHeaderView::section`` from
    ``core/theme.py`` out of the same tokens and must stay in step with it.
    Its cells hold the widths the rows do, which is what lines a heading up
    over its column. Sections keep their own grid and are not sorted from
    here: the columns describe clips only.
    """

    sort_changed = Signal(str, bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("videoListHeader")
        # A bare QWidget ignores stylesheet backgrounds without this.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            f"QWidget#videoListHeader {{ background-color: {CARD_BG};"
            f" border-bottom: 1px solid {BORDER}; }}"
            f" QWidget#videoListHeader QLabel {{ color: {TEXT_MUTED}; font-weight: 600; }}"
            f' QWidget#videoListHeader QLabel[sortable="true"]:hover'
            f" {{ color: {WINDOW_TEXT}; }}"
        )

        row = QHBoxLayout(self)
        row.setContentsMargins(SPACE_SM, HEADER_PAD_V, SPACE_SM, HEADER_PAD_V)
        row.setSpacing(SPACE_SM)

        # Two blank spacers holding the disclosure and link columns, so Name
        # starts exactly where a row's file name does.
        for width in (DISCLOSURE_WIDTH, ICON_SM):
            spacer = QLabel()
            spacer.setFixedWidth(width)
            row.addWidget(spacer)

        self._cells: dict[str, _HeaderCell] = {}
        self._titles: dict[str, str] = {}
        self._add_cell(row, "Name", SORT_NAME, NAME_CHARS)
        self._add_cell(row, "Recorded", SORT_RECORDED, RECORDED_CHARS)
        self._add_cell(row, "Length", SORT_LENGTH, LENGTH_CHARS, right=True)
        self._add_cell(row, "Size", SORT_SIZE, SIZE_CHARS, right=True)

        gravity = _HeaderCell("Gravity", sortable=False)
        # The row's gravity block is a dot, a gap, then its text.
        gravity.setFixedWidth(
            ICON_SM + SPACE_XS + gravity.fontMetrics().averageCharWidth() * GRAVITY_CHARS
        )
        row.addWidget(gravity)
        row.addWidget(_HeaderCell("Sections", sortable=False), 1)

        self._column = DEFAULT_SORT_COLUMN
        self._descending = DEFAULT_SORT_DESCENDING
        self._relabel()

    def _add_cell(
        self, row: QHBoxLayout, title: str, column: str, chars: int, *, right: bool = False
    ) -> None:
        cell = _HeaderCell(title, sortable=True)
        _fixed_width(cell, chars)
        if right:
            cell.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        cell.setToolTip(f"Sort by {title.lower()} (click again to reverse)")
        cell.clicked.connect(lambda column=column: self.sort_by(column))
        row.addWidget(cell)
        self._cells[column] = cell
        self._titles[column] = title

    @property
    def column(self) -> str:
        return self._column

    @property
    def descending(self) -> bool:
        return self._descending

    def cell(self, column: str) -> QLabel:
        return self._cells[column]

    def sort_by(self, column: str) -> None:
        """What a click does: a new column sorts ascending, the current one reverses."""
        descending = not self._descending if column == self._column else False
        self.set_sort(column, descending)
        self.sort_changed.emit(column, descending)

    def set_sort(self, column: str, descending: bool) -> None:
        """Show a sort chosen elsewhere, without reporting it back as a click."""
        self._column = column
        self._descending = descending
        self._relabel()

    def _relabel(self) -> None:
        arrow = SORT_DESC_GLYPH if self._descending else SORT_ASC_GLYPH
        for column, cell in self._cells.items():
            title = self._titles[column]
            cell.setText(f"{title} {arrow}" if column == self._column else title)


# What a refresh compares to decide whether the list has to be rebuilt: each
# group, its clips, whether each clip is open, and the sections under it.
_GroupShape = tuple[str, tuple[tuple[str, bool, tuple[str, ...]], ...]]


class VideoLibraryList(QScrollArea):
    """Every imported clip, under the date it was shot, over the sections cut from it."""

    play_requested = Signal(str)
    reveal_requested = Signal(str)
    new_section_requested = Signal(str)
    activated = Signal(str)
    span_clicked = Signal(str)
    section_activated = Signal(str)
    section_add_to_cart = Signal(str)
    section_retrim = Signal(str)
    section_reassign = Signal(str)
    section_delete = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.Shape.NoFrame)
        body = QWidget()
        self._body_layout = QVBoxLayout(body)
        self._body_layout.setContentsMargins(0, 0, 0, 0)
        self._body_layout.setSpacing(SPACE_XS)
        self.setWidget(body)
        self._body = body
        self._shape: list[_GroupShape] = []
        self._groups: list[DateGroup] = []
        self._rows: dict[str, VideoRow] = {}
        self._sections: dict[str, SectionRow] = {}
        self._sections_by_video: dict[str, list[SectionRow]] = {}
        self._expanded: set[str] = set()
        self._selected: str | None = None
        self._selected_section: str | None = None
        # Outlives every rebuild, so the one line telling the user the list takes
        # a drop is not something a refresh can take away.
        self.drop_hint = muted_label(DROP_HINT)
        self._body_layout.addStretch(1)
        self._body_layout.addWidget(self.drop_hint)

    @property
    def selected(self) -> str | None:
        return self._selected

    @property
    def selected_section(self) -> str | None:
        return self._selected_section

    def rows(self) -> dict[str, VideoRow]:
        return dict(self._rows)

    def sections(self) -> dict[str, SectionRow]:
        return dict(self._sections)

    def set_groups(
        self,
        groups: Sequence[DateGroup],
        transect_name: Callable[[Any], str | None] = lambda _id: None,
        *,
        in_cart: Callable[[str], bool] = lambda _pass_id: False,
    ) -> None:
        """Fill the list. Rebuilt only when its shape changes.

        The page refreshes this on every scan, and a rebuild under the cursor
        loses the row being clicked and scrolls the list back to the top. The
        shape carries the sections and which clips are open as well as the
        clips, or a section cut a moment ago would never get a row.
        """
        self._groups = list(groups)
        shape = self._shape_of(self._groups)
        if shape != self._shape:
            self._rebuild(self._groups)
            self._shape = shape
        for group in self._groups:
            for entry in group.entries:
                video_id = str(entry.video.id)
                row = self._rows[video_id]
                row.set_entry(entry, transect_name)
                row.set_expanded(video_id in self._expanded)
                for pass_, status, run_count in section_facts(entry):
                    self._sections[str(pass_.id)].set_section(
                        pass_,
                        transect_name=transect_name(pass_.transect_id),
                        status=status,
                        run_count=run_count,
                        in_cart=bool(in_cart(str(pass_.id))),
                    )
        self._apply_expansion()
        self._paint_selection()

    def expand(self, video_id: str) -> None:
        """Open a clip from outside, as clicking its chevron would."""
        row = self._rows.get(video_id)
        if row is not None:
            row.set_expanded(True)
        self._set_expanded(video_id, True)

    def set_selected(self, video_id: str | None) -> None:
        self._selected = video_id
        # One thing at a time: a clip and a section describe different levels,
        # and two highlights would leave the detail pane's subject ambiguous.
        self._selected_section = None
        self._paint_selection()

    def set_selected_section(self, pass_id: str | None) -> None:
        self._selected_section = pass_id
        self._selected = None
        self._paint_selection()

    def _shape_of(self, groups: Sequence[DateGroup]) -> list[_GroupShape]:
        return [
            (
                group.key,
                tuple(
                    (
                        str(entry.video.id),
                        str(entry.video.id) in self._expanded,
                        tuple(str(pass_.id) for pass_, _, _ in section_facts(entry)),
                    )
                    for entry in group.entries
                ),
            )
            for group in groups
        ]

    def _rebuild(self, groups: Iterable[DateGroup]) -> None:
        while self._body_layout.count():
            item = self._body_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None and widget is not self.drop_hint:
                widget.deleteLater()
        self._rows = {}
        self._sections = {}
        self._sections_by_video = {}
        for group in groups:
            self._body_layout.addWidget(SectionHeader(group.title))
            for entry in group.entries:
                self._add_clip(entry)
        self._body_layout.addStretch(1)
        self._body_layout.addWidget(self.drop_hint)

    def _add_clip(self, entry: VideoLibraryEntry) -> None:
        video_id = str(entry.video.id)
        row = VideoRow()
        row.play_requested.connect(self.play_requested)
        row.reveal_requested.connect(self.reveal_requested)
        row.new_section_requested.connect(self.new_section_requested)
        row.span_clicked.connect(self.span_clicked)
        row.activated.connect(self._on_activated)
        row.expand_toggled.connect(self._set_expanded)
        self._body_layout.addWidget(row)
        self._rows[video_id] = row

        sections = []
        for pass_, _status, _run_count in section_facts(entry):
            section = SectionRow()
            section.activated.connect(self._on_section_activated)
            section.add_to_cart_requested.connect(self.section_add_to_cart)
            section.retrim_requested.connect(self.section_retrim)
            section.reassign_requested.connect(self.section_reassign)
            section.delete_requested.connect(self.section_delete)
            self._body_layout.addWidget(section)
            self._sections[str(pass_.id)] = section
            sections.append(section)
        self._sections_by_video[video_id] = sections

    def _set_expanded(self, video_id: str, expanded: bool) -> None:
        if expanded:
            self._expanded.add(video_id)
        else:
            self._expanded.discard(video_id)
        self._apply_expansion()
        # The shape carries which clips are open, so it is recorded here too.
        # Left alone, the next scan would read the list as changed and rebuild
        # it under the cursor.
        self._shape = self._shape_of(self._groups)

    def _apply_expansion(self) -> None:
        for video_id, sections in self._sections_by_video.items():
            visible = video_id in self._expanded
            for section in sections:
                section.setVisible(visible)

    def _on_activated(self, video_id: str) -> None:
        self.set_selected(video_id)
        self.activated.emit(video_id)

    def _on_section_activated(self, pass_id: str) -> None:
        self.set_selected_section(pass_id)
        self.section_activated.emit(pass_id)

    def _paint_selection(self) -> None:
        for video_id, row in self._rows.items():
            _set_selected(row, video_id == self._selected)
        for pass_id, section in self._sections.items():
            _set_selected(section, pass_id == self._selected_section)
