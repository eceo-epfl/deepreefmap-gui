"""Model catalogue, cache detection, and discovery.

Covers deepreefmap_gui/models/cache.py (ALL_MODELS metadata, HF-cache status
detection, prefetch guards) and deepreefmap_gui/models/families.py (synthesising
ModelInfo from repo names, registry dispatch by family).
"""

from __future__ import annotations

import json

import pytest
from _factories import repo_commit, write_cache_repo
from deepreefmap.segmentation.dinov3_dpt import DinoV3DPTWrapper
from deepreefmap.segmentation.registry import (
    create_segmentation_model,
    list_segmentation_models,
    model_processing_size,
    register_segmentation_model,
)
from deepreefmap.segmentation.segformer import SegformerWrapper

from deepreefmap_gui.models.cache import ALL_MODELS, ModelInfo, register_discovered
from deepreefmap_gui.models.families import synthesize_model_info


def test_model_list_has_all_expected_models() -> None:

    names = {m.name for m in ALL_MODELS}
    assert "segformer-b2" in names
    assert "scsfmlearner" in names
    assert "coralscapes-vit-b-dpt" in names


@pytest.mark.parametrize(
    "name, gated",
    [
        ("segformer-b2", False),
        ("coralscapes-vit-b-dpt", True),
    ],
)
def test_model_gated_flag(name, gated) -> None:

    info = next(m for m in ALL_MODELS if m.name == name)
    assert info.gated is gated


def test_cache_detection_returns_false_for_nonexistent() -> None:
    from deepreefmap_gui.models.cache import ModelInfo, is_model_cached

    fake = ModelInfo(
        name="fake",
        kind="test",
        hf_repos=["nonexistent-org/nonexistent-model-abc123"],
        gated=False,
        description="test",
    )
    assert not is_model_cached(fake)


def test_dinov3_dpt_entries_include_facebook_backbone() -> None:

    expected = {
        "coralscapes-vit-s-dpt": "facebook/dinov3-vits16-pretrain-lvd1689m",
        "coralscapes-vit-b-dpt": "facebook/dinov3-vitb16-pretrain-lvd1689m",
        "coralscapes-vit-l-dpt": "facebook/dinov3-vitl16-pretrain-lvd1689m",
    }
    for name, backbone in expected.items():
        info = next(m for m in ALL_MODELS if m.name == name)
        assert backbone in info.hf_repos, (
            f"{name} must list {backbone} so offline laptops also cache the "
            "DINOv3 backbone that coralscapes_hub_model.py pulls in at load time"
        )


def test_loger_entries_materialise_into_ckpts_dir() -> None:
    from deepreefmap.mapping.registry import _LOGER_CKPTS

    from deepreefmap_gui.models.cache import MAPPING_MODELS

    by_name = {m.name: m for m in MAPPING_MODELS}
    assert "loger" in by_name and "loger_star" in by_name

    loger = by_name["loger"]
    assert loger.hf_repos == ["Junyi42/LoGeR"]
    assert loger.materialise_to[
        "LoGeR/latest.pt"
    ] == _LOGER_CKPTS / "LoGeR" / "latest.pt"

    star = by_name["loger_star"]
    assert star.materialise_to[
        "LoGeR_star/latest.pt"
    ] == _LOGER_CKPTS / "LoGeR_star" / "latest.pt"


def test_resolve_model_versions_uses_the_source_commit_per_repo(tmp_path, monkeypatch) -> None:
    """The recorded id is HuggingFace's own refs/main commit, not one we compute."""
    from deepreefmap_gui.models import cache as model_manager
    from deepreefmap_gui.models.cache import resolve_model_versions

    cache = tmp_path / "hf"
    monkeypatch.setattr(model_manager, "_HF_CACHE_ROOT", cache)

    def write_ref(repo_id: str, commit: str) -> None:
        ref = cache / f"models--{repo_id.replace('/', '--')}" / "refs" / "main"
        ref.parent.mkdir(parents=True, exist_ok=True)
        ref.write_text(commit)

    write_ref("EPFL-ECEO/coralscapes-vit-s-dpt", "a" * 40)
    write_ref("facebook/dinov3-vits16-pretrain-lvd1689m", "b" * 40)
    write_ref("EPFL-ECEO/deepreefmap-sfm-net", "c" * 40)

    versions = resolve_model_versions(["coralscapes-vit-s-dpt", "scsfmlearner"])
    assert versions == {
        "EPFL-ECEO/coralscapes-vit-s-dpt": "a" * 40,
        "facebook/dinov3-vits16-pretrain-lvd1689m": "b" * 40,
        "EPFL-ECEO/deepreefmap-sfm-net": "c" * 40,
    }


