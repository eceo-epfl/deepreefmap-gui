"""Where a hover card lands, given an anchor near an edge of the screen.

Scenario: the cards this places follow controls that sit in the corners of the
window: the drive buttons at the foot, the running row of a queue. A card
placed at a fixed offset from those spills off the screen, which is what these
cover: for every corner, the card has to end up whole and on screen.

Expected behaviour: clamping alone is not enough. A card asked to sit above an
anchor at the top of the screen has a negative y, and clamping would pin it over
the anchor it describes; it has to flip below instead.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, QRect, QSize
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QLabel, QWidget

from deepreefmap_gui.core.hover_card import (
    apply_hover_card_flags,
    place_near_cursor,
    place_near_widget,
)


@pytest.fixture
def card(qapp) -> QWidget:
    widget = QLabel("a card with some width to it\nand a second line")
    apply_hover_card_flags(widget)
    widget.adjustSize()
    return widget


def bounds() -> QRect:
    return QGuiApplication.primaryScreen().availableGeometry()


def corners(size: QSize) -> list[QPoint]:
    """One anchor point hard against each corner of the screen, and the middle."""
    area = bounds()
    return [
        QPoint(area.left(), area.top()),
        QPoint(area.right() - size.width(), area.top()),
        QPoint(area.left(), area.bottom() - size.height()),
        QPoint(area.right() - size.width(), area.bottom() - size.height()),
        area.center(),
    ]


def test_a_card_placed_against_a_widget_stays_on_screen(card, qapp) -> None:
    anchor = QWidget()
    anchor.resize(120, 24)
    for point in corners(anchor.size()):
        rect = QRect(point, anchor.size())
        place_near_widget(card, anchor, anchor_rect=rect)
        assert bounds().contains(card.geometry()), f"anchored at {point}"


def test_a_card_placed_near_the_cursor_stays_on_screen(card, qapp) -> None:
    for point in corners(QSize(1, 1)):
        place_near_cursor(card, point)
        assert bounds().contains(card.geometry()), f"cursor at {point}"


def test_no_room_above_puts_the_card_below_rather_than_over_the_anchor(card, qapp) -> None:
    """Clamping instead of flipping would cover the thing being described."""
    area = bounds()
    anchor = QWidget()
    anchor.resize(120, 24)
    rect = QRect(QPoint(area.center().x(), area.top()), anchor.size())

    place_near_widget(card, anchor, anchor_rect=rect, prefer="above")

    assert card.geometry().top() >= rect.bottom()


def test_no_room_below_puts_the_card_above(card, qapp) -> None:
    area = bounds()
    anchor = QWidget()
    anchor.resize(120, 24)
    rect = QRect(QPoint(area.center().x(), area.bottom() - 24), anchor.size())

    place_near_widget(card, anchor, anchor_rect=rect, prefer="below")

    assert card.geometry().bottom() <= rect.top()


def test_the_flags_let_a_plain_widget_paint_its_own_border(card) -> None:
    """A QWidget ignores a stylesheet box model without WA_StyledBackground."""
    from PySide6.QtCore import Qt

    assert card.testAttribute(Qt.WidgetAttribute.WA_StyledBackground)
    assert card.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    assert card.windowFlags() & Qt.WindowType.FramelessWindowHint
