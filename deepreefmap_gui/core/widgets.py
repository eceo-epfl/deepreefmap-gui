"""Small shared building blocks: section cards, empty states, tables, dialogs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.core.theme import (
    BAR_HEIGHT,
    BORDER,
    BORDER_STRONG,
    BUTTON,
    CARD_BG,
    CONTROL_HEIGHT,
    ERROR,
    FONT_SM,
    GROOVE,
    IDLE,
    ON_ACCENT,
    PRIMARY,
    RADIUS,
    RADIUS_SM,
    SPACE_MD,
    SPACE_SM,
    SPACE_XL,
    SPACE_XS,
    SUCCESS,
    SURFACE_HI,
    TABLE_ROW_HEIGHT,
    TEXT_DIM,
    TEXT_MUTED,
    WARN_BG,
    WARN_BORDER,
    WARN_TEXT,
    WARNING,
    WEIGHT_SEMIBOLD,
    WINDOW_TEXT,
    bar_qss,
)
from deepreefmap_gui.survey import statuses


def section_title_font(label: QLabel) -> None:
    """The one weight a section title is set in."""
    font = label.font()
    font.setWeight(QFont.Weight.DemiBold)
    label.setFont(font)
    label.setStyleSheet(f"color: {TEXT_MUTED};")


def tone_label(text: str = "", tone: str = "muted", parent: QWidget | None = None) -> QLabel:
    """A label carrying one of the app's text roles rather than a colour.

    The tone is a QSS property (see the ``QLabel[tone=...]`` rules in theme.py),
    so the colour lives with the palette. Prefer this to
    ``setStyleSheet(f"color: {...}")``, which opts the label out of the theme.
    """
    label = QLabel(text, parent)
    label.setProperty("tone", tone)
    return label


def muted_label(text: str = "", parent: QWidget | None = None) -> QLabel:
    """A caption or field label beside the thing it describes."""
    return tone_label(text, "muted", parent)


def secondary_label(text: str = "", parent: QWidget | None = None) -> QLabel:
    """A readout: a shade brighter than a caption, dimmer than body text."""
    return tone_label(text, "secondary", parent)


class SectionHeader(QLabel):
    """A section title, at the app's one title weight and colour."""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        section_title_font(self)


def section_card(title: str = "", *, spacing: int = SPACE_SM) -> tuple[QWidget, QVBoxLayout]:
    """A titled panel that sits above the shell, and the layout to fill it.

    Returns the card and its content layout, so callers add their widgets to the
    layout without needing to know how the title is built.
    """
    card = QWidget()
    card.setObjectName("sectionCard")
    # Object-name scoped so the fill lands on the card only: an unscoped rule
    # would cascade into every child widget's background too. Views inside a
    # card drop their own border, which would otherwise double the card's.
    card.setStyleSheet(
        f"QWidget#sectionCard {{ background-color: {CARD_BG};"
        f" border: 1px solid {BORDER}; border-radius: {RADIUS}px; }}"
        " QWidget#sectionCard QAbstractItemView { border: none; }"
    )
    outer = QVBoxLayout(card)
    outer.setContentsMargins(SPACE_MD, SPACE_SM, SPACE_MD, SPACE_MD)
    outer.setSpacing(spacing)
    if title:
        outer.addWidget(SectionHeader(title))
    return card, outer


def section_column(title: str = "", *, spacing: int = SPACE_SM) -> tuple[QWidget, QVBoxLayout]:
    """A titled pane that *is* the page, rather than a card sitting on one.

    The same shape as `section_card` without the fill, the border and the
    margins. An item view already draws itself as a recessed BASE panel with its
    own hairline, so wrapping one in a card draws the frame twice and spends
    24px of margin restating it -- which is why `section_card` has to suppress
    the inner border to look right at all.

    Use this for the pane holding a page's primary content, and `section_card`
    for a pane describing one selected thing. A page whose every pane is a card
    has no figure and no ground: the raised fill stops reading as "this is
    raised" and just makes the window lighter.
    """
    column = QWidget()
    layout = QVBoxLayout(column)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(spacing)
    if title:
        layout.addWidget(SectionHeader(title))
    return column, layout