def test_resolve_model_versions_skips_unknown_and_uncached(tmp_path, monkeypatch) -> None:
    from deepreefmap_gui.models import cache as model_manager
    from deepreefmap_gui.models.cache import resolve_model_versions

    monkeypatch.setattr(model_manager, "_HF_CACHE_ROOT", tmp_path / "empty")
    assert resolve_model_versions(["not-a-model", "scsfmlearner"]) == {}


def _write_snapshot(cache_root, repo_id, files):
    """Lay down a minimal HF-cache snapshot (refs/main + one revision) for a repo.

    `files` maps repo-relative paths to bytes/str contents. Mirrors the real
    cache layout closely enough for the offline verifier in model_manager.
    """
    repo_dir = cache_root / f"models--{repo_id.replace('/', '--')}"
    commit = "0" * 40
    (repo_dir / "refs").mkdir(parents=True, exist_ok=True)
    (repo_dir / "refs" / "main").write_text(commit)
    snap = repo_dir / "snapshots" / commit
    snap.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        path = snap / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode())
    return snap


def test_is_model_cached_requires_materialised_destinations(tmp_path, monkeypatch) -> None:
    from deepreefmap_gui.models import cache as model_manager
    from deepreefmap_gui.models.cache import ModelInfo, is_model_cached

    fake_cache = tmp_path / "hf"
    monkeypatch.setattr(model_manager, "_HF_CACHE_ROOT", fake_cache)
    _write_snapshot(fake_cache, "fake/repo", {"model.safetensors": b"weights"})

    dest = tmp_path / "ckpts" / "weight.pt"
    info = ModelInfo(
        name="materialise-fake",
        kind="test",
        hf_repos=["fake/repo"],
        gated=False,
        description="test",
        materialise_to={"weight.pt": dest},
    )
    assert not is_model_cached(info), "missing materialised file must read as not cached"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"x")
    assert is_model_cached(info)


def test_config_only_snapshot_reads_as_partial(tmp_path, monkeypatch) -> None:
    """A DPT head with only config.json (the crash-in-the-field state) must not
    read as cached: the custom loader named in config.json is missing."""
    from deepreefmap_gui.models import cache as model_manager
    from deepreefmap_gui.models.cache import ModelInfo, ModelStatus, is_model_cached, model_status

    fake_cache = tmp_path / "hf"
    monkeypatch.setattr(model_manager, "_HF_CACHE_ROOT", fake_cache)
    _write_snapshot(
        fake_cache,
        "fake/dpt-head",
        {"config.json": json.dumps({"hub_inference_module": "loader.py"})},
    )
    info = ModelInfo(
        name="dpt-head-fake",
        kind="segmentation",
        hf_repos=["fake/dpt-head"],
        gated=False,
        description="test",
    )
    status, reason = model_status(info)
    assert status is ModelStatus.PARTIAL
    assert "loader.py" in reason
    assert not is_model_cached(info)

    _write_snapshot(
        fake_cache,
        "fake/dpt-head",
        {
            "config.json": json.dumps({"hub_inference_module": "loader.py"}),
            "loader.py": "# custom loader",
            "model.safetensors": b"weights",
        },
    )
    assert model_status(info)[0] is ModelStatus.COMPLETE
    assert is_model_cached(info)


def test_metadata_only_snapshot_reads_as_absent(tmp_path, monkeypatch) -> None:
    """A stub with only README/LICENSE is 'not downloaded', not a repair case."""
    from deepreefmap_gui.models import cache as model_manager
    from deepreefmap_gui.models.cache import ModelInfo, ModelStatus, model_status

    fake_cache = tmp_path / "hf"
    monkeypatch.setattr(model_manager, "_HF_CACHE_ROOT", fake_cache)
    _write_snapshot(fake_cache, "fake/stub", {"README.md": "hi", "LICENSE.md": "x"})
    info = ModelInfo(
        name="stub-fake",
        kind="backbone",
        hf_repos=["fake/stub"],
        gated=True,
        description="test",
    )
    assert model_status(info)[0] is ModelStatus.ABSENT


def test_prefetch_refuses_when_disk_is_low(tmp_path, monkeypatch) -> None:
    from deepreefmap_gui.models import cache as model_manager
    from deepreefmap_gui.models.cache import (
        InsufficientDiskSpace,
        ModelInfo,
        prefetch_model,
    )

    monkeypatch.setattr(model_manager, "_HF_CACHE_ROOT", tmp_path)
    monkeypatch.setattr(
        model_manager.shutil,
        "disk_usage",
        lambda _p: type("U", (), {"total": 1, "used": 1, "free": 1})(),
    )
    info = ModelInfo(
        name="any",
        kind="test",
        hf_repos=["fake/repo"],
        gated=False,
        description="test",
    )
    with pytest.raises(InsufficientDiskSpace):
        prefetch_model(info)


