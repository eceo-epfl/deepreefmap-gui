"""Resolve the classes YAML the app should hand the library.

The library's default is a repo-relative path, and its cache key reads the file
directly, so an installed app whose working directory is anywhere else must pass
an absolute path. `None` (no custom classes chosen) resolves to the library's
bundled copy.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path


def resolve_classes_path(classes_path: Path | None) -> Path:
    if classes_path is not None:
        return classes_path
    return Path(str(resources.files("deepreefmap.resources").joinpath("configs/classes_coralscapes.yaml")))