def lent_panel_home(parent: QWidget) -> tuple[QWidget, QWidget, QVBoxLayout]:
    """A permanent holder for a panel that lives somewhere else, and the panel.

    Two widgets rather than one because the page is lent to a destination and
    handed back; the home is the empty holder it returns to. Parented and hidden,
    because a parentless widget made visible maps itself as a top-level window.
    """
    # No layout-level AlignTop: it shrinks the layout to its size hint and wraps
    # word-wrapped labels narrow. Panels that need top alignment end with a
    # stretch of their own.
    home = QWidget(parent)
    home.setVisible(False)
    home_layout = QVBoxLayout(home)
    home_layout.setContentsMargins(0, 0, 0, 0)
    page = QWidget()
    page_layout = QVBoxLayout(page)
    page_layout.setContentsMargins(0, 0, 0, 0)
    home_layout.addWidget(page)
    return home, page, page_layout


def segmented_qss(*, first: bool, last: bool) -> str:
    """One button of a joined segmented control, filled when it is the live one.

    The segments share a seam: only the outermost corners round, and every
    segment after the first drops its left border so the row reads as one
    control rather than as several loose pills.
    """
    corners = []
    if first:
        corners.append(f"border-top-left-radius: {RADIUS}px;")
        corners.append(f"border-bottom-left-radius: {RADIUS}px;")
    else:
        corners.append("border-left: none;")
    if last:
        corners.append(f"border-top-right-radius: {RADIUS}px;")
        corners.append(f"border-bottom-right-radius: {RADIUS}px;")
    return (
        f"QToolButton {{ border: 1px solid {BORDER}; border-radius: 0; {' '.join(corners)}"
        f" padding: {SPACE_XS}px {SPACE_MD}px; min-height: {CONTROL_HEIGHT - 2 * SPACE_XS}px;"
        f" background: {BUTTON}; color: {WINDOW_TEXT}; }}"
        f" QToolButton:hover {{ background: {SURFACE_HI}; border-color: {BORDER_STRONG}; }}"
        f" QToolButton:focus {{ border-color: {PRIMARY}; }}"
        f" QToolButton:checked {{ background: {PRIMARY}; color: {ON_ACCENT};"
        f" font-weight: {WEIGHT_SEMIBOLD}; }}"
        f" QToolButton:disabled {{ color: {TEXT_DIM}; background: transparent; }}"
    )


class KeyValueList(QWidget):
    """Facts about one thing, as aligned label/value rows.

    A grid rather than one rich-text paragraph with bold run-in labels: the
    values line up, so a run's numbers can be compared down the column instead of
    hunted for inside a sentence.
    """

    def __init__(self, parent: QWidget | None = None, *, wrap: bool = True) -> None:
        super().__init__(parent)
        self._wrap = wrap
        self._keys: list[str] = []
        self._values: list[QLabel] = []
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(SPACE_MD)
        self._grid.setVerticalSpacing(SPACE_XS)
        self._grid.setColumnStretch(1, 1)

    def set_rows(self, rows: list[tuple[str, str]]) -> None:
        """Fill the list. Rebuilt only when the *keys* change.

        A caller showing a fixed set of fields calls this once per selection with
        the same keys every time, and rewriting the values in place means the
        layout never reflows. Rebuilding unconditionally is what let a pane grow
        and shrink under the cursor as the table was arrowed through.
        """
        if [key for key, _ in rows] == self._keys:
            for (_, value), label in zip(rows, self._values, strict=True):
                self._fill(label, value)
            return

        while self._grid.count():
            item = self._grid.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()
        self._keys = [key for key, _ in rows]
        self._values = []
        for row, (key, value) in enumerate(rows):
            name = QLabel(key)
            name.setStyleSheet(f"color: {TEXT_MUTED}; font-size: {FONT_SM};")
            name.setAlignment(
                Qt.AlignmentFlag.AlignRight
                | (Qt.AlignmentFlag.AlignTop if self._wrap else Qt.AlignmentFlag.AlignVCenter)
            )
            self._grid.addWidget(name, row, 0)
            shown = secondary_label()
            shown.setWordWrap(self._wrap)
            if not self._wrap:
                # Ignored, not Preferred: a long value's own width hint would
                # otherwise widen the whole pane to fit it and shove the table
                # aside, which is the same reason the ortho strip is Ignored.
                shown.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            shown.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
            self._fill(shown, value)
            self._grid.addWidget(shown, row, 1)
            self._values.append(shown)

    def _fill(self, label: QLabel, value: str) -> None:
        label.setText(value)
        # The tooltip carries what a one-line row cannot show in full.
        label.setToolTip("" if self._wrap else value)

    def clear(self) -> None:
        self.set_rows([])


