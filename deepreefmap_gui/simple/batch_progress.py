"""Session progress on Process: which pass is running, and how long is left.

Fed the same per-run signals as the viewer's ProgressPanel, but it reports the
whole batch rather than the pass in flight: the bar spans every queued pass, and
the estimate carries on past the one being processed.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QProgressBar, QVBoxLayout, QWidget

from deepreefmap_gui.core.theme import PRIMARY, TEXT_SECONDARY
from deepreefmap_gui.core.widgets import secondary_label, section_card
from deepreefmap_gui.profiling.eta import format_remaining

# Below this the pass has not run long enough for its own fill to say anything
# about how long the rest of it will take.
_MIN_PERCENT_TO_EXTRAPOLATE = 5


class BatchProgressCard(QWidget):
    """Overall progress of a survey batch, sized in passes rather than frames."""

    pass_percent_changed = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card, layout = section_card()
        outer.addWidget(card)

        self._heading = QLabel()
        self._heading.setStyleSheet(f"color: {PRIMARY}; font-weight: bold;")
        layout.addWidget(self._heading)

        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setTextVisible(True)
        layout.addWidget(self._bar)

        self._eta = secondary_label()
        layout.addWidget(self._eta)

        self._detail = QLabel()
        self._detail.setWordWrap(True)
        self._detail.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self._detail)

        self._index = 0
        self._total = 0
        self._pass_percent = 0
        self._pass_seconds: float | None = None
        self._remaining_s: float | None = None
        self.set_idle("No batch in progress.")

    # --- Batch shape ---

    def set_batch_plan(self, total: int, pass_seconds: float | None) -> None:
        """How many passes this batch holds, and what one has cost before now.

        ``pass_seconds`` is the median of this machine's recorded runs, or None on
        a laptop with no history, in which case the estimate is inferred from the
        pass in flight once it has run far enough to be worth extrapolating.
        """
        self._total = total
        self._pass_seconds = pass_seconds

    def set_batch_context(self, index: int, total: int, name: str) -> None:
        self._index = index
        self._total = total
        self._pass_percent = 0
        self._remaining_s = None
        self._heading.setText(f"Processing pass {index} of {total} · {name}")
        self._heading.setVisible(True)
        self._bar.setVisible(True)
        self._render()

    def clear_batch_context(self) -> None:
        self._index = 0
        self._heading.setVisible(False)

    # --- Live run signals ---

    def set_percent(self, percent: int) -> None:
        self._pass_percent = max(0, min(100, int(percent)))
        self.pass_percent_changed.emit(self._pass_percent)
        self._render()

    def set_eta_seconds(self, seconds: float | None) -> None:
        self._remaining_s = seconds
        self._render()

    def set_eta(self, text: str) -> None:
        """The pass-scoped wording; the card shows a batch-scoped one instead."""

    def set_status_html(self, text: str) -> None:
        self._detail.setText(text)

    def set_idle(self, message: str) -> None:
        self._heading.setVisible(False)
        self._bar.setValue(0)
        self._bar.setVisible(False)
        self._eta.setText("")
        self._detail.setText(f'<span style="color: {TEXT_SECONDARY};">{message}</span>')
        self._index = 0
        self._pass_percent = 0
        self._remaining_s = None

    # --- Rendering ---

    def _batch_percent(self) -> int:
        if self._total <= 0 or self._index <= 0:
            return 0
        done = (self._index - 1) + self._pass_percent / 100.0
        return int(round(100.0 * done / self._total))

    def batch_remaining_s(self) -> float | None:
        """Seconds left across every pass still to process, or None if unknown."""
        if self._remaining_s is None or self._total <= 0 or self._index <= 0:
            return None
        after = max(0, self._total - self._index)
        if after == 0:
            return self._remaining_s
        per_pass = self._pass_seconds
        if per_pass is None and self._pass_percent >= _MIN_PERCENT_TO_EXTRAPOLATE:
            # No history to lean on, so infer a whole pass from how much of this
            # one is left and how far through it is.
            per_pass = self._remaining_s / (1.0 - self._pass_percent / 100.0)
        if per_pass is None:
            return None
        return self._remaining_s + after * per_pass

    def _render(self) -> None:
        self._bar.setValue(self._batch_percent())
        remaining = self.batch_remaining_s()
        if remaining is None:
            self._eta.setText("Estimating time for the batch…")
        else:
            self._eta.setText(f"{format_remaining(remaining)} left for the batch")
