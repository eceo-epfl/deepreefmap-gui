"""Where you were before a link took you somewhere else.

Only cross-page jumps push. Choosing a destination directly is a fresh start,
not a step to unwind.

A selection is part of a place: returning to a queue with nothing selected is
half a return, so each entry carries the row as well as the page.
"""

from __future__ import annotations

from dataclasses import dataclass

# Enough to walk back through an afternoon's jumping about, bounded so a long
# session cannot grow it without limit.
_MAX_DEPTH = 50


@dataclass(frozen=True)
class Place:
    """A destination, and what was picked out on it."""

    section: str
    selection: str | None = None


class NavigationHistory:
    """Back and forward over the places a jump has taken you.

    A new jump discards anything forward of where you are.
    """

    def __init__(self) -> None:
        self._places: list[Place] = []
        self._index = -1

    def __len__(self) -> int:
        return len(self._places)

    @property
    def can_go_back(self) -> bool:
        return self._index > 0

    @property
    def can_go_forward(self) -> bool:
        return -1 < self._index < len(self._places) - 1

    def current(self) -> Place | None:
        if 0 <= self._index < len(self._places):
            return self._places[self._index]
        return None

    def push(self, place: Place) -> None:
        """Record arriving somewhere, unless that is where we already are."""
        if self.current() == place:
            return
        del self._places[self._index + 1 :]
        self._places.append(place)
        if len(self._places) > _MAX_DEPTH:
            del self._places[0]
        self._index = len(self._places) - 1

    def back(self) -> Place | None:
        if not self.can_go_back:
            return None
        self._index -= 1
        return self._places[self._index]

    def forward(self) -> Place | None:
        if not self.can_go_forward:
            return None
        self._index += 1
        return self._places[self._index]

    def drop_current(self) -> Place | None:
        """Forget where we just tried to go, because it is no longer there.

        The entry is removed and the one before it answers, so a dead step costs
        no extra press. The index steps back rather than merely deleting:
        otherwise whatever shifts into the freed slot answers instead.
        """
        if not 0 <= self._index < len(self._places):
            return None
        del self._places[self._index]
        self._index -= 1
        return self.current()
