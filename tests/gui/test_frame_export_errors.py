"""What the user is told when exporting the current frame fails.

Scenario: the export reads a composite from the viewer. That read can fail for
two unrelated reasons: the viewer genuinely has no such capability, or the call
raised while doing its job.

Expected behaviour: the two are reported differently. They were not: the second
was caught by an `except AttributeError` meant for the first, so a bug inside
current_frame_stack was reported as a missing feature.
"""

from __future__ import annotations

import pytest


class _ViewerWithoutExport:
    """An older viewer that never had the method."""


class _ViewerThatRaises:
    """The method exists and fails, which is the case that was mis-reported."""

    def current_frame_stack(self):
        raise AttributeError("'NoneType' object has no attribute 'frame_order'")


class _ViewerWithNoFrame:
    def current_frame_stack(self):
        return None


@pytest.fixture
def exporting(window):
    """A window whose slider reports frames, so the export gets past its guard."""
    window._frame_slider.setMaximum(10)
    window._frame_slider.setValue(3)
    return window


def test_a_viewer_without_the_method_reports_a_missing_capability(exporting):
    exporting._viewer = _ViewerWithoutExport()

    exporting._on_export_current_frame()

    assert "doesn't support" in exporting._status_label.text()


def test_a_failure_inside_the_call_reports_the_real_error(exporting):
    """The message used to say frame export was unsupported, sending the user
    to look for a missing feature instead of at the traceback."""
    exporting._viewer = _ViewerThatRaises()

    exporting._on_export_current_frame()

    text = exporting._status_label.text()
    assert "doesn't support" not in text, "a bug was reported as a missing feature"
    assert "frame_order" in text, "the real error is not in the message"


def test_no_current_frame_is_its_own_message(exporting):
    exporting._viewer = _ViewerWithNoFrame()

    exporting._on_export_current_frame()

    assert "not available" in exporting._status_label.text()


def test_no_frames_at_all_is_reported_before_the_viewer_is_asked(window):
    window._frame_slider.setMaximum(0)
    window._viewer = _ViewerThatRaises()

    window._on_export_current_frame()

    assert "No frames" in window._status_label.text()
