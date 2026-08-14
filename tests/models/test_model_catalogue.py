"""The GUI's answers about segmentation models must match the library's.

The run form asks this package rather than `deepreefmap.segmentation.registry`,
because that module imports torch at module scope and the form is built before
the window is on screen. Two sources of the same fact only stay one fact if
something compares them.
"""

from __future__ import annotations

import pytest

from deepreefmap_gui.models.cache import segmentation_model_names
from deepreefmap_gui.models.families import model_processing_size

torch = pytest.importorskip("torch")


def test_segmentation_names_match_the_library() -> None:
    from deepreefmap.segmentation.registry import list_segmentation_models

    assert segmentation_model_names() == list_segmentation_models()


def test_processing_size_matches_the_library_for_every_model() -> None:
    from deepreefmap.segmentation.registry import (
        list_segmentation_models,
    )
    from deepreefmap.segmentation.registry import (
        model_processing_size as library_size,
    )

    for name in list_segmentation_models():
        assert model_processing_size(name) == library_size(name), name


def test_unknown_model_has_no_processing_size() -> None:
    assert model_processing_size("not-a-model") is None


def test_discovered_models_reach_the_dropdown() -> None:
    """Discovery registers into both catalogues; the form reads ours."""
    from deepreefmap_gui.models.cache import _DISCOVERED_MODELS, ModelInfo, register_discovered

    info = ModelInfo(
        name="coralscapes-vit-x-dpt",
        kind="segmentation",
        hf_repos=["EPFL-ECEO/coralscapes-vit-x-dpt"],
        gated=True,
        description="",
        approx_size_mb=None,
    )
    assert register_discovered(info)
    try:
        assert "coralscapes-vit-x-dpt" in segmentation_model_names()
    finally:
        _DISCOVERED_MODELS.remove(info)
