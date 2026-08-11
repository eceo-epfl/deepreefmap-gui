"""Sizing a model from its own files, and what the memory model does with it.

Scenario: the tabled weights figures were hand-written, and for the DPT
segmenters they counted the head and missed the backbone that is fetched at
first use, understating the largest by over a gigabyte in the direction
model_costs.py calls an OOM kill rather than a recoverable warning.

Expected behaviour: a checkpoint states its own size, and that figure drives
host RAM as well as graphics memory, because a checkpoint is materialised in RAM
before any of it reaches the device.
"""

from __future__ import annotations

import json
import struct
import zipfile

import pytest

from deepreefmap_gui.profiling import model_weights
from deepreefmap_gui.profiling.model_costs import mapping_cost, segmentation_cost
from deepreefmap_gui.profiling.model_weights import file_weights_bytes, weights_bytes

MB = 1024**2


@pytest.fixture(autouse=True)
def _fresh_cache():
    model_weights.forget_cached_sizes()
    yield
    model_weights.forget_cached_sizes()


def write_safetensors(path, tensors: dict[str, tuple[str, list[int]]]):
    """A file with a real safetensors header and no tensor data behind it.

    The header states the sizes, so the payload is never read: sizing a 4.7 GB
    checkpoint costs a 40 KB read.
    """
    header = {
        name: {"dtype": dtype, "shape": shape, "data_offsets": [0, 0]}
        for name, (dtype, shape) in tensors.items()
    }
    blob = json.dumps(header).encode()
    path.write_bytes(struct.pack("<Q", len(blob)) + blob)
    return path


def test_a_safetensors_file_states_its_own_size(tmp_path):
    path = write_safetensors(
        tmp_path / "model.safetensors",
        {"a": ("F32", [1000, 1000]), "b": ("F16", [1000, 1000]), "c": ("I64", [10])},
    )
    # 1M floats at 4 bytes, 1M halves at 2, ten int64s at 8.
    assert file_weights_bytes(path) == 4_000_000 + 2_000_000 + 80


def test_a_torch_archive_is_sized_from_its_storage_records(tmp_path):
    path = tmp_path / "latest.pt"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("latest/data/0", b"x" * 4096)
        archive.writestr("latest/data/1", b"y" * 2048)
        archive.writestr("latest/data.pkl", b"not tensor data")
    assert file_weights_bytes(path) == 4096 + 2048


def test_an_unreadable_file_sizes_to_zero_rather_than_raising(tmp_path):
    """A checkpoint that cannot be read must not stop the run being graded."""
    bad = tmp_path / "model.safetensors"
    bad.write_bytes(b"nowhere near a header")
    assert file_weights_bytes(bad) == 0
    assert file_weights_bytes(tmp_path / "absent.safetensors") == 0


def test_an_uninstalled_model_answers_none_not_zero():
    """Zero would grade the run as free; None sends the caller to its table."""
    assert weights_bytes("no-such-model") is None


def test_a_dpt_head_is_counted_with_its_backbone(monkeypatch, tmp_path):
    """The head repo ships without the encoder AutoModel pulls in at first use."""
    head = tmp_path / "head"
    backbone = tmp_path / "backbone"
    head.mkdir()
    backbone.mkdir()
    write_safetensors(head / "model.safetensors", {"h": ("F32", [1000])})
    write_safetensors(backbone / "model.safetensors", {"b": ("F32", [3000])})

    roots = {"EPFL-ECEO/coralscapes-vit-s-dpt": head,
             "facebook/dinov3-vits16-pretrain-lvd1689m": backbone}
    monkeypatch.setattr(
        "deepreefmap_gui.models.cache.snapshot_dir", roots.get
    )

    assert weights_bytes("coralscapes-vit-s-dpt") == 4000 + 12000


def test_two_models_sharing_a_repo_are_not_charged_for_each_other(monkeypatch, tmp_path):
    """LoGeR and LoGeR* ship one checkpoint each from a single repo."""
    root = tmp_path / "loger"
    (root / "LoGeR").mkdir(parents=True)
    (root / "LoGeR_star").mkdir(parents=True)
    for name, size in (("LoGeR", 8192), ("LoGeR_star", 4096)):
        with zipfile.ZipFile(root / name / "latest.pt", "w") as archive:
            archive.writestr(f"{name}/data/0", b"z" * size)

    monkeypatch.setattr(
        "deepreefmap_gui.models.cache.snapshot_dir",
        lambda repo: root if repo == "Junyi42/LoGeR" else None,
    )

    assert weights_bytes("loger") == 8192
    assert weights_bytes("loger_star") == 4096


# --- What the memory model does with the figure ---


def test_measured_weights_move_host_ram_as_well_as_vram(monkeypatch):
    """A checkpoint is materialised in RAM before any of it reaches the device.

    Grading only the graphics card off the real figure would leave the machine's
    own memory sized from a number known to be wrong.
    """
    weight = [1000 * MB]
    monkeypatch.setattr(
        "deepreefmap_gui.profiling.model_weights.weights_bytes", lambda name: weight[0]
    )
    small = segmentation_cost("coralscapes-vit-l-dpt")
    weight[0] = 2000 * MB
    large = segmentation_cost("coralscapes-vit-l-dpt")

    assert small.weights_vram_bytes == 1000 * MB
    assert large.weights_vram_bytes == 2000 * MB
    # The load ratio the table encodes is kept; only its base is replaced, so
    # host RAM follows the real weights instead of a hand-written figure.
    assert large.load_ram_bytes == 2 * small.load_ram_bytes
    assert small.load_ram_bytes > small.weights_vram_bytes


def test_a_mapping_backend_keeps_its_activations_over_the_real_weights(monkeypatch):
    tabled = mapping_cost("loger_star")
    activations = tabled.vram_fixed_bytes - tabled.weights_ram_bytes
    monkeypatch.setattr(
        "deepreefmap_gui.profiling.model_weights.weights_bytes", lambda name: 1000 * MB
    )
    measured = mapping_cost("loger_star")

    assert measured.weights_ram_bytes == 1000 * MB
    assert measured.vram_fixed_bytes == 1000 * MB + activations


def test_an_unknown_backend_is_costed_at_the_measured_fallback(monkeypatch):
    """Overstating is recoverable; understating is an OOM kill, measured or not."""
    monkeypatch.setattr(
        "deepreefmap_gui.profiling.model_weights.weights_bytes", lambda name: 1234 * MB
    )
    assert mapping_cost("whatever") == mapping_cost("loger_star")
    assert segmentation_cost("whatever") == segmentation_cost("coralscapes-vit-l-dpt")


def test_a_missing_model_leaves_the_table_alone(monkeypatch):
    monkeypatch.setattr(
        "deepreefmap_gui.profiling.model_weights.weights_bytes", lambda name: None
    )
    from deepreefmap_gui.profiling.model_costs import _MAPPING_BY_KEY

    assert mapping_cost("loger") == _MAPPING_BY_KEY["loger"]