# The map viewport as a filter. Shared so the Plan list and the Browse run list
# offer the same two words for the same idea.
SCOPE_FILTERS = (
    ("in_view", "In view"),
    ("all", "All transects"),
)


def utility_button_qss(right_padding: int = SPACE_SM) -> str:
    """A header utility control: bordered and quiet rather than filled.

    Distinct on purpose from the workspace pills beside it. Those say where you
    are working; a utility is somewhere you visit and leave, or a panel you
    toggle. ``right_padding`` reserves room for anything painted after the
    label, so the text does not shift as it comes and goes.
    """
    return (
        f"QToolButton {{ border: 1px solid {BORDER}; border-radius: {RADIUS_SM}px;"
        f" background: {BUTTON}; color: {WINDOW_TEXT};"
        f" padding: {SPACE_XS}px {right_padding}px {SPACE_XS}px {SPACE_SM}px;"
        f" min-height: {CONTROL_HEIGHT}px; }}"
        f" QToolButton:hover {{ background: {SURFACE_HI}; border-color: {BORDER_STRONG}; }}"
        f" QToolButton:focus {{ border-color: {PRIMARY}; }}"
        f" QToolButton:checked {{ background: {PRIMARY}; color: {ON_ACCENT};"
        f" font-weight: {WEIGHT_SEMIBOLD}; border-color: transparent; }}"
    )


# How strongly a chip tints its own background, out of 255. Low because the text
# on it is the same colour as the fill; test_design_system.py holds the floor.
PILL_TINT_ALPHA = 36
PILL_PROGRESS_ALPHA = 96
# The selected filter chip's outline.
PILL_BORDER_ALPHA = 110


def tinted(colour: str, alpha: int) -> str:
    """``colour`` as an rgba() string, so a stylesheet keeps the theme token it
    came from instead of carrying a second, transparent hex."""
    c = QColor(colour)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {alpha})"


# Shorter than CONTROL_HEIGHT: a chip is a label you may be able to press, not a
# control in a form, and at the full height a row of them reads as buttons.
CHIP_HEIGHT = CONTROL_HEIGHT - 2 * SPACE_XS


def chip_qss(colour: str, *, interactive: bool) -> str:
    """One chip: an outcome, or the filter that selects it.

    A pill tinted from its own ink, never filled solid: a solid fill is what a
    pressed button looks like, and both sit on the same screens.
    """
    shape = (
        f" border-radius: {CHIP_HEIGHT // 2}px; padding: 0px {SPACE_MD}px;"
        f" min-height: {CHIP_HEIGHT}px;"
    )
    lit = (
        f" background: {tinted(colour, PILL_TINT_ALPHA)}; color: {colour};"
        f" font-weight: {WEIGHT_SEMIBOLD};"
    )
    if not interactive:
        return f"QLabel {{ border: 1px solid transparent;{shape}{lit} }}"
    # Only the chip in force carries its outcome's colour.
    return (
        f"QToolButton {{ border: 1px solid {BORDER};{shape}"
        f" background: transparent; color: {TEXT_MUTED}; }}"
        f" QToolButton:hover {{ color: {WINDOW_TEXT}; border-color: {BORDER_STRONG}; }}"
        f" QToolButton:focus {{ border-color: {PRIMARY}; color: {WINDOW_TEXT}; }}"
        f" QToolButton:checked {{ border-color: {tinted(colour, PILL_BORDER_ALPHA)};{lit} }}"
    )


