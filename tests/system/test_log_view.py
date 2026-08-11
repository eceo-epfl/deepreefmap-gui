"""The stdout/stderr shim that pipes library output into the in-app log."""

from __future__ import annotations

import logging


def test_stream_to_logger_buffers_partial_lines(caplog) -> None:
    from deepreefmap_gui.system.log_view import _StreamToLogger

    logger = logging.getLogger("deepreefmap.test_stream")
    shim = _StreamToLogger(logger, logging.INFO)
    with caplog.at_level(logging.INFO, logger="deepreefmap.test_stream"):
        shim.write("hel")
        shim.write("lo\nwor")
        assert [r.message for r in caplog.records] == ["hello"]
        shim.flush()
    assert [r.message for r in caplog.records] == ["hello", "wor"]
    assert shim.isatty() is False


def test_stream_to_logger_drops_bar_redraws(caplog) -> None:
    from deepreefmap_gui.system.log_view import _StreamToLogger

    logger = logging.getLogger("deepreefmap.test_stream_cr")
    shim = _StreamToLogger(logger, logging.WARNING)
    with caplog.at_level(logging.WARNING, logger="deepreefmap.test_stream_cr"):
        shim.write("frame 1/10\rframe 2/10\r")
        shim.write("frame 3/10\n")
        shim.write("   \n")
    assert [r.message for r in caplog.records] == ["frame 3/10"]

