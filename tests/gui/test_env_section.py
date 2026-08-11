"""The installed-versions section on Setup's Updates view.

It only exists under an installed binary, so every test here mocks one: in a dev
checkout `_build_env_section` returns before building anything.
"""

from __future__ import annotations

from PySide6.QtWidgets import QLabel

GB = 1024**3


def _env(version: str, *, current: bool, reclaimable: int, apparent: int) -> dict:
    return {
        "version": version,
        "path": f"/pyapp/deepreefmap-gui/dist/{version}",
        "current": current,
        "reclaimable": reclaimable,
        "apparent": apparent,
    }


def _row_labels(window) -> list[str]:
    layout = window._env_list_layout
    texts = []
    for index in range(layout.count()):
        row = layout.itemAt(index).widget()
        label = row if isinstance(row, QLabel) else row.findChild(QLabel)
        texts.append(label.text())
    return texts


def test_the_section_builds_under_an_installed_binary(monkeypatch, make_window):
    monkeypatch.setenv("DEEPREEFMAP_MOCK_PYAPP", "1")
    window = make_window()

    assert _row_labels(window) == ["Measuring environments…"]
    assert "Measuring downloaded models" in window._model_cache_label.text()


def test_managing_models_lands_on_the_models_view(monkeypatch, make_window):
    """Expected behaviour: the button reaches Setup's Models view. It used to
    address a Models tab, which no longer exists."""
    monkeypatch.setenv("DEEPREEFMAP_MOCK_PYAPP", "1")
    window = make_window()

    window._manage_models_btn.click()

    assert window._machine_view == "models"


def test_each_version_reports_what_deleting_it_frees(monkeypatch, make_window):
    monkeypatch.setenv("DEEPREEFMAP_MOCK_PYAPP", "1")
    window = make_window()

    window._apply_envs(
        {
            "environments": [
                _env("1.2.0", current=True, reclaimable=2 * GB, apparent=5 * GB),
                _env("1.1.0", current=False, reclaimable=1 * GB, apparent=4 * GB),
            ],
            "model_bytes": 3 * GB,
        }
    )

    running, older = _row_labels(window)
    assert "1.2.0" in running and "(running)" in running
    assert "2.0 GB on disk" in running and "3.0 GB shared with the cache" in running
    assert "1.1.0" in older and "(running)" not in older
    assert "3.0 GB" in window._model_cache_label.text()


def test_the_running_version_offers_no_delete(monkeypatch, make_window):
    monkeypatch.setenv("DEEPREEFMAP_MOCK_PYAPP", "1")
    window = make_window()

    window._apply_envs(
        {
            "environments": [
                _env("1.2.0", current=True, reclaimable=2 * GB, apparent=5 * GB),
                _env("1.1.0", current=False, reclaimable=1 * GB, apparent=4 * GB),
            ],
            "model_bytes": None,
        }
    )

    layout = window._env_list_layout
    buttons = [
        [b.text() for b in layout.itemAt(i).widget().findChildren(type(window._manage_models_btn))]
        for i in range(layout.count())
    ]
    assert buttons == [[], ["Delete"]]
    assert "size unavailable" in window._model_cache_label.text()


def test_no_environments_says_so(monkeypatch, make_window):
    monkeypatch.setenv("DEEPREEFMAP_MOCK_PYAPP", "1")
    window = make_window()

    window._apply_envs({"environments": [], "model_bytes": 0})

    assert _row_labels(window) == ["No installed environments found."]