class StatusChip(QLabel):
    """An outcome, in the shape and colour the tables paint it."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.set_status("", TEXT_MUTED)

    def set_status(self, text: str, colour: str = TEXT_MUTED) -> None:
        self.setText(text)
        self.setStyleSheet(chip_qss(colour, interactive=False))
        self.setVisible(bool(text))


class FilterChips(QWidget):
    """A row of exclusive filters, each carrying its own count.

    The count is the point: a chip reading "Failed 3" answers the question
    before it is clicked, and a chip reading "Failed 0" says not to bother. Empty
    chips stay visible rather than disappearing, so the row does not reflow under
    the cursor as a batch runs.
    """

    changed = Signal(str)

    def __init__(
        self,
        options: Sequence[tuple[str, ...]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPACE_XS)
        self._labels = {key: title for key, title, *_ in options}
        self._buttons: dict[str, QToolButton] = {}
        group = QButtonGroup(self)
        group.setExclusive(True)
        for key, title, *rest in options:
            button = QToolButton()
            button.setText(title)
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            # A filter selecting an outcome takes that outcome's colour once it
            # is in force. Filters that select anything else take the accent.
            button.setStyleSheet(chip_qss(rest[0] if rest else PRIMARY, interactive=True))
            button.toggled.connect(
                lambda checked, k=key: self.changed.emit(k) if checked else None
            )
            group.addButton(button)
            layout.addWidget(button)
            self._buttons[key] = button
        first = options[0][0]
        self._buttons[first].setChecked(True)

    def set_counts(self, counts: dict[str, int]) -> None:
        for key, button in self._buttons.items():
            count = counts.get(key)
            button.setText(
                self._labels[key] if count is None else f"{self._labels[key]}  {count}"
            )

    def current(self) -> str:
        return next(k for k, b in self._buttons.items() if b.isChecked())

    def set_current(self, key: str) -> None:
        button = self._buttons.get(key)
        if button is not None and not button.isChecked():
            button.setChecked(True)


class EmptyState(QWidget):
    """What an empty pane says: what is missing, and what fills it."""

    def __init__(self, message: str, hint: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(SPACE_XL, SPACE_XL, SPACE_XL, SPACE_XL)
        layout.setSpacing(SPACE_XS)
        layout.addStretch(1)

        self._message = QLabel(message)
        self._message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._message.setWordWrap(True)
        self._message.setStyleSheet(f"color: {TEXT_MUTED};")
        layout.addWidget(self._message)

        self._hint = QLabel(hint)
        self._hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._hint.setWordWrap(True)
        self._hint.setStyleSheet(f"color: {TEXT_DIM};")
        # Parented before shown: setVisible on a parentless widget maps it as a
        # top-level window, which flashes an empty titlebar box on screen.
        layout.addWidget(self._hint)
        self._hint.setVisible(bool(hint))

        layout.addStretch(1)

    def set_text(self, message: str, hint: str = "") -> None:
        self._message.setText(message)
        self._hint.setText(hint)
        self._hint.setVisible(bool(hint))


def warning_banner_qss() -> str:
    """The app's one warning surface, for a label that is its own banner.

    Three panels carried a character-for-character copy of this, each spelling
    its own 6px padding and 3px radius -- a radius the theme does not have.
    """
    return (
        f"background-color: {WARN_BG}; color: {WARN_TEXT};"
        f" border: 1px solid {WARN_BORDER};"
        f" padding: {SPACE_SM}px; border-radius: {RADIUS_SM}px;"
    )


class NotReadyStrip(QWidget):
    """The one thing blocking a page, next to the button that goes and fixes it.

    Directions the page cannot follow ("switch modes, then find the tab") are
    not an action, so the destination is a button. Hidden whenever nothing
    blocks, which is why it sits above the content rather than inside it.
    """

    action_clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("notReadyStrip")
        self.setStyleSheet(
            f"QWidget#notReadyStrip {{ background-color: {WARN_BG};"
            f" border: 1px solid {WARN_BORDER}; border-radius: {RADIUS_SM}px; }}"
        )
        row = QHBoxLayout(self)
        row.setContentsMargins(SPACE_MD, SPACE_SM, SPACE_MD, SPACE_SM)
        row.setSpacing(SPACE_MD)

        self._reason = QLabel("")
        self._reason.setWordWrap(True)
        self._reason.setStyleSheet(f"color: {WARN_TEXT};")
        row.addWidget(self._reason, 1)

        self._action = QPushButton("")
        self._action.clicked.connect(self.action_clicked)
        row.addWidget(self._action)

        self.setVisible(False)

    def show_blocker(self, reason: str, action: str = "") -> None:
        """Name the blocker. An empty action means it is fixed on this page."""
        self._reason.setText(reason)
        self._action.setText(action)
        self._action.setVisible(bool(action))
        self.setVisible(bool(reason))

    def clear(self) -> None:
        self._reason.setText("")
        self.setVisible(False)


class HeaderAlert(QWidget):
    """The one destination that wants attention, named in the header.

    Hidden whenever nothing is wrong. ``NotReadyStrip`` is the page-level
    equivalent: it names what stops one page working, this says which page.
    """

    clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("headerAlert")
        # A bare QWidget takes its background from the palette and ignores the
        # stylesheet's, which leaves the box invisible.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        row = QHBoxLayout(self)
        row.setContentsMargins(SPACE_SM, SPACE_XS, SPACE_SM, SPACE_XS)
        row.setSpacing(SPACE_SM)

        self._glyph = QLabel()
        row.addWidget(self._glyph)
        self._text = QLabel("")
        row.addWidget(self._text)

        # One tint for both states it can carry; severity is the glyph's job.
        self.setStyleSheet(
            f"QWidget#headerAlert {{ background-color: {WARN_BG};"
            f" border: 1px solid {WARN_BORDER}; border-radius: {RADIUS_SM}px; }}"
            f" QLabel {{ color: {WARN_TEXT}; background: transparent; }}"
        )
        self.setVisible(False)

    def show_alert(self, text: str, tooltip: str = "", pixmap: QPixmap | None = None) -> None:
        self._text.setText(text)
        self.setToolTip(tooltip or text)
        self._glyph.setVisible(pixmap is not None)
        self._glyph.setPixmap(pixmap if pixmap is not None else QPixmap())
        self.setVisible(bool(text))

    def clear(self) -> None:
        self._text.setText("")
        self.setVisible(False)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


def ok_cancel_row(dialog: QDialog) -> QDialogButtonBox:
    """The Ok/Cancel pair at the foot of a dialog, wired to accept and reject.

    Three dialogs carried a character-for-character copy of this. The box is
    returned rather than added to a layout, because the caller places it and one
    caller keeps the Ok button to gate it on a selection.
    """
    buttons = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
    )
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    return buttons


def confirm(parent: QWidget | None, title: str, text: str) -> bool:
    """Ask a yes/no question, with No as the button Enter picks.

    Every prompt in the app guards something the answer cannot take back: a
    delete, a trim written over every other pass of a transect, a batch started
    on a disk that will not hold it. Most sites spelled that default out by
    hand; the delete prompt took Qt's, which puts the focus ring on Yes.
    """
    answer = QMessageBox.question(
        parent,
        title,
        text,
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    return answer == QMessageBox.StandardButton.Yes


def configure_table(
    table: QTableWidget, headers: Sequence[str], *, alternating: bool = True
) -> None:
    """The defaults every table in the app reads under: whole rows, no edit
    triggers, the theme's alternate fill.

    ``alternating`` is for the one table whose cells are widgets: they paint
    their own background, so the stripe would stop halfway across the row.
    """
    table.setHorizontalHeaderLabels(list(headers))
    table.verticalHeader().setVisible(False)
    # Row height lives here, not in the QSS item padding: QTableView sizes its
    # rows off defaultSectionSize and ignores that padding, so the stylesheet
    # alone changed how a row looked without fitting another one on screen.
    table.verticalHeader().setDefaultSectionSize(TABLE_ROW_HEIGHT)
    table.setShowGrid(False)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setAlternatingRowColors(alternating)


def _sorts_before(mine: object, theirs: object, descending: bool) -> bool:
    """The one ordering both sortable item classes share.

    A row with no value sinks to the bottom in *both* directions. Qt sorts with
    ``__lt__`` and reverses for descending, so a plain comparison would float
    the blanks to the top of a descending sort; the caller reads the current
    order off its header and passes it in to cancel that out.
    """
    if (mine is None) != (theirs is None):
        return descending if mine is None else not descending
    if mine is None:
        return False
    try:
        return mine < theirs  # type: ignore[operator]
    except TypeError:
        return str(mine) < str(theirs)


class SortableItem(QTableWidgetItem):
    """A cell that sorts by a value rather than by its formatted text.

    "1.2M pts" above "988k pts", "1.6 GB" above "1015 MB": the display strings
    order wrongly under every string comparison, so the raw number rides along.
    """

    def __init__(self, text: str, value: object = None) -> None:
        super().__init__(text)
        self._value = value

    def __lt__(self, other: QTableWidgetItem) -> bool:
        if not isinstance(other, SortableItem):
            return super().__lt__(other)
        table = self.tableWidget()
        descending = (
            table is not None
            and table.horizontalHeader().sortIndicatorOrder()
            == Qt.SortOrder.DescendingOrder
        )
        return _sorts_before(self._value, other._value, descending)


class SortableTreeItem(QTreeWidgetItem):
    """A row that sorts by per-column values, under SortableItem's contract.

    ``values`` maps column index to sort value. A column with no entry sinks in
    both directions, which is also how a whole valueless row stays pinned to
    the foot of its tree whatever the sort.
    """

    def __init__(
        self,
        tree: QTreeWidget,
        columns: Sequence[str],
        values: Mapping[int, object] | None = None,
    ) -> None:
        super().__init__(tree, list(columns))
        self._values = dict(values or {})

    def __lt__(self, other: QTreeWidgetItem) -> bool:
        if not isinstance(other, SortableTreeItem):
            return super().__lt__(other)
        tree = self.treeWidget()
        column = tree.sortColumn() if tree is not None else 0
        descending = (
            tree is not None
            and tree.header().sortIndicatorOrder() == Qt.SortOrder.DescendingOrder
        )
        return _sorts_before(self._values.get(column), other._values.get(column), descending)


def enable_sorting(
    view: QTableWidget | QTreeWidget,
    column: int | None = 0,
    order: Qt.SortOrder = Qt.SortOrder.AscendingOrder,
) -> None:
    """Turn on click-to-sort, with the indicator shown from the start.

    The ``sortable`` property is what the theme keys the header hover affordance
    on, so only headers that really sort light up under the pointer. Call this
    after the rows are in place: enabling sorting sorts there and then, and
    cells landing in a live sort scatter across half-built rows.

    Pass ``column=None`` to keep the rows in the order the caller built them.
    Headers still sort on click, but nothing sorts until then.
    """
    header = view.header() if isinstance(view, QTreeWidget) else view.horizontalHeader()
    header.setProperty("sortable", "true")
    # Re-polish so the property is read even on a header styled before this ran.
    header.style().unpolish(header)
    header.style().polish(header)
    header.setSortIndicatorShown(True)
    if column is None:
        # Parking the indicator on no section makes the enable below a no-op
        # sort, so a caller-built order (say newest first) survives it.
        header.setSortIndicator(-1, order)
        view.setSortingEnabled(True)
    else:
        view.setSortingEnabled(True)
        view.sortByColumn(column, order)


# The palette for survey/statuses.py's colour roles. That table is Qt-free; this
# is the one place it meets the theme.
TONE_COLORS = {
    statuses.TONE_GOOD: SUCCESS,
    statuses.TONE_BAD: ERROR,
    statuses.TONE_BUSY: WARNING,
    statuses.TONE_IDLE: IDLE,
    statuses.TONE_QUIET: TEXT_DIM,
}

# Every status the interface can show. Public: the tables, the detail panes and
# the filter chips all read it, so one status has one colour.
STATUS_COLORS = {
    status: TONE_COLORS[statuses.status_tone(status)] for status in statuses.DISPLAY_STATUSES
}


def clip_outcome_color(outcome: str) -> str:
    """The colour for a clip's own state, from the roles the runs use."""
    return TONE_COLORS[statuses.clip_spec(outcome).tone]


