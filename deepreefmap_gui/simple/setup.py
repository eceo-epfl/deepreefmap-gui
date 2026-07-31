"""First-run environment check, shown before planning.

Three rows say whether this machine can process a dive: the graphics card, the
models it needs, and disk space. Each failing row carries the one action that
fixes it. When all three pass the step stops leading on launch.

The verdict functions are pure and Qt-free so the pass/fail logic is tested
without a window, the same split progress.py uses for the step badges.
"""

from __future__ import annotations

import json
import logging
import statistics
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.core.theme import ERROR, GUTTER, SUCCESS, TEXT_MUTED
from deepreefmap_gui.core.widgets import section_card
from deepreefmap_gui.core.window_protocol import MixinBase
from deepreefmap_gui.models.manager import GPU_ONLY_BACKENDS
from deepreefmap_gui.profiling.system_probe import GPU_NONE, format_bytes, probe_system

logger = logging.getLogger(__name__)

# Fallback per-pass size, used only until this machine has processed a run of its
# own. Nothing measured it: a pass leaves frame caches, a point cloud and a
# manifest behind, and the total depends on clip length, fps and resolution. It
# is deliberately generous, because warning early beats filling the disk halfway
# through a batch nobody is watching. measure_bytes_per_footage_minute replaces
# it with a figure from real output as soon as there is one.
ROUGH_PASS_BYTES = 3 * 1024**3

# Sizing a run means walking every cached frame in it, and this runs whenever the
# setup page repaints, so only the most recent runs are measured.
_SIZED_RUNS = 5


def measure_bytes_per_footage_minute(out_root: Path, limit: int = _SIZED_RUNS) -> float | None:
    """Output bytes per minute of footage, measured from recent runs.

    None until this machine has processed something, which is the honest answer
    before then: per-pass size varies with clip length, fps and resolution, so
    there is nothing to extrapolate from. Manifests carry the frame count and the
    rate they were sampled at, which is the footage duration the run consumed.
    """
    from deepreefmap_gui.survey.catalogue import dir_size_bytes

    try:
        manifests = sorted(
            out_root.glob("*/run_manifest.json"), key=lambda p: p.stat().st_mtime, reverse=True
        )
    except OSError:
        return None
    rates: list[float] = []
    for manifest_path in manifests[:limit]:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            frames = int(manifest.get("frames_processed") or 0)
            fps = float(manifest.get("fps") or 0)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            continue
        if frames <= 0 or fps <= 0:
            continue
        size = dir_size_bytes(manifest_path.parent)
        if size <= 0:
            continue
        rates.append(size / (frames / fps / 60))
    return statistics.median(rates) if rates else None


@dataclass(frozen=True)
class SetupCheck:
    """One setup row: whether it passes, its heading, and a plain-language line."""

    key: str
    ok: bool
    title: str
    detail: str


def graphics_check(*, gpu_name: str | None, requires_gpu: bool) -> SetupCheck:
    """Graphics card row. Passes unless the chosen method needs a card and none exists."""
    if gpu_name is not None:
        return SetupCheck("graphics", True, "Graphics card", gpu_name)
    if requires_gpu:
        return SetupCheck(
            "graphics",
            False,
            "Graphics card",
            "None detected. The selected processing method requires one; "
            "select the standard method in settings.",
        )
    return SetupCheck(
        "graphics",
        True,
        "Graphics card",
        "None detected. Processing will run on the CPU, which is slower.",
    )


def models_check(missing_models: list[str]) -> SetupCheck:
    """Models row. Passes when nothing the current settings need is absent."""
    if not missing_models:
        return SetupCheck("models", True, "Models", "All models required by these settings are installed.")
    names = ", ".join(missing_models)
    count = len(missing_models)
    noun = "model" if count == 1 else "models"
    return SetupCheck(
        "models",
        False,
        "Models",
        f"{count} required {noun} not installed ({names}). Import from a USB drive, or download.",
    )


def _coarse_footage(minutes: float) -> str:
    """Round a capacity to the precision it actually carries.

    Extrapolated from a handful of runs, so a figure like "157h 51m" would claim
    an accuracy the estimate does not have.
    """
    if minutes < 90:
        return f"{max(5, round(minutes / 5) * 5)} minutes"
    hours = minutes / 60
    if hours < 10:
        return f"{hours:.0f} hours"
    return f"{round(hours / 10) * 10} hours"


def space_check(
    free_bytes: int, min_free_bytes: int, bytes_per_footage_minute: float | None = None
) -> SetupCheck:
    """Disk space row, sized in footage where this machine has runs to measure."""
    free = format_bytes(free_bytes)
    if free_bytes < min_free_bytes:
        return SetupCheck(
            "space",
            False,
            "Disk space",
            f"{free} free, below the {format_bytes(min_free_bytes)} required. "
            "Delete old surveys to make room.",
        )
    if bytes_per_footage_minute:
        capacity = _coarse_footage(free_bytes / bytes_per_footage_minute)
        return SetupCheck(
            "space",
            True,
            "Disk space",
            f"{free} free, about {capacity} of footage at recent run sizes.",
        )
    # Deliberately no capacity figure: nothing has been processed here to size
    # against, and ROUGH_PASS_BYTES is a fallback for blocking a doomed batch,
    # not a number to quote at someone.
    return SetupCheck(
        "space", True, "Disk space", f"{free} free. Capacity is estimated once a run is recorded."
    )


