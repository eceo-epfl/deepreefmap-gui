"""Models tab: reveal the cache folder and export/import portable model packs.

Kept apart from management.py (download/delete/auth) so each mixin stays focused. All
heavy work runs on a daemon thread and marshals back through the _sig_pack_* signals,
matching the download/QC-render pattern elsewhere in the window.
"""

from __future__ import annotations

from deepreefmap_gui.core.window_protocol import MixinBase

import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QLabel,
    QProgressDialog,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from deepreefmap_gui.models.manager import ModelInfo

logger = logging.getLogger(__name__)


def _size_hint(approx_size_mb: int | None) -> str:
    if not approx_size_mb:
        return ""
    if approx_size_mb >= 1024:
        return f"~{approx_size_mb / 1024:.1f} GB"
    return f"~{approx_size_mb} MB"


class ModelSelectDialog(QDialog):
    """Checkbox list of cached models to include in an export pack."""

    def __init__(self, models: list[ModelInfo], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export models")
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Choose the models to include in the pack:"))
        self._checks: dict[str, QCheckBox] = {}
        for info in models:
            size = _size_hint(info.approx_size_mb)
            cb = QCheckBox(f"{info.name}    {size}".rstrip())
            cb.setChecked(True)
            self._checks[info.name] = cb
            layout.addWidget(cb)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_names(self) -> set[str]:
        return {name for name, cb in self._checks.items() if cb.isChecked()}


class ModelLibraryMixin(MixinBase):
    """DeepReefMapWindow methods for opening the cache and packing/unpacking models."""

    def _open_model_library(self) -> None:
        from deepreefmap_gui.models.manager import hf_cache_root

        root = hf_cache_root()
        root.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(root)))

    def _start_pack_progress(self, title: str) -> None:
        progress = QProgressDialog(title, "Cancel", 0, 100, self)
        progress.setWindowModality(Qt.WindowModality.ApplicationModal)
        progress.setAutoClose(True)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        self._pack_progress_dialog = progress

    def _on_export_models(self) -> None:
        if self._downloading:
            self._status_label.setText("Wait for downloads to finish before exporting.")
            return
        cached = [info for info, is_cached in self._last_model_states if is_cached]
        if not cached:
            self._status_label.setText("No downloaded models to export yet.")
            return

        dlg = ModelSelectDialog(cached, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        chosen = dlg.selected_names()
        models = [info for info in cached if info.name in chosen]
        if not models:
            return

        dest = QFileDialog.getExistingDirectory(self, "Choose a folder for the model pack")
        if not dest:
            return

        self._status_label.setText("Exporting model pack…")
        self._start_pack_progress("Exporting models…")

        def _worker() -> None:
            from deepreefmap_gui.models import library

            try:
                pack = library.export_model_pack(
                    models,
                    dest,
                    progress_cb=self._sig_pack_progress.emit,
                )
                self._sig_pack_done.emit(True, f"Exported model pack to {pack}")
            except Exception as exc:
                logger.exception("Model pack export failed")
                self._sig_pack_done.emit(False, f"Export failed: {str(exc)[:200]}")

        threading.Thread(target=_worker, daemon=True).start()

    def _on_import_model_pack(self) -> None:
        from deepreefmap_gui.models import library

        if self._downloading:
            self._status_label.setText("Wait for downloads to finish before importing.")
            return

        chosen = QFileDialog.getExistingDirectory(self, "Select a model pack folder")
        if not chosen:
            return
        pack_dir = Path(chosen)
        # Accept either the pack folder itself or a parent that contains it.
        if not library.is_model_pack(pack_dir):
            candidate = pack_dir / library.PACK_DIR_NAME
            if library.is_model_pack(candidate):
                pack_dir = candidate
            else:
                self._status_label.setText(
                    "That folder is not a DeepReefMap model pack (no models.tar found)."
                )
                return

        self._status_label.setText("Importing model pack…")
        self._start_pack_progress("Importing model pack…")

        def _worker() -> None:
            try:
                result = library.import_model_pack(
                    pack_dir,
                    progress_cb=self._sig_pack_progress.emit,
                )
                parts: list[str] = []
                if result.imported:
                    parts.append(f"{len(result.imported)} imported")
                if result.already_present:
                    parts.append(f"{len(result.already_present)} already present")
                detail = f" ({', '.join(parts)})" if parts else ""
                self._sig_pack_done.emit(True, f"Imported model pack{detail}")
            except Exception as exc:
                logger.exception("Model pack import failed")
                self._sig_pack_done.emit(False, f"Import failed: {str(exc)[:200]}")

        threading.Thread(target=_worker, daemon=True).start()

    def _on_pack_progress(self, phase: str, current: int, total: int) -> None:
        dlg = getattr(self, "_pack_progress_dialog", None)
        if dlg is None or dlg.wasCanceled():
            return
        total = max(total, 1)
        verb = {"export": "Exporting", "verify": "Verifying"}.get(phase, "Importing")
        dlg.setLabelText(
            f"{verb} models… {current / 1024**2:.0f} / {total / 1024**2:.0f} MB"
        )
        dlg.setMaximum(total)
        dlg.setValue(min(current, total))

    def _on_pack_done(self, ok: bool, message: str) -> None:
        dlg = getattr(self, "_pack_progress_dialog", None)
        if dlg is not None:
            dlg.close()
            self._pack_progress_dialog = None
        self._status_label.setText(message)
        if ok:
            # Reflow the model rows so freshly imported models show as cached.
            threading.Thread(target=self._refresh_model_status, daemon=True).start()