# Percent complete of the pass a status cell describes, 0-100. Absent on every
# row but the one being processed, which is what makes the pill a progress bar.
PASS_PERCENT_ROLE = Qt.ItemDataRole.UserRole + 1

class StatusPillDelegate(QStyledItemDelegate):
    """Paints a run status as a tinted pill, readable across the table.

    A delegate rather than a cell widget so the QTableWidgetItem stays the
    source of truth for the status text. The running row's pill doubles as its
    progress bar: PASS_PERCENT_ROLE fills it from the left.
    """

    def paint(self, painter, option: QStyleOptionViewItem, index) -> None:
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        if not text:
            super().paint(painter, option, index)
            return

        # Selection and the alternating row fill still come from the style.
        blank = QStyleOptionViewItem(option)
        self.initStyleOption(blank, index)
        blank.text = ""
        style = option.widget.style() if option.widget else None
        if style is not None:
            style.drawControl(QStyle.ControlElement.CE_ItemViewItem, blank, painter, option.widget)

        # Key off the leading word so a decorated pill ("Succeeded ⚠") keeps its
        # colour; case-insensitive so a title-cased "Failed" pill still reads red.
        color = QColor(STATUS_COLORS.get(text.lower().split()[0], TEXT_MUTED))
        metrics = option.fontMetrics
        pad_x = SPACE_SM
        width = min(metrics.horizontalAdvance(text) + pad_x * 2, option.rect.width() - SPACE_SM)
        height = metrics.height() + 4
        pill = QRectF(
            option.rect.left() + 4,
            option.rect.center().y() - height / 2 + 1,
            max(1, width),
            height,
        )

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # Light enough that the accent text keeps its contrast against the tint:
        # the pill fill is drawn from the same colour as the text on top of it,
        # so every point of alpha here is a point of legibility lost.
        fill = QColor(color)
        fill.setAlpha(PILL_TINT_ALPHA)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        painter.drawRoundedRect(pill, height / 2, height / 2)

        # A running pass fills its own pill rather than growing a second widget in
        # the row: the pill is already the thing the eye goes to for that pass.
        percent = index.data(PASS_PERCENT_ROLE)
        if percent is not None:
            done = QColor(color)
            done.setAlpha(PILL_PROGRESS_ALPHA)
            painter.setBrush(done)
            painter.save()
            clip = QRectF(pill)
            clip.setWidth(pill.width() * max(0.0, min(100.0, float(percent))) / 100.0)
            painter.setClipRect(clip)
            painter.drawRoundedRect(pill, height / 2, height / 2)
            painter.restore()

        painter.setPen(color)
        painter.drawText(
            pill,
            Qt.AlignmentFlag.AlignCenter,
            metrics.elidedText(text, Qt.TextElideMode.ElideRight, int(pill.width()) - pad_x),
        )
        painter.restore()