def evaluate_setup(
    *,
    gpu_name: str | None,
    requires_gpu: bool,
    missing_models: list[str],
    free_bytes: int,
    min_free_bytes: int,
    bytes_per_footage_minute: float | None = None,
) -> list[SetupCheck]:
    """The three setup rows, in the order they are shown."""
    return [
        graphics_check(gpu_name=gpu_name, requires_gpu=requires_gpu),
        models_check(missing_models),
        space_check(free_bytes, min_free_bytes, bytes_per_footage_minute),
    ]


def setup_ready(checks: list[SetupCheck]) -> bool:
    return all(check.ok for check in checks)


@dataclass(frozen=True)
class BatchDiskEstimate:
    """Rough disk a batch needs, next to what is free, for the pre-flight summary."""

    pass_count: int
    need_bytes: int
    free_bytes: int

    @property
    def fits(self) -> bool:
        return self.free_bytes >= self.need_bytes


def estimate_batch_disk(
    pass_count: int, free_bytes: int, per_pass_bytes: int = ROUGH_PASS_BYTES
) -> BatchDiskEstimate:
    return BatchDiskEstimate(pass_count, pass_count * per_pass_bytes, free_bytes)


_TICK = f'<span style="color:{SUCCESS}; font-weight:bold;">&#10003;</span>'
_CROSS = f'<span style="color:{ERROR}; font-weight:bold;">&#10007;</span>'