def test_synthesize_dpt_carries_backbone_and_resolution() -> None:
    info, resolution, family = synthesize_model_info("EPFL-ECEO/coralscapes-vit-l-dpt")
    assert family == "dpt"
    assert info.name == "coralscapes-vit-l-dpt"
    assert info.gated is True
    assert resolution == (768, 1376)
    # Offline self-sufficiency: the DINOv3 backbone is cached alongside the head.
    assert info.hf_repos == [
        "EPFL-ECEO/coralscapes-vit-l-dpt",
        "facebook/dinov3-vitl16-pretrain-lvd1689m",
    ]


def test_synthesize_dpt_small_uses_small_resolution() -> None:
    _info, resolution, _family = synthesize_model_info("EPFL-ECEO/coralscapes-vit-s-dpt")
    assert resolution == (384, 688)


def test_model_processing_size_swaps_to_width_height() -> None:
    # _MODELS stores (height, width); processing/image sizes are (width, height).
    assert model_processing_size("coralscapes-vit-s-dpt") == (688, 384)
    assert model_processing_size("coralscapes-vit-b-dpt") == (1376, 768)
    assert model_processing_size("segformer-b2") == (1024, 1024)
    assert model_processing_size("no-such-model") is None


def test_synthesize_segformer_is_ungated_no_backbone() -> None:
    info, resolution, family = synthesize_model_info(
        "EPFL-ECEO/segformer-b4-finetuned-coralscapes-1024-1024"
    )
    assert family == "segformer"
    assert info.name == "segformer-b4"
    assert info.gated is False
    assert resolution == (1024, 1024)
    assert info.hf_repos == ["EPFL-ECEO/segformer-b4-finetuned-coralscapes-1024-1024"]


@pytest.mark.parametrize(
    "repo",
    [
        "EPFL-ECEO/deepreefmap-sfm-net",
        "EPFL-ECEO/coralscapes-vit-g-dpt",
        "EPFL-ECEO/some-other-repo",
    ],
)
def test_synthesize_unknown_repo_is_skipped(repo) -> None:
    assert synthesize_model_info(repo) is None


@pytest.fixture
def clean_segmentation_registry():
    """Restore the library's segmentation registry after a test writes to it.

    register_segmentation_model mutates module-level state shared with every
    later test in the session, so without this the models registered below stay
    visible in list_segmentation_models() for the rest of the run.
    """
    import deepreefmap.segmentation.registry as registry

    models, repos = dict(registry._MODELS), dict(registry._REPOS)
    yield
    registry._MODELS.clear()
    registry._MODELS.update(models)
    registry._REPOS.clear()
    registry._REPOS.update(repos)


def test_register_then_create_dispatches_by_family(clean_segmentation_registry) -> None:
    register_segmentation_model(
        "segformer-b4", "EPFL-ECEO/segformer-b4-finetuned-coralscapes-1024-1024",
        "segformer", (1024, 1024),
    )
    register_segmentation_model(
        "coralscapes-vit-x-dpt", "EPFL-ECEO/coralscapes-vit-x-dpt", "dpt", (768, 1376),
    )
    assert "segformer-b4" in list_segmentation_models()
    assert isinstance(create_segmentation_model("segformer-b4"), SegformerWrapper)
    assert isinstance(create_segmentation_model("coralscapes-vit-x-dpt"), DinoV3DPTWrapper)


def test_register_segmentation_model_is_no_op_for_known_name(clean_segmentation_registry) -> None:
    # Hardcoded entries stay authoritative: re-registering must not change family.
    register_segmentation_model("segformer-b2", "EPFL-ECEO/bogus", "dpt", (1, 1))
    assert isinstance(create_segmentation_model("segformer-b2"), SegformerWrapper)


def test_register_discovered_dedups_against_catalogue() -> None:
    duplicate = ModelInfo(
        name="segformer-b2", kind="segmentation",
        hf_repos=["EPFL-ECEO/segformer-b2-finetuned-coralscapes-1024-1024"],
        gated=False, description="dup",
    )
    assert register_discovered(duplicate) is False




def test_silent_tqdm_reports_percent_without_opening_a_file() -> None:
    """The progress shim used to hold an unclosed handle on os.devnull.

    Expected behaviour: tqdm's output is swallowed by an in-process sink, so a
    download that reports progress costs no file descriptor.
    """
    import psutil

    from deepreefmap_gui.models.cache import _make_silent_tqdm

    seen: list[tuple[int, int]] = []
    proc = psutil.Process()
    before = proc.num_fds() if hasattr(proc, "num_fds") else None

    tqdm_class = _make_silent_tqdm(lambda current, total: seen.append((current, total)))
    for _ in range(5):
        with tqdm_class(total=100, unit="B") as bar:
            bar.update(50)
            bar.update(50)

    assert seen and seen[-1] == (100, 100)
    if before is not None:
        assert proc.num_fds() == before