class MeterBar(QProgressBar):
    """A thin utilisation bar that can also say it has nothing to report.

    Qt draws a range of (0, 0) as a busy animation, so an unknown figure reads
    as activity. Unavailable is painted instead as hazard hatching: static, and
    obviously not a level.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setRange(0, 100)
        self.setTextVisible(False)
        self.setFixedHeight(BAR_HEIGHT)
        self._unavailable = False

    def set_level(self, percent: float, colour: str) -> None:
        self._unavailable = False
        self.setRange(0, 100)
        self.setValue(int(round(max(0.0, min(100.0, percent)))))
        self.setStyleSheet(bar_qss(colour))
        self.update()

    def set_unavailable(self) -> None:
        self._unavailable = True
        self.setRange(0, 100)
        self.setValue(0)
        self.setStyleSheet(bar_qss(GROOVE))
        self.update()

    def paintEvent(self, event) -> None:
        if not self._unavailable:
            super().paintEvent(event)
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        track = QRectF(self.rect())
        radius = track.height() / 2.0
        path = QPainterPath()
        path.addRoundedRect(track, radius, radius)
        painter.fillPath(path, QColor(GROOVE))
        painter.setClipPath(path)
        pen = QPen(QColor(ERROR))
        pen.setWidthF(2.0)
        painter.setPen(pen)
        # Diagonals at a fixed pitch, offset so each starts off the left edge.
        pitch = 7.0
        x = -track.height()
        while x < track.width() + track.height():
            painter.drawLine(QPointF(x, track.bottom()), QPointF(x + track.height(), track.top()))
            x += pitch
        painter.end()
