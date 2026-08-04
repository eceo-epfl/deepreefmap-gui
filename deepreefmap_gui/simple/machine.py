"""This machine: one destination for everything about the computer you are on.

Readiness, models and system used to be three separate places, two of which a
diver could not reach at all. They are three views of one subject, so they get
one destination and a segmented control inside it rather than three entries in
the header competing with the work.

The header button reports the machine before it is opened, in two slots and no
more: what is stopping work, and whether an update is waiting. Its verdict comes
from machine_state() in progress.py, which is the same Qt-free vocabulary the
step badges use and is computed from the same checks the Run gate blocks on, so
the header cannot claim the machine is fine while Process refuses to start.
"""

from __future__ import annotations

import logging
from functools import partial

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPainter
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.core.icons import (
    ICON_SM,
    blocked_icon,
    download_icon,
    machine_icon,
    warning_icon,
)
from deepreefmap_gui.core.theme import (
    BORDER,
    BORDER_STRONG,
    BUTTON,
    CONTROL_HEIGHT,
    GUTTER,
    PRIMARY,
    RADIUS_SM,
    SPACE_SM,
    SPACE_XS,
    SURFACE_HI,
    TEXT_MUTED,
    UPDATE,
    WINDOW_TEXT,
)
from deepreefmap_gui.core.widgets import SectionHeader, section_card, segmented_qss
from deepreefmap_gui.core.window_protocol import MixinBase
from deepreefmap_gui.simple.progress import ATTENTION, BLOCKED, SectionState, machine_state

logger = logging.getLogger(__name__)

# Left to right in the order they matter: what stops a run, what a run needs
# installed, and what the machine is doing while it runs.
MACHINE_VIEWS = ("readiness", "models", "system")

_VIEW_LABELS = {
    "readiness": "Readiness",
    "models": "Models",
    "system": "System",
}

_VIEW_TIPS = {
    "readiness": "Whether this computer can process a dive, and how to fix it if not.",
    "models": "The models installed here, and how to add or remove them.",
    "system": "Live usage, what past runs cost, where results are saved, and updates.",
}

# The header button's badge size, and the gap between two of them.
_BADGE_PX = ICON_SM
_BADGE_GAP = SPACE_XS


def _machine_button_qss(right_padding: int) -> str:
    """A utility control, so it is bordered and quiet rather than filled.

    Distinct on purpose from the workspace pills beside it: those say where you
    are working, this one is somewhere you visit and leave.
    """
    return (
        f"QToolButton {{ border: 1px solid {BORDER}; border-radius: {RADIUS_SM}px;"
        f" background: {BUTTON}; color: {WINDOW_TEXT};"
        f" padding: {SPACE_XS}px {right_padding}px {SPACE_XS}px {SPACE_SM}px;"
        f" min-height: {CONTROL_HEIGHT}px; }}"
        f" QToolButton:hover {{ background: {SURFACE_HI}; border-color: {BORDER_STRONG}; }}"
        f" QToolButton:focus {{ border-color: {PRIMARY}; }}"
    )


