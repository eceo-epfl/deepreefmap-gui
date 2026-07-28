"""Crash-safety of the shared file-replacement helper.

Expected behaviour: a write that does not complete leaves the previous contents
in place, and leaves no scratch files behind.
"""

from __future__ import annotations

import json

import pytest

from deepreefmap_gui.io.atomic import atomic_write_json, atomic_write_text


def test_writes_a_new_file(tmp_path):
    target = tmp_path / "nested" / "config.txt"

    atomic_write_text(target, "hello")

    assert target.read_text() == "hello"


def test_replaces_an_existing_file(tmp_path):
    target = tmp_path / "config.txt"
    target.write_text("old")

    atomic_write_text(target, "new")

    assert target.read_text() == "new"


def test_a_failed_write_leaves_the_previous_contents(tmp_path, monkeypatch):
    """The whole point: `write_text` truncates first, so a mid-write failure
    destroys the file it was updating."""
    target = tmp_path / "config.txt"
    target.write_text("survivor")

    def explode(*_args, **_kwargs):
        raise OSError("No space left on device")

    monkeypatch.setattr("os.replace", explode)

    with pytest.raises(OSError, match="No space left"):
        atomic_write_text(target, "replacement")

    assert target.read_text() == "survivor"


def test_a_failed_write_leaves_no_scratch_file(tmp_path, monkeypatch):
    target = tmp_path / "config.txt"
    target.write_text("survivor")
    monkeypatch.setattr("os.replace", lambda *a, **k: (_ for _ in ()).throw(OSError()))

    with pytest.raises(OSError):
        atomic_write_text(target, "replacement")

    assert [p.name for p in tmp_path.iterdir()] == ["config.txt"]


def test_unserialisable_json_does_not_touch_the_destination(tmp_path):
    """Serialising before opening the file means a bad payload is caught with
    the old contents still intact."""
    target = tmp_path / "data.json"
    target.write_text('{"kept": true}')

    with pytest.raises(TypeError):
        atomic_write_json(target, {"bad": object()})

    assert json.loads(target.read_text()) == {"kept": True}
    assert [p.name for p in tmp_path.iterdir()] == ["data.json"]


def test_concurrent_writers_do_not_share_a_scratch_file(tmp_path, monkeypatch):
    """A fixed `.tmp` name lets a second process replace the target with the
    first one's half-written bytes."""
    target = tmp_path / "data.json"
    names: list[str] = []
    real_replace = __import__("os").replace

    def record(src, dst):
        names.append(str(src))
        real_replace(src, dst)

    monkeypatch.setattr("os.replace", record)

    atomic_write_json(target, {"a": 1})
    atomic_write_json(target, {"a": 2})

    assert names[0] != names[1]
    assert json.loads(target.read_text()) == {"a": 2}
