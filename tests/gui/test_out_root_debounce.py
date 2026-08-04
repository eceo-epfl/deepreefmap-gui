"""What happens while the output root is being typed.

Scenario: the user types or pastes a path into the output-root field. Every
keystroke emits textChanged.

Expected behaviour: the tree walk and the survey-store open happen once the path
has settled, not per character. SurveyStore creates its database under whatever
path it is handed, so scanning a half-typed path is not merely slow -- it leaves
a survey.db behind in every parent directory that happens to exist already.
"""

from __future__ import annotations

import pytest

from deepreefmap_gui.survey import catalogue
from deepreefmap_gui.survey.store import SURVEY_DB_NAME


@pytest.fixture
def scan_spy(monkeypatch):
    calls: list = []

    def fake_scan(root):
        calls.append(root)
        return []

    monkeypatch.setattr(catalogue, "scan_out_root", fake_scan)
    return calls


def _type(window, text: str) -> None:
    """Enter text one character at a time, as textChanged sees it."""
    window._out_root_input.clear()
    for i in range(1, len(text) + 1):
        window._out_root_input.setText(text[:i])


def _settle(ms: int = 500) -> None:
    """Run the event loop long enough for the debounce to fire."""
    from PySide6.QtCore import QEventLoop, QTimer

    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def test_typing_a_path_does_not_scan_per_keystroke(window, scan_spy, tmp_path):
    target = tmp_path / "surveys" / "reef"
    target.mkdir(parents=True)
    scan_spy.clear()

    _type(window, str(target))

    assert scan_spy == [], "every keystroke walked the filesystem"


def test_the_scan_happens_once_the_path_settles(window, scan_spy, tmp_path):
    target = tmp_path / "surveys" / "reef"
    target.mkdir(parents=True)
    scan_spy.clear()

    _type(window, str(target))
    _settle()

    # Deliberately unfiltered. This is the only test that runs an event loop, so
    # any work another test queued and never drove lands here -- an extra entry
    # means something leaked, which is worth failing on.
    assert scan_spy == [target]


def test_typing_leaves_no_database_in_the_parents_it_passes(window, tmp_path):
    """The parents of a real path are themselves real, so an unguarded scan
    opens a store in each one on the way past."""
    target = tmp_path / "surveys" / "reef"
    target.mkdir(parents=True)
    # The window already opened a store under its own root at construction.
    before = set(tmp_path.rglob(SURVEY_DB_NAME))

    _type(window, str(target))
    _settle()

    strays = set(tmp_path.rglob(SURVEY_DB_NAME)) - before - {target / SURVEY_DB_NAME}
    assert not strays, f"a survey database was created under {sorted(str(p) for p in strays)}"


def test_the_field_holds_what_was_typed_while_the_scan_waits(window, scan_spy, tmp_path):
    """The debounce defers the scan, not the text: the field is what the user
    reads back, and every later run resolves its output root from it."""
    target = tmp_path / "surveys"
    target.mkdir(parents=True)

    _type(window, str(target))

    assert window._out_root_input.text() == str(target)
