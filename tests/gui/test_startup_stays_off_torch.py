"""Nothing on the way to the first frame may import torch.

Importing torch costs half a second of GIL-holding bytecode and then pulls the
GPU driver in behind it -- 3.3 s on the ROCm build, because HIP initialises
before it will say how many cards there are. A worker thread is no escape: it
holds the interpreter lock through the import just as the GUI thread would, and
the window comes up as a frozen rectangle for the duration.

Two things have already done this and would again:
  - form/panel.py reaching for deepreefmap.segmentation.registry, which imports
    torch at module scope, to list five model names;
  - packaging/binary_swap.py importing torch to read torch.version.hip, on the
    update-check thread, seconds into startup.

Both now answer from data. This test is what keeps the third one out.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

pytest.importorskip("torch")


def _torch_loaded_after(source: str) -> bool:
    """Run source in a bare interpreter; True if torch ended up imported.

    A child process, because the answer is about this process's sys.modules and
    the test session has torch loaded long before it starts.
    """
    proc = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source)],
        capture_output=True,
        text=True,
        timeout=300,
        # Asserted below instead, so a failure shows the child's stderr rather
        # than a CalledProcessError that says only that it exited non-zero.
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    return proc.stdout.strip().endswith("TORCH=True")


def test_building_the_window_does_not_import_torch() -> None:
    assert not _torch_loaded_after(
        """
        import os, sys
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        from deepreefmap.config.classes import load_classes
        import deepreefmap_gui.app as app_mod

        qt_app = QApplication([])
        app_mod.DeepReefMapWindow(load_classes(None), None)
        print("TORCH=%s" % ("torch" in sys.modules))
        """
    )


def test_resolving_the_update_asset_does_not_import_torch() -> None:
    """Runs on a worker thread a few hundred ms in, while the window is painting."""
    assert not _torch_loaded_after(
        """
        import sys
        from deepreefmap_gui.packaging.binary_swap import resolve_asset_name
        resolve_asset_name("linux")
        resolve_asset_name("win32")
        print("TORCH=%s" % ("torch" in sys.modules))
        """
    )


def test_the_run_form_lists_models_without_importing_torch() -> None:
    assert not _torch_loaded_after(
        """
        import sys
        from deepreefmap_gui.models.cache import segmentation_model_names
        from deepreefmap_gui.models.families import model_processing_size

        assert segmentation_model_names()
        assert model_processing_size("segformer-b2")
        print("TORCH=%s" % ("torch" in sys.modules))
        """
    )


def test_grading_this_machine_does_not_import_torch() -> None:
    """probe_system(wait_for_gpu=False) is what every repaint calls."""
    assert not _torch_loaded_after(
        """
        import sys
        from deepreefmap_gui.profiling.system_probe import (
            gpu_present, probe_system, sample_utilisation,
        )

        probe_system(wait_for_gpu=False)
        sample_utilisation()
        gpu_present(wait=False)
        print("TORCH=%s" % ("torch" in sys.modules))
        """
    )
