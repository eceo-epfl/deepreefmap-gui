"""Updates UI: release check worker, Updates-tab controls, desktop entry toggle."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtGui import QColor

from deepreefmap_gui.core.theme import UPDATE
from deepreefmap_gui.core.widgets import confirm
from deepreefmap_gui.core.window_protocol import MixinBase
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

    # The newest release worth offering, or "" when this is already it. Read by
    # the Setup button, which paints its own slot for it.
    _update_available: str = ""

    def _check_for_update(self) -> None:
        current = current_version()
        releases = fetch_releases()
        pyapp_bin = pyapp_binary_path()
        self._sig_update_check_done.emit(current, releases, pyapp_bin)

    def _set_updates_tab_alert(self, latest: str | None) -> None:
        """Say a release is waiting, wherever the running interface can show it.

        The Setup button carries a slot for it, so a release waiting is
        visible without opening anything. Passing None clears it.
        """
        self._update_available = latest or ""
        self._refresh_machine_button()

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

    def _populate_update_versions(self) -> None:
        current = self._current_version_str
        include_older = self._update_show_all.isChecked()
        selectable = selectable_releases(self._available_releases, current, include_older)
        current_v = parse_version(current)
        self._update_version_combo.clear()
        for rel in selectable:
            version = release_version(rel)
            rv = parse_version(version)
            marker = ""
            if current_v is not None and rv is not None:
                marker = " ↑" if rv > current_v else " ↓"
            self._update_version_combo.addItem(f"{version}{marker}", rel)
        has_items = self._update_version_combo.count() > 0
        self._update_version_combo.setVisible(has_items)
        self._update_btn.setVisible(has_items)
        if not has_items:
            self._update_status_label.setText("Up to date.")
        elif include_older:
            self._update_status_label.setText(
                "Pick a version to install or roll back to. Rolling back may make "
                "surveys created by this version unreadable until you upgrade again:"
            )
        else:
            self._update_status_label.setText(
                f"Latest: <b>{release_version(selectable[0])}</b>. Pick a version to install:"
            )

    def _on_toggle_show_all_versions(self, _checked: bool) -> None:
        if self._available_releases:
            self._populate_update_versions()

    def _confirm_downgrade(self, target: str) -> bool:
        """Warn before rolling back, and take a backup that makes it reversible.

        Going back is not symmetric with going forward. Migrations only run one
        way, so an older build refuses a survey database a newer one has already
        migrated. The backup written here is what its recovery dialog offers to
        restore, and it is taken before anything is swapped -- a warning that
        only warns would leave the user to find out afterwards.
        """
        current_v = parse_version(self._current_version_str)
        target_v = parse_version(target)
        if current_v is None or target_v is None or target_v >= current_v:
            return True

        from deepreefmap_gui.survey.backup import write_backup
        from deepreefmap_gui.survey.health import inspect_survey_db
        from deepreefmap_gui.survey.store import SURVEY_DB_NAME

        db_path = Path(self._out_root_input.text()).expanduser() / SURVEY_DB_NAME
        health = inspect_survey_db(db_path)
        saved = (
            write_backup(db_path, health.db_version)
            if health.db_version is not None
            else None
        )
        where = (
            f"A copy of this survey has been saved as {saved.name}, which "
            f"version {target} can restore."
            if saved is not None
            else "No survey database was found in the current output folder to copy."
        )
        return confirm(
            self,
            "Roll back to an older version",
            f"Version {target} is older than the version running now "
            f"({self._current_version_str}).\n\n"
            f"Surveys created or opened by this version may use a database "
            f"format {target} cannot read. If that happens it will offer to "
            f"restore a backup or rebuild from your run folders.\n\n"
            f"{where}\n\nRoll back to {target}?",
        )

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
        if not self._confirm_downgrade(version):
            return
        self._update_btn.setEnabled(False)
        try:
            dialog = UpdateProgressDialog(
                target_version=version,
                release=release,
                binary_path=Path(pyapp_bin),
                parent=self,
            )
            dialog.run()
        finally:
            self._update_btn.setEnabled(True)
