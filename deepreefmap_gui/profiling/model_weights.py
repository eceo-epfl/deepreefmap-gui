"""How large a model's parameters are, read off the files on disk.

A checkpoint states its own size, so the weights term is measured rather than
tabled. It bounds both budgets: a checkpoint is materialised in host RAM before
any of it reaches the device, so the same figure drives the RAM terms.

No model load, no network, no torch import. Safetensors states its tensors in a
JSON header, and a torch ``.pt`` is a zip whose storage records carry their sizes.
"""

from __future__ import annotations

import json
import logging
import struct
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

# safetensors dtype codes to bytes per element.
_ITEMSIZE = {
    "F64": 8, "F32": 4, "F16": 2, "BF16": 2, "F8_E4M3": 1, "F8_E5M2": 1,
    "I64": 8, "I32": 4, "I16": 2, "I8": 1, "U8": 1, "BOOL": 1,
}

# A header larger than this is not a header. Guards against reading a corrupt or
# truncated file's length prefix as an allocation.
_MAX_HEADER_BYTES = 100 * 1024 * 1024

# Parsed sizes, keyed by the file's own identity. Called from _row_fit, which
# runs on every keystroke.
_SIZES: dict[tuple[str, int, int], int] = {}

# Answers per model name, so the walk that finds a repo's files runs once.
_RESOLVED: dict[str, int | None] = {}


def _stamp(path: Path) -> tuple[str, int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (str(path), stat.st_mtime_ns, stat.st_size)


def _safetensors_bytes(path: Path) -> int:
    """Sum every tensor the header declares, without reading the tensors."""
    with path.open("rb") as handle:
        prefix = handle.read(8)
        if len(prefix) < 8:
            return 0
        length = struct.unpack("<Q", prefix)[0]
        if not 0 < length <= _MAX_HEADER_BYTES:
            return 0
        header = json.loads(handle.read(length))
    total = 0
    for name, spec in header.items():
        if name == "__metadata__" or not isinstance(spec, dict):
            continue
        count = 1
        for dim in spec.get("shape") or []:
            count *= int(dim)
        total += count * _ITEMSIZE.get(spec.get("dtype", ""), 4)
    return total


def _torch_zip_bytes(path: Path) -> int:
    """Sum a torch archive's storage records, which are the tensor data."""
    with zipfile.ZipFile(path) as archive:
        return sum(
            info.file_size for info in archive.infolist() if "/data/" in info.filename
        )


def file_weights_bytes(path: Path) -> int:
    """Parameter bytes in one checkpoint file, or 0 if it cannot be read."""
    stamp = _stamp(path)
    if stamp is None:
        return 0
    cached = _SIZES.get(stamp)
    if cached is not None:
        return cached
    try:
        if path.suffix == ".safetensors":
            size = _safetensors_bytes(path)
        elif path.suffix in (".pt", ".pth", ".bin", ".ckpt"):
            size = _torch_zip_bytes(path)
        else:
            return 0
    except (OSError, ValueError, KeyError, struct.error, zipfile.BadZipFile):
        # A checkpoint that cannot be read is not a reason to refuse a grade:
        # the caller falls back to its table.
        logger.warning("Could not size the checkpoint at %s", path, exc_info=True)
        return 0
    _SIZES[stamp] = size
    return size


def _repo_weights_bytes(repo_id: str, *, only: list[str] | None = None) -> int:
    """Every checkpoint in a repo, or only the named ones.

    ``only`` matters where one repo serves several models: LoGeR and LoGeR* ship
    from a single repo with a checkpoint each, and summing the folder would
    charge either of them for both.
    """
    from deepreefmap_gui.models.cache import snapshot_dir

    root = snapshot_dir(repo_id)
    if root is None:
        return 0
    if only is not None:
        return sum(file_weights_bytes(Path(root) / name) for name in only)
    total = 0
    for pattern in ("*.safetensors", "*.pt", "*.pth", "*.bin", "*.ckpt"):
        for path in sorted(Path(root).rglob(pattern)):
            total += file_weights_bytes(path)
    return total


def forget_cached_sizes() -> None:
    """Drop what has been measured, after a download changes what is on disk."""
    _SIZES.clear()
    _RESOLVED.clear()


def weights_bytes(model_name: str) -> int | None:
    """Resident parameter bytes for an installed model, or None if it is not.

    None rather than zero, so a caller falls back to its table instead of
    grading the run as free.

    A DPT head is counted with its backbone: the head's repo ships only the
    head, and the backbone is fetched separately at first use.
    """
    from deepreefmap_gui.models.cache import DPT_BACKBONE_MAP, all_known_models

    if model_name in _RESOLVED:
        return _RESOLVED[model_name]
    info = next((m for m in all_known_models() if m.name == model_name), None)
    if info is None:
        return None
    repos = list(info.hf_repos)
    backbone = DPT_BACKBONE_MAP.get(model_name)
    if backbone and backbone not in repos:
        repos.append(backbone)
    # A model that names the files it uses is charged for those alone.
    named = sorted(info.materialise_to)
    total = 0
    for repo in repos:
        only = named if named and repo == info.hf_repos[0] else None
        total += _repo_weights_bytes(repo, only=only)
    answer = total or None
    _RESOLVED[model_name] = answer
    return answer
