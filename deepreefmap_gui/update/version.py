"""Updates UI: release check worker, Updates-tab controls, desktop entry toggle."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtGui import QColor

from deepreefmap_gui.core.window_protocol import MixinBase
from deepreefmap_gui.core.theme import UPDATE
from deepreefmap_gui.packaging.releases import (
    current_version,
    fetch_releases,
    newer_releases,
    parse_version,
    pyapp_binary_path,
    release_version,
    selectable_releases,
)

logger = logging.getLogger(__name__)

# Amber accent used to flag the Updates tab when a newer release exists.
_UPDATE_ACCENT = QColor(UPDATE)


class VersionCheckMixin(MixinBase):
    """DeepReefMapWindow methods for checking GitHub releases and installing updates."""

    def _check_for_update(self) -> None:
        current = current_version()
        releases = fetch_releases()
        pyapp_bin = pyapp_binary_path()
        self._sig_update_check_done.emit(current, releases, pyapp_bin)

    def _set_updates_tab_alert(self, latest: str | None) -> None:
        """Flag the System tab (which hosts updates) amber when `latest` is available.

        Passing None clears the alert and restores the default tab style.
        """
        bar = self._sidebar_tabs.tabBar()
        idx = self._TAB_SYSTEM
        if latest is None:
            bar.setTabText(idx, "System")
            bar.setTabTextColor(idx, QColor())  # invalid color falls back to theme default
            self._sidebar_tabs.setTabToolTip(idx, "")
            return
        bar.setTabText(idx, "System ●")
        bar.setTabTextColor(idx, _UPDATE_ACCENT)
        self._sidebar_tabs.setTabToolTip(idx, f"Version {latest} is available")

    def _apply_update_check(self, current: str, releases: list[dict] | None, pyapp_bin: str | None) -> None:
        self._current_version_str = current
        self._update_version_label.setText(f"Version: <b>{current}</b>")
        self._set_updates_tab_alert(None)
        self._update_show_all.setVisible(False)
        self._update_version_combo.setVisible(False)
        self._update_btn.setVisible(False)
        self._available_releases = list(releases or [])

        # Surface a newer release in the tab regardless of mode, as a nudge.
        newer = newer_releases(self._available_releases, current)
        if newer:
            self._set_updates_tab_alert(release_version(newer[0]))

        # Dev mode: running from source, not the installed binary. In-app
        # install/rollback swap the binary in place, which only makes sense for
        # the installed application, so the controls stay hidden here.
        if pyapp_bin is None:
            self._update_status_label.setText(
                "Running development mode. Launch from a binary to manage versions."
            )
            return

        if releases is None:
            self._update_status_label.setText("Couldn't reach GitHub.")
            return
        if not releases:
            self._update_status_label.setText("No releases found.")
            return
        # Installed binary: a rollback is only meaningful if there is any version
        # other than the current one.
        self._update_show_all.setVisible(
            any(release_version(r) != current for r in releases)
        )
        self._populate_update_versions()

    def _locally_kept_versions(self) -> set[str]:
        """Versions whose binary is on disk, so rollback needs no download."""
        from deepreefmap_gui.packaging.binary_swap import available_previous_versions

        pyapp_bin = pyapp_binary_path()
        if pyapp_bin is None:
            return set()
        try:
            return set(available_previous_versions(pyapp_bin))
        except OSError:
            return set()

    def _populate_update_versions(self) -> None:
        current = self._current_version_str
        include_older = self._update_show_all.isChecked()
        selectable = selectable_releases(self._available_releases, current, include_older)
        current_v = parse_version(current)
        kept = self._locally_kept_versions()
        self._update_version_combo.clear()
        for rel in selectable:
            version = release_version(rel)
            rv = parse_version(version)
            marker = ""
            if current_v is not None and rv is not None:
                marker = " ↑" if rv > current_v else " ↓"
            # A kept binary rolls back with no download.
            kept_marker = " (kept)" if version in kept else ""
            self._update_version_combo.addItem(f"{version}{marker}{kept_marker}", rel)
        has_items = self._update_version_combo.count() > 0
        self._update_version_combo.setVisible(has_items)
        self._update_btn.setVisible(has_items)
        if not has_items:
            self._update_status_label.setText("Up to date.")
        elif include_older:
            self._update_status_label.setText("Pick a version to install or roll back to:")
        else:
            self._update_status_label.setText(
                f"Latest: <b>{release_version(selectable[0])}</b>. Pick a version to install:"
            )

    def _on_toggle_show_all_versions(self, _checked: bool) -> None:
        if self._available_releases:
            self._populate_update_versions()

    # --- Storage manager (System tab) ----------------------------------------
    # Environments are never pruned automatically. This is where the user sees
    # each version's real (hardlink-aware) footprint and deletes the ones they no
    # longer want. Models are shown too, but managed on the Models tab.

    @staticmethod
    def _format_size(num_bytes: int) -> str:
        gb = num_bytes / 1024**3
        if gb >= 1:
            return f"{gb:.1f} GB"
        return f"{num_bytes // 1024**2} MB"

    def _refresh_storage(self) -> None:
        import threading

        threading.Thread(target=self._measure_storage, daemon=True).start()

    def _measure_storage(self) -> None:
        """Off-thread: each version env's real size, plus the model-cache total."""
        from deepreefmap_gui.packaging.environments import env_disk_usage, list_environments

        info: dict = {"environments": [], "model_bytes": None}
        try:
            for env in list_environments():
                reclaimable, apparent = env_disk_usage(env.path)
                info["environments"].append(
                    {
                        "version": env.version,
                        "path": str(env.path),
                        "current": env.current,
                        "reclaimable": reclaimable,
                        "apparent": apparent,
                    }
                )
        except OSError:
            logger.debug("Environment scan failed", exc_info=True)
        try:
            from huggingface_hub import scan_cache_dir

            from deepreefmap_gui.models.manager import hf_cache_root

            info["model_bytes"] = scan_cache_dir(cache_dir=hf_cache_root()).size_on_disk
        except Exception:
            logger.debug("Model cache scan failed", exc_info=True)
        self._sig_storage_done.emit(info)

    def _apply_storage(self, info: dict) -> None:
        from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

        layout = self._env_list_layout
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()

        environments = info.get("environments", [])
        if not environments:
            layout.addWidget(QLabel("No installed environments found."))
        for env in environments:
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            real = self._format_size(env["reclaimable"])
            shared = self._format_size(max(env["apparent"] - env["reclaimable"], 0))
            running = " (running)" if env["current"] else ""
            label = QLabel(f"<b>{env['version']}</b>{running} — {real} on disk, {shared} shared with the cache")
            label.setWordWrap(True)
            row_layout.addWidget(label, 1)
            if not env["current"]:
                delete_btn = QPushButton("Delete")
                delete_btn.clicked.connect(
                    lambda _c=False, p=env["path"], v=env["version"]: self._on_delete_environment(p, v)
                )
                row_layout.addWidget(delete_btn)
            layout.addWidget(row)

        model_bytes = info.get("model_bytes")
        if model_bytes is None:
            self._model_cache_label.setText("Downloaded models: size unavailable.")
        else:
            self._model_cache_label.setText(
                f"Downloaded models: <b>{self._format_size(model_bytes)}</b> "
                "(manage on the Models tab)"
            )

    def _on_delete_environment(self, path: str, version: str) -> None:
        from PySide6.QtWidgets import QMessageBox

        from deepreefmap_gui.packaging.environments import delete_environment

        confirm = QMessageBox.question(
            self,
            "Delete environment",
            f"Delete the environment for version {version}?\n\n"
            "Its unique files are freed now. If you install or roll back to this "
            "version later, it is rebuilt from the package cache (no large download "
            "when the cache is warm).",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            delete_environment(path)
        except (OSError, ValueError):
            logger.exception("Failed to delete environment %s", path)
        self._refresh_storage()

    def _go_to_models_tab(self) -> None:
        self._sidebar_tabs.setCurrentIndex(self._TAB_MODELS)

    def _refresh_desktop_entry_button(self) -> None:
        from deepreefmap_gui.packaging.desktop_entry import desktop_entry_installed

        if desktop_entry_installed():
            self._desktop_entry_btn.setText("Remove from applications menu")
        else:
            self._desktop_entry_btn.setText("Add to applications menu")

    def _on_toggle_desktop_entry(self) -> None:
        from deepreefmap_gui.packaging.desktop_entry import (
            desktop_entry_installed,
            install_desktop_entry,
            remove_desktop_entry,
        )

        try:
            if desktop_entry_installed():
                remove_desktop_entry()
            else:
                pyapp_bin = pyapp_binary_path()
                if pyapp_bin is None:
                    return
                install_desktop_entry(pyapp_bin)
        except OSError:
            logger.exception("Desktop entry update failed")
        self._refresh_desktop_entry_button()

    def _on_update(self) -> None:
        from deepreefmap_gui.update.dialog import UpdateProgressDialog

        pyapp_bin = pyapp_binary_path()
        if pyapp_bin is None:
            logger.warning("Install clicked but no PyApp binary detected")
            return
        index = self._update_version_combo.currentIndex()
        if index < 0:
            return
        release = self._update_version_combo.itemData(index)
        if not isinstance(release, dict):
            logger.warning("Selected release has no metadata")
            return
        version = release_version(release)
        # A kept binary older than the current one rolls back offline; anything
        # else downloads. parse_version guards against a rollback attempt on a
        # version whose binary is not actually on disk.
        rollback = version in self._locally_kept_versions()
        self._update_btn.setEnabled(False)
        try:
            dialog = UpdateProgressDialog(
                target_version=version,
                release=release,
                binary_path=Path(pyapp_bin),
                current_version=self._current_version_str,
                rollback=rollback,
                parent=self,
            )
            dialog.run()
        finally:
            self._update_btn.setEnabled(True)
