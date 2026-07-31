"""Wait on queued cross-thread signals from GUI tests.

Not a conftest: a plain function, imported from tests/gui/ the same way
_factories is, so every suite that adds videos waits the same way.
"""

from __future__ import annotations

import time
from collections.abc import Callable


def wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    """Pump the event loop until ``predicate()`` holds, or the timeout passes.

    Queueing a video probes it on a worker thread and appends its row from a
    queued signal, so asserting straight after the call races the worker.
    """
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        if app is not None:
            app.processEvents()
        time.sleep(0.005)
    return predicate()
