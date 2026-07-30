"""Per-version environment discovery, hardlink-aware sizing, and deletion."""

from __future__ import annotations

import os

import pytest

from deepreefmap_gui.packaging import environments


def _dist_dir(tmp_path):
    """A fake PyApp `<dist_id>` dir with the `pyapp` component the guards require."""
    return tmp_path / "pyapp" / "deepreefmap-gui" / "hash"


def test_list_environments_flags_the_running_one(tmp_path, monkeypatch) -> None:
    dist = _dist_dir(tmp_path)
    (dist / "1.0.0" / "python").mkdir(parents=True)
    (dist / "1.1.0" / "python").mkdir(parents=True)
    monkeypatch.setattr(environments.sys, "prefix", str(dist / "1.1.0" / "python"))

    envs = {e.version: e.current for e in environments.list_environments()}

    assert envs == {"1.1.0": True, "1.0.0": False}


def test_list_environments_empty_in_a_dev_checkout(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(environments.sys, "prefix", str(tmp_path / "venv" / "python"))
    assert environments.list_environments() == []


def test_env_disk_usage_excludes_bytes_shared_with_the_cache(tmp_path) -> None:
    env = tmp_path / "env"
    env.mkdir()
    (env / "unique.pyc").write_bytes(b"u" * 1000)  # env-private: freed on delete
    shared = env / "shared.so"
    shared.write_bytes(b"s" * 5000)
    # A hardlink from "the cache" (outside the env) bumps shared to nlink=2, so
    # deleting the env would not free it.
    os.link(shared, tmp_path / "cache_copy.so")

    reclaimable, apparent = environments.env_disk_usage(env)

    assert reclaimable == 1000  # only the unique file
    assert apparent == 6000  # both files, each counted once


def test_delete_environment_refuses_the_running_one(tmp_path, monkeypatch) -> None:
    dist = _dist_dir(tmp_path)
    (dist / "1.1.0" / "python").mkdir(parents=True)
    monkeypatch.setattr(environments.sys, "prefix", str(dist / "1.1.0" / "python"))

    with pytest.raises(ValueError, match="running environment"):
        environments.delete_environment(dist / "1.1.0")
    assert (dist / "1.1.0").exists()


def test_delete_environment_removes_a_non_current_one(tmp_path, monkeypatch) -> None:
    dist = _dist_dir(tmp_path)
    (dist / "1.1.0" / "python").mkdir(parents=True)
    (dist / "1.0.0" / "python").mkdir(parents=True)
    monkeypatch.setattr(environments.sys, "prefix", str(dist / "1.1.0" / "python"))

    environments.delete_environment(dist / "1.0.0")

    assert not (dist / "1.0.0").exists()
    assert (dist / "1.1.0").exists()


def test_delete_environment_refuses_a_non_pyapp_path(tmp_path) -> None:
    victim = tmp_path / "important"
    victim.mkdir()
    with pytest.raises(ValueError, match="non-PyApp"):
        environments.delete_environment(victim)
    assert victim.exists()
