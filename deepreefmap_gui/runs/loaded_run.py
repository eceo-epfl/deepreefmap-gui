"""GUI-side cached-run loading with scene-file fast path.

Wraps the library's ``load_cached_run`` (which no longer knows about scene files)
with the GUI's zarr scene-file cache: a completed run loads from an up-to-date
scene file when present, otherwise takes the library slow path and regenerates
the scene file in the background for next time.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from deepreefmap.config.classes import ClassConfig
from deepreefmap.pipeline.artifacts import FrameBatch, MappingSequenceResult, SemanticPointCloud
from deepreefmap.pipeline.run_loader import (
    SEMANTIC_MODE,
    _world_points_fallback_warning,
    load_cached_run,
)

from deepreefmap_gui.io.scene_file import (
    SCENE_FILE_SUFFIX,
    LazyFrameBatch,
    SceneFrameAccessor,
    find_scene_file,
    load_scene_file,
    save_scene_file,
    scene_file_name,
)

if TYPE_CHECKING:
    from deepreefmap.pointcloud.filters import PointFilterConfig
    from deepreefmap.pointcloud.final_cloud_index import FinalCloudIndex

logger = logging.getLogger(__name__)


@dataclass
class GuiLoadedRun:
    """A loaded run plus the GUI-only scene-file fields the viewer wires from."""

    run_dir: Path
    manifest: dict[str, Any]
    classes_config: ClassConfig
    frame_batch: FrameBatch | LazyFrameBatch
    mapping_result: MappingSequenceResult
    output_files: list[str]
    mode: str = SEMANTIC_MODE
    reference_cloud: SemanticPointCloud = field(default_factory=SemanticPointCloud.empty)
    geometry_xyz: np.ndarray | None = None
    geometry_rgb: np.ndarray | None = None
    world_points_warning: str | None = None
    from_scene_file: bool = False
    scene_accessor: SceneFrameAccessor | None = None
    final_cloud_index: FinalCloudIndex | None = None


def load_run(
    run_dir: Path,
    *,
    point_filter_config: PointFilterConfig | None = None,
) -> GuiLoadedRun:
    """Load a completed reconstruction folder.

    Prefers an up-to-date scene file, otherwise takes the library slow path and
    regenerates the scene file in the background for next time.
    """
    run_dir = Path(run_dir)

    # --- Fast path: try scene file ---
    scene_path = find_scene_file(run_dir)
    if scene_path is not None:
        try:
            loaded = _load_from_scene_file(scene_path, run_dir)
            if loaded is not None:
                logger.info("Loaded from scene file (fast path): %s", scene_path)
                return loaded
            logger.info("Scene file stale or incompatible, falling back to slow path")
        except Exception:
            logger.warning("Scene file load failed, falling back to slow path", exc_info=True)

    # Clean up stale .tmp files from interrupted background generation
    for tmp in run_dir.glob("*" + SCENE_FILE_SUFFIX + ".tmp"):
        try:
            tmp.unlink()
            logger.info("Cleaned up stale temp file: %s", tmp)
        except OSError:
            pass

    # --- Slow path (library loader) ---
    result = _wrap_loaded_run(load_cached_run(run_dir, point_filter_config=point_filter_config))

    # Generate scene file in background for next time
    if result.mode == SEMANTIC_MODE and len(result.reference_cloud) > 0:
        _generate_scene_file_async(run_dir, result)

    return result


def _load_from_scene_file(scene_path: Path, run_dir: Path) -> GuiLoadedRun | None:
    scene = load_scene_file(scene_path, run_dir=run_dir)
    if scene is None:
        return None

    fb = LazyFrameBatch(scene.frame_accessor, scene.mapping_result.intrinsics)
    output_files = scene.manifest.get("output_files", [])

    return GuiLoadedRun(
        run_dir=run_dir,
        manifest=scene.manifest,
        classes_config=scene.classes_config,
        frame_batch=fb,
        mapping_result=scene.mapping_result,
        output_files=output_files,
        mode=scene.run_mode,
        from_scene_file=True,
        scene_accessor=scene.frame_accessor,
        final_cloud_index=scene.final_cloud_index,
        world_points_warning=_world_points_fallback_warning(scene.manifest, scene.mapping_result),
    )


def _wrap_loaded_run(loaded) -> GuiLoadedRun:
    """Re-express a library ``LoadedRun`` as a ``GuiLoadedRun`` (scene fields default)."""
    return GuiLoadedRun(
        run_dir=loaded.run_dir,
        manifest=loaded.manifest,
        classes_config=loaded.classes_config,
        frame_batch=loaded.frame_batch,
        mapping_result=loaded.mapping_result,
        output_files=loaded.output_files,
        mode=loaded.mode,
        reference_cloud=loaded.reference_cloud,
        geometry_xyz=loaded.geometry_xyz,
        geometry_rgb=loaded.geometry_rgb,
        world_points_warning=loaded.world_points_warning,
    )


def _generate_scene_file_async(run_dir: Path, result: GuiLoadedRun) -> None:
    """Build and save a scene file on a daemon thread so the next load is fast."""

    def _worker() -> None:
        try:
            from deepreefmap.pointcloud.final_cloud_index import build_final_cloud_index

            frame_order = [int(f.frame_index) for f in result.frame_batch.frames]
            class_colors = result.classes_config.id_to_color
            fci = build_final_cloud_index(result.reference_cloud, frame_order, class_colors)

            fname = scene_file_name(result.manifest, run_dir)
            out = run_dir / fname
            save_scene_file(
                out,
                manifest=result.manifest,
                classes_config=result.classes_config,
                mapping_result=result.mapping_result,
                frame_batch=result.frame_batch,  # type: ignore[arg-type]  # LazyFrameBatch is interface-compatible but not a FrameBatch subclass
                final_cloud_index=fci,
                run_dir=run_dir,
            )
            logger.info("Scene file generated for next load: %s", out)
        except Exception:
            logger.warning("Background scene file generation failed", exc_info=True)

    threading.Thread(target=_worker, daemon=True).start()