class SimpleSetupMixin(MixinBase):
    """DeepReefMapWindow methods for the first-run laptop setup step."""

    def _build_setup_page(self) -> QWidget:
        """The setup step: three status rows and the actions that fix them.

        The models row carries the two provisioning actions, so a diver never
        has to leave simple mode to get the models a survey needs.
        """
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(GUTTER)

        card, card_layout = section_card("Environment")
        intro = QLabel("Requirements for processing a dive on this machine.")
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {TEXT_MUTED};")
        card_layout.addWidget(intro)

        self._setup_check_rows: dict[str, tuple[QLabel, QLabel, list[QWidget]]] = {}

        graphics_settings = QPushButton("Open settings")
        graphics_settings.clicked.connect(self._on_edit_run_settings)
        card_layout.addWidget(self._build_setup_row("graphics", [graphics_settings]))

        self._setup_usb_btn = QPushButton("Import from USB drive…")
        self._setup_usb_btn.clicked.connect(self._on_setup_import_pack)
        self._setup_download_btn = QPushButton("Download models (requires internet)")
        self._setup_download_btn.setProperty("cta", "true")
        self._setup_download_btn.clicked.connect(self._on_setup_download_models)
        card_layout.addWidget(
            self._build_setup_row("models", [self._setup_usb_btn, self._setup_download_btn])
        )

        space_browse = QPushButton("Open past surveys")
        space_browse.clicked.connect(lambda: self._go_to_step("browse"))
        card_layout.addWidget(self._build_setup_row("space", [space_browse]))

        # Memory advisory: driven by the batch grade, so it only appears when a
        # survey is queued that might run the machine low. Off by default.
        self._setup_memory_label = QLabel("")
        self._setup_memory_label.setWordWrap(True)
        self._setup_memory_label.setVisible(False)
        card_layout.addWidget(self._setup_memory_label)

        layout.addWidget(card)

        footer = QHBoxLayout()
        self._setup_summary = QLabel("")
        self._setup_summary.setWordWrap(True)
        self._setup_summary.setStyleSheet(f"color: {TEXT_MUTED};")
        footer.addWidget(self._setup_summary, 1)
        continue_btn = QPushButton("Start planning →")
        continue_btn.setProperty("cta", "true")
        continue_btn.clicked.connect(self._on_setup_continue)
        footer.addWidget(continue_btn)
        layout.addLayout(footer)
        layout.addStretch(1)

        self._refresh_setup_page()
        return page

    def _build_setup_row(self, key: str, actions: list[QWidget]) -> QWidget:
        """One status row: a tick or cross, a heading and a line, then its actions."""
        row = QWidget()
        outer = QHBoxLayout(row)
        outer.setContentsMargins(0, 4, 0, 4)
        outer.setSpacing(10)

        icon = QLabel(_TICK)
        icon.setFixedWidth(18)
        outer.addWidget(icon)

        text = QVBoxLayout()
        text.setSpacing(1)
        title = QLabel("")
        title.setStyleSheet("font-weight: 600;")
        detail = QLabel("")
        detail.setWordWrap(True)
        detail.setStyleSheet(f"color: {TEXT_MUTED};")
        detail.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        text.addWidget(title)
        text.addWidget(detail)
        outer.addLayout(text, 1)

        # Titles are static; the check functions own the same headings so the
        # painter and the verdict never disagree.
        for action in actions:
            outer.addWidget(action)

        self._setup_check_rows[key] = (icon, detail, actions)
        # Seed the static title once from a passing check of the same key.
        title.setText({"graphics": "Graphics card", "models": "Models", "space": "Disk space"}[key])
        return row

    def _build_setup_nav_button(self) -> QToolButton:
        """Header entry point, so the step is reopenable after the first launch."""
        button = QToolButton()
        button.setText("Environment")
        button.setProperty("quiet", "true")
        button.setToolTip("Check this machine meets the requirements for processing.")
        button.clicked.connect(lambda: self._set_simple_section("setup"))
        self._setup_nav_button = button
        return button

    def _current_setup_checks(self) -> list[SetupCheck]:
        """Probe the machine and the current settings into the three verdicts."""
        from deepreefmap_gui.models.manager import _MIN_FREE_BYTES

        out_root = Path(self._out_root_input.text()).expanduser()
        profile = probe_system(out_root)
        gpu_name = profile.gpu.name if profile.gpu.kind != GPU_NONE else None
        preset = getattr(self, "_survey_preset", None) or {}
        mapping = preset.get("mapping_name") or self._map_combo.currentText()
        return evaluate_setup(
            gpu_name=gpu_name,
            requires_gpu=mapping in GPU_ONLY_BACKENDS,
            missing_models=self._survey_missing_models(),
            free_bytes=profile.disk_free_bytes,
            min_free_bytes=_MIN_FREE_BYTES,
            bytes_per_footage_minute=self._footage_size_rate(out_root),
        )

    def _footage_size_rate(self, out_root: Path) -> float | None:
        """Measured output bytes per footage minute, remeasured when the root moves.

        Cached because the page repaints on every batch change and measuring walks
        the frame caches of several runs. _on_survey_done clears the cache, so a
        finished batch is measured in without re-walking on every keystroke.
        """
        cache = getattr(self, "_footage_rate_cache", None)
        if cache is not None and cache[0] == out_root:
            return cache[1]
        rate = measure_bytes_per_footage_minute(out_root)
        self._footage_rate_cache = (out_root, rate)
        return rate

    def _refresh_setup_page(self) -> None:
        """Repaint the rows from a fresh probe, and record readiness once reached."""
        if not hasattr(self, "_setup_check_rows"):
            return
        checks = self._current_setup_checks()
        by_key = {check.key: check for check in checks}
        for key, (icon, detail, actions) in self._setup_check_rows.items():
            check = by_key[key]
            icon.setText(_TICK if check.ok else _CROSS)
            detail.setText(check.detail)
            for action in actions:
                action.setVisible(not check.ok)
        ready = setup_ready(checks)
        unmet = sum(1 for check in checks if not check.ok)
        self._setup_summary.setText(
            "All requirements met."
            if ready
            else f"{unmet} requirement{'' if unmet == 1 else 's'} not met."
        )
        if ready:
            # Once the machine can run, setup stops leading on launch.
            self._settings.setValue("setup_complete", True)

    def _initial_simple_section(self) -> str:
        """Lead to setup on first launch, unless the laptop is already ready."""
        if str(self._settings.value("setup_complete", "false")).lower() == "true":
            return "plan"
        if setup_ready(self._current_setup_checks()):
            self._settings.setValue("setup_complete", True)
            return "plan"
        return "setup"

    def _on_setup_continue(self) -> None:
        # Acknowledging the step is itself a reason to stop leading with it: the
        # diver has seen the state and chosen to move on.
        self._settings.setValue("setup_complete", True)
        self._set_simple_section("plan")

    def _on_setup_import_pack(self) -> None:
        if self._survey_worker_running:
            self._status_label.setText("Unavailable while processing.")
            return
        self._on_import_model_pack()

    def _on_setup_download_models(self) -> None:
        """Download the models the current settings need, signing in first if asked."""
        if self._survey_worker_running:
            self._status_label.setText("Unavailable while processing.")
            return
        missing = self._survey_missing_models()
        if not missing:
            self._status_label.setText("All required models are already installed.")
            self._refresh_setup_page()
            return
        from deepreefmap_gui.models.manager import all_known_models

        catalogue = {info.name: info for info in all_known_models()}
        needs_account = (
            self._hf_auth_user is None
            and any(catalogue[name].gated for name in missing if name in catalogue)
        )
        if needs_account:
            # Fold the sign-in into the one download action.
            self._status_label.setText(
                "Some models require a free Hugging Face account. Sign in, then download again."
            )
            self._on_hf_auth_button()
            return
        for name in missing:
            self._download_model(name)
        self._status_label.setText(f"Downloading {len(missing)} model(s)…")
