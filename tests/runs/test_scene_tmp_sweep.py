"""load_run's sweep of leftover scene .tmp files.

Scenario: a run whose scene file is being generated on a background thread is
opened again. load_run reaches the sweep precisely because the scene file is not
there yet, so the write in flight is the one most exposed to it.

Expected behaviour: debris goes, a live write stays. The slow path is allowed to
fail here -- these drive an empty run dir, and the sweep runs before it.
"""

from __future__ import annotations

import os
import time

import pytest

from deepreefmap_gui.io.scene_file import _TMP_ABANDONED_AFTER_S, SCENE_FILE_SUFFIX
from deepreefmap_gui.runs.loaded_run import load_run


def _tmp(run_dir, name, *, age_s=0.0):
    path = run_dir / (name + SCENE_FILE_SUFFIX + ".tmp")
    path.write_bytes(b"partially written scene")
    if age_s:
        stamp = time.time() - age_s
        os.utime(path, (stamp, stamp))
    return path


def test_the_sweep_keeps_a_live_write_and_drops_the_debris(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    live = _tmp(run_dir, "being_written")
    debris = _tmp(run_dir, "killed_last_week", age_s=_TMP_ABANDONED_AFTER_S + 3600)

    with pytest.raises(FileNotFoundError):
        load_run(run_dir)

    assert live.exists(), "deleting this fails the write and pins the run to the slow path"
    assert not debris.exists()


def test_a_write_registered_by_this_process_survives_regardless_of_age(tmp_path, monkeypatch):
    """The in-process registry is what covers the same-app case, where mtime
    could still look old between two slow chunk flushes."""
    import deepreefmap_gui.io.scene_file as scene_file

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    live = _tmp(run_dir, "being_written", age_s=_TMP_ABANDONED_AFTER_S + 3600)
    monkeypatch.setattr(scene_file, "_ACTIVE_TMP_PATHS", {live})

    with pytest.raises(FileNotFoundError):
        load_run(run_dir)

    assert live.exists()