class MachineButton(QToolButton):
    """Header entry to This machine, carrying its verdict before it is opened.

    Two badge slots at most. The first is the highest-severity thing standing in
    the way, the second is an available update, and neither appears when there
    is nothing to say: a badge that is always lit is a badge nobody reads.

    An update is never painted red. It is a chore, not a blocker, and it has its
    own silhouette so the two are distinguishable without relying on colour.

    The badges are painted rather than set as an icon because QToolButton puts
    its one icon beside the text, and these belong after it: the label is what
    the button is, the badges are what it currently has to report.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._badges: list[QIcon] = []
        self.setText("This machine")
        self.setIcon(machine_icon(_BADGE_PX))
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.set_verdict(SectionState("ok", "Ready"), "")

    def set_verdict(self, verdict: SectionState, update_version: str) -> None:
        """Repaint the badges, and say the same thing in words for a reader."""
        badges = []
        if verdict.state == BLOCKED:
            badges.append(blocked_icon(_BADGE_PX))
        elif verdict.state == ATTENTION:
            badges.append(warning_icon(_BADGE_PX))
        if update_version:
            badges.append(download_icon(_BADGE_PX, QColor(UPDATE)))
        self._badges = badges
        # The badges are painted into reserved padding rather than over the
        # label, so the text does not shift as they come and go.
        reserved = SPACE_SM + len(badges) * (_BADGE_PX + _BADGE_GAP)
        self.setStyleSheet(_machine_button_qss(reserved))
        self.setToolTip(verdict.reason)
        # One phrase per glyph, so the name a screen reader announces and the
        # badges a sighted user sees are the same two facts in the same order.
        name = f"This machine: {verdict.count}"
        if update_version:
            name += f", version {update_version} available"
        self.setAccessibleName(name)
        self.setAccessibleDescription(verdict.reason)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().paintEvent(event)
        if not self._badges:
            return
        painter = QPainter(self)
        count = len(self._badges)
        width = count * _BADGE_PX + (count - 1) * _BADGE_GAP
        x = self.width() - SPACE_SM - width
        y = (self.height() - _BADGE_PX) // 2
        for badge in self._badges:
            badge.paint(painter, int(x), y, _BADGE_PX, _BADGE_PX)
            x += _BADGE_PX + _BADGE_GAP
        painter.end()


class SimpleMachineMixin(MixinBase):
    """DeepReefMapWindow methods for the This machine destination and its button."""

    _machine_view: str = "readiness"

    def _build_machine_page(self) -> QWidget:
        """One page, three views, and a segmented control to pick between them.

        A segmented control rather than tabs or another header entry: these are
        three ways of looking at one computer, not three places to go, and the
        component is the one Browse already uses for the same job.
        """
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(GUTTER)

        header = QHBoxLayout()
        header.setSpacing(GUTTER)
        header.addWidget(SectionHeader("This machine"))
        header.addStretch(1)
        self._machine_view_buttons: dict[str, QToolButton] = {}
        group = QButtonGroup(page)
        group.setExclusive(True)
        for index, view in enumerate(MACHINE_VIEWS):
            button = QToolButton()
            button.setText(_VIEW_LABELS[view])
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setToolTip(_VIEW_TIPS[view])
            button.setStyleSheet(
                segmented_qss(first=index == 0, last=index == len(MACHINE_VIEWS) - 1)
            )
            button.clicked.connect(partial(self._set_machine_view, view))
            group.addButton(button)
            header.addWidget(button)
            self._machine_view_buttons[view] = button
        outer.addLayout(header)

        self._machine_stack = QStackedWidget()
        views = {
            "readiness": self._build_readiness_view(),
            "models": self._build_machine_host("_machine_models_host"),
            "system": self._build_machine_system_view(),
        }
        for view in MACHINE_VIEWS:
            self._machine_stack.addWidget(views[view])
        outer.addWidget(self._machine_stack, 1)

        self._set_machine_view("readiness")
        return page

    def _build_machine_host(self, attribute: str) -> QWidget:
        """A scrolling slot for a panel that is lent here from its home.

        The panels are long and the window is not, so each view scrolls on its
        own rather than the destination scrolling as one and carrying the
        segmented control off the top of the screen with it.
        """
        host = QWidget()
        host_layout = QVBoxLayout(host)
        host_layout.setContentsMargins(0, 0, 0, 0)
        setattr(self, attribute, host)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(host)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        return scroll

    def _build_machine_system_view(self) -> QWidget:
        """The system panel, with the folder every run is written to above it.

        The output root belongs to the machine rather than to a run: it is the
        one setting that decides where a whole survey lands, and it used to
        decide that from inside a form nobody ever saw.
        """
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(GUTTER)

        card, card_layout = section_card("Where results are saved")
        caption = QLabel(
            "Every run, and the survey database that tracks them, is written under "
            "this folder."
        )
        caption.setWordWrap(True)
        caption.setStyleSheet(f"color: {TEXT_MUTED};")
        card_layout.addWidget(caption)
        self._machine_out_root_host = QWidget()
        host_layout = QVBoxLayout(self._machine_out_root_host)
        host_layout.setContentsMargins(0, 0, 0, 0)
        card_layout.addWidget(self._machine_out_root_host)
        layout.addWidget(card)

        layout.addWidget(self._build_machine_host("_machine_system_host"), 1)
        return page

    def _build_machine_nav_button(self) -> MachineButton:
        """Header entry point, and the machine's own status line.

        Never the only place a blocker shows: the Run step names the same
        problem beside the button that is refusing to start, which is where
        somebody trying to work will be looking.
        """
        button = MachineButton()
        button.clicked.connect(lambda: self._set_simple_section("machine"))
        self._machine_nav_button = button
        return button

    def _host_machine_panels(self) -> None:
        """Take the panels out of their homes and onto this page.

        The homes still exist because the run settings dialog borrows the form
        out of one of them, and because a widget needs somewhere to be returned
        to. Moving rather than rebuilding: two copies of the model library would
        disagree the moment a download finished against only one of them.
        """
        for widget_attr, host_attr in (
            ("_models_page", "_machine_models_host"),
            ("_system_page", "_machine_system_host"),
            ("_out_root_widget", "_machine_out_root_host"),
        ):
            widget = getattr(self, widget_attr)
            host = getattr(self, host_attr)
            layout = host.layout()
            if layout is not None and widget.parentWidget() is not host:
                layout.addWidget(widget)

    def _set_machine_view(self, view: str) -> None:
        if view not in MACHINE_VIEWS:
            raise ValueError(f"Unknown machine view: {view!r}")
        self._machine_view = view
        for name, button in self._machine_view_buttons.items():
            button.blockSignals(True)
            button.setChecked(name == view)
            button.blockSignals(False)
        self._machine_stack.setCurrentIndex(MACHINE_VIEWS.index(view))
        self._sync_system_gauges_running()

    def _sync_system_gauges_running(self) -> None:
        """The 1 Hz gauge poll runs only while the gauges are on screen."""
        timer = getattr(self, "_sys_timer", None)
        if timer is None:
            return
        if self._current_section() == "machine" and self._machine_view == "system":
            self._refresh_system_gauges()
            self._refresh_recorded_runs()
            timer.start()
        else:
            timer.stop()

    def _machine_verdict(self) -> SectionState:
        """This computer's verdict, from the checks the Run gate blocks on."""
        checks = self._current_setup_checks()
        return machine_state(
            unmet=sum(1 for check in checks if not check.ok),
            advisory=getattr(self, "_memory_advisory", ""),
            update_version=getattr(self, "_update_available", ""),
        )

    def _refresh_machine_button(self) -> None:
        """Repaint the header button from a fresh verdict.

        Called from the readiness repaint, the update check and the memory
        grade, which is every input the verdict has.
        """
        button = getattr(self, "_machine_nav_button", None)
        if button is None:
            return
        button.set_verdict(self._machine_verdict(), getattr(self, "_update_available", ""))