# --- deleting a model that shares its download ---------------------------
#
# Several catalogue entries point at one repo: loger and loger_star are two
# checkpoint folders in a single 4.8 GB download, and each DINOv3 backbone is
# listed both on its own and as the encoder a DPT head loads at first use.


def _write_repo(cache_root, repo_id, files):
    """A cache repo with real blobs and snapshot symlinks, as scan_cache_dir expects.

    Each repo gets its own commit hash, which is what these tests turn on.
    """
    return write_cache_repo(cache_root, repo_id, files, commit=repo_commit(repo_id))


@pytest.fixture
def shared_repo_cache(tmp_path, monkeypatch):
    """Two installed models backed by one repo, each materialising its own file."""
    from deepreefmap_gui.models import cache as model_manager

    cache = tmp_path / "hf"
    monkeypatch.setattr(model_manager, "_HF_CACHE_ROOT", cache)
    repo_dir = _write_repo(
        cache, "fake/shared", {"A/latest.pt": b"a-weights", "B/latest.pt": b"b-weights"}
    )

    def _entry(name, member):
        dest = tmp_path / "ckpts" / name / "latest.pt"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"materialised")
        return ModelInfo(
            name=name,
            kind="mapping",
            hf_repos=["fake/shared"],
            gated=False,
            description="test",
            materialise_to={member: dest},
        )

    first, second = _entry("first", "A/latest.pt"), _entry("second", "B/latest.pt")
    monkeypatch.setattr(model_manager, "all_known_models", lambda: [first, second])
    return model_manager, repo_dir, first, second


def test_deleting_one_model_keeps_the_repo_its_sibling_needs(shared_repo_cache) -> None:
    manager, repo_dir, first, second = shared_repo_cache
    assert manager.is_model_cached(second)

    result = manager.delete_model(first)

    assert manager.is_model_cached(second), "sibling lost the weights it shares"
    assert repo_dir.is_dir()
    assert result.revisions_removed == 0
    assert result.kept_repos == {"fake/shared": ["second"]}
    assert result.kept_summary() == "second"


def test_deleting_a_model_still_drops_its_own_materialised_file(shared_repo_cache) -> None:
    """Keeping the shared repo must not keep the deleted entry looking installed."""
    manager, _repo_dir, first, _second = shared_repo_cache

    manager.delete_model(first)

    assert not manager.is_model_cached(first)
    assert not next(iter(first.materialise_to.values())).exists()


def test_deleting_the_last_model_using_a_repo_removes_it(shared_repo_cache) -> None:
    manager, repo_dir, first, second = shared_repo_cache

    manager.delete_model(first)
    result = manager.delete_model(second)

    assert result.revisions_removed == 1
    assert result.kept_repos == {}
    assert not any(repo_dir.glob("snapshots/*/*"))


def test_a_sibling_that_is_not_installed_does_not_pin_the_repo(shared_repo_cache) -> None:
    """Listed in the catalogue is not the same as present on disk."""
    manager, repo_dir, first, second = shared_repo_cache
    next(iter(second.materialise_to.values())).unlink()
    assert not manager.is_model_cached(second)

    result = manager.delete_model(first)

    assert result.revisions_removed == 1
    assert result.kept_repos == {}
    assert not any(repo_dir.glob("snapshots/*/*"))


def test_a_materialised_file_two_entries_share_is_kept(tmp_path, monkeypatch) -> None:
    """Discovered models are built at run time, so two entries can name the same
    destination even though the shipped catalogue does not."""
    from deepreefmap_gui.models import cache as model_manager

    cache = tmp_path / "hf"
    monkeypatch.setattr(model_manager, "_HF_CACHE_ROOT", cache)
    _write_repo(cache, "fake/one", {"latest.pt": b"w"})
    _write_repo(cache, "fake/two", {"latest.pt": b"w"})
    dest = tmp_path / "ckpts" / "latest.pt"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"materialised")

    def _entry(name, repo):
        return ModelInfo(
            name=name, kind="mapping", hf_repos=[repo], gated=False,
            description="test", materialise_to={"latest.pt": dest},
        )

    first, second = _entry("first", "fake/one"), _entry("second", "fake/two")
    monkeypatch.setattr(model_manager, "all_known_models", lambda: [first, second])

    model_manager.delete_model(first)

    assert dest.exists()
    assert model_manager.is_model_cached(second)
