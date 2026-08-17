"""The workspace reset behind the toolbar "+" button."""

from __future__ import annotations

import logging

from deepreefmap_gui.core.window_protocol import MixinBase

logger = logging.getLogger(__name__)


class PastRunsMixin(MixinBase):
    """DeepReefMapWindow methods for clearing the workspace."""

    def _on_new_reconstruction(self) -> None:
        self._viewer._clear_scene_data()
        self._results_group.setVisible(False)
        self._results_empty.setVisible(True)
        self._viewer.legend_overlay.setVisible(False)
        # The display controls act on a loaded cloud, so they go with it.
        self._set_overlay_controls_visible(False)
        self._clear_run_facts()
        self._clear_run_warnings()
        self._active_run_dir = None
        self._active_run_manifest = None
        self._set_ortho_sources(None, None, None)
        self._status_label.setText("Workspace cleared.")
        self._set_app_mode("SETUP")
