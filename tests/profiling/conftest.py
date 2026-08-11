from __future__ import annotations

import pytest


@pytest.fixture
def timings(tmp_path, monkeypatch):
    """The run-timings profile this test writes to, one per test."""
    path = tmp_path / "run_timings.json"
    monkeypatch.setenv("DEEPREEFMAP_RUN_TIMINGS", str(path))
    return path
