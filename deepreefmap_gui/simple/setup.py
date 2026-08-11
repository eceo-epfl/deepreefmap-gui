"""The readiness check, shown before planning on a machine that is not ready.

Four rows say whether this machine can process a dive: the graphics card,
memory, the models it needs, and disk space. Each failing row carries the one
action that fixes it. When they pass, the check stops leading on launch.

Memory is advisory: it fails against the session currently queued rather than
against the machine, and processing still starts.

It is the first view of the Setup destination, which is why the page is
a bare card rather than a titled one: simple/machine.py owns the heading and the
switch that reaches the other views.

The verdict functions are pure and Qt-free so the pass/fail logic is tested
without a window, the same split progress.py uses for the step badges.
"""

from __future__ import annotations

import json
import logging
import statistics
import threading
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.core.theme import (
    ERROR,
    GUTTER,
    READING_WIDTH,
    SPACE_SM,
    SPACE_XS,
    SUCCESS,
    TEXT_MUTED,
    WARNING,
)
from deepreefmap_gui.core.widgets import section_card
from deepreefmap_gui.core.window_protocol import MixinBase
from deepreefmap_gui.models.cache import GPU_ONLY_BACKENDS
from deepreefmap_gui.packaging.shortcuts import (
    ShortcutResult,
    ShortcutState,
    ShortcutStatus,
    install_shortcut,
    remove_shortcut,
    shortcut_status,
)
from deepreefmap_gui.profiling.system_probe import GPU_NONE, format_bytes, probe_system
from deepreefmap_gui.survey.health import SurveyDbHealth

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

# A status row's status glyph, and the gap between it and the text, so anything
# that has to line up with that text can be indented by the sum.
_ROW_ICON_WIDTH = 18
_ROW_GAP = 10
_ROW_TEXT_INDENT = _ROW_ICON_WIDTH + _ROW_GAP

# Where the footage estimate stops being a figure and becomes a reassurance.
# Well beyond any single field season, so the only readings it suppresses are
# the ones a near-zero measured rate produced.
_FOOTAGE_CEILING_HOURS = 500


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
    """One setup row: whether it passes, its heading, and a plain-language line.

    An advisory row can fail without the machine being unready: processing still
    runs, it may just not finish.
    """

    key: str
    ok: bool
    title: str
    detail: str
    advisory: bool = False
    # What the row's action offers right now. Empty keeps the default, where an
    # action is a way to fix what the row reports and so is hidden once it
    # passes. A row whose action toggles something -- rather than fixing it --
    # names the action here and stays visible in both states.
    action_label: str = ""


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
    an accuracy the estimate does not have. Past a few days of footage it stops
    quoting a number at all: the extrapolation is from a rate measured over
    minutes, and "612490 hours" is not a capacity, it is a division.
    """
    if minutes < 90:
        return f"about {max(5, round(minutes / 5) * 5)} minutes"
    hours = minutes / 60
    if hours < 10:
        return f"about {hours:.0f} hours"
    if hours > _FOOTAGE_CEILING_HOURS:
        return f"well over {_FOOTAGE_CEILING_HOURS} hours"
    return f"about {round(hours / 10) * 10} hours"


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
            f"{free} free, {capacity} of footage at recent run sizes.",
        )
    # Deliberately no capacity figure: nothing has been processed here to size
    # against, and ROUGH_PASS_BYTES is a fallback for blocking a doomed batch,
    # not a number to quote at someone.
    return SetupCheck(
        "space", True, "Disk space", f"{free} free. Capacity is estimated once a run is recorded."
    )


def memory_check(
    *, total_ram_bytes: int, vram_bytes: int | None, advisory: str = ""
) -> SetupCheck:
    """Memory row. Advisory: a session graded short of memory still runs."""
    if advisory:
        return SetupCheck("memory", False, "Memory", advisory, advisory=True)
    installed = format_bytes(total_ram_bytes)
    if vram_bytes:
        return SetupCheck(
            "memory",
            True,
            "Memory",
            f"{installed} of memory, and {format_bytes(vram_bytes)} on the graphics card.",
        )
    return SetupCheck("memory", True, "Memory", f"{installed} of memory.")


def survey_check(health: SurveyDbHealth) -> SetupCheck:
    """Survey database row. Blocking: a run that cannot be recorded loses its
    provenance, and nothing in Transects, Process or Browse works without it."""
    if health.openable:
        return SetupCheck(
            "survey", True, "Survey database", f"Reading and writing {health.path}."
        )
    return SetupCheck(
        "survey",
        False,
        "Survey database",
        health.detail or f"{health.path} could not be opened.",
        action_label="Repair…",
    )


def shortcut_check(status: ShortcutStatus) -> SetupCheck:
    """Applications-menu row. Advisory: a missing shortcut stops nothing.

    A shortcut somebody else put there -- an installer, a disk image -- is
    reported without an action. Offering to remove it would delete the entry its
    uninstaller expects to find.
    """
    if status.state is ShortcutState.UNSUPPORTED:
        return SetupCheck("shortcut", True, "Applications menu", status.detail, advisory=True)
    if not status.owned and status.state in (ShortcutState.CURRENT, ShortcutState.UNKNOWN):
        return SetupCheck(
            "shortcut", True, "Applications menu", "Installed, and managed by the installer."
        )
    if status.state is ShortcutState.ABSENT:
        return SetupCheck(
            "shortcut",
            False,
            "Applications menu",
            "DeepReefMap is not in this computer's applications menu.",
            advisory=True,
            action_label="Add",
        )
    if status.state is ShortcutState.STALE:
        return SetupCheck(
            "shortcut",
            False,
            "Applications menu",
            "The existing entry points at a copy of DeepReefMap that has moved.",
            advisory=True,
            action_label="Update",
        )
    return SetupCheck(
        "shortcut",
        True,
        "Applications menu",
        f"Opens from the applications menu ({status.location}).",
        action_label="Remove",
    )


def evaluate_setup(
    *,
    gpu_name: str | None,
    requires_gpu: bool,
    missing_models: list[str],
    free_bytes: int,
    min_free_bytes: int,
    total_ram_bytes: int = 0,
    vram_bytes: int | None = None,
    memory_advisory: str = "",
    bytes_per_footage_minute: float | None = None,
    survey_health: SurveyDbHealth | None = None,
    shortcut_status: ShortcutStatus | None = None,
) -> list[SetupCheck]:
    """The setup rows, in the order they are shown."""
    checks = [
        graphics_check(gpu_name=gpu_name, requires_gpu=requires_gpu),
        memory_check(
            total_ram_bytes=total_ram_bytes, vram_bytes=vram_bytes, advisory=memory_advisory
        ),
        models_check(missing_models),
        space_check(free_bytes, min_free_bytes, bytes_per_footage_minute),
    ]
    if survey_health is not None:
        checks.append(survey_check(survey_health))
    if shortcut_status is not None:
        checks.append(shortcut_check(shortcut_status))
    return checks


def setup_ready(checks: list[SetupCheck]) -> bool:
    """Advisory rows are excluded: they report a risk, not a missing requirement."""
    return all(check.ok for check in checks if not check.advisory)


@dataclass(frozen=True)
class BatchDiskEstimate:
    """Rough disk a session needs, next to what is free, for the pre-flight summary."""

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


_ROW_TITLES = {
    "graphics": "Graphics card",
    "memory": "Memory",
    "models": "Models",
    "space": "Disk space",
    "survey": "Survey database",
    "shortcut": "Applications menu",
}

_TICK = f'<span style="color:{SUCCESS}; font-weight:bold;">&#10003;</span>'
_CROSS = f'<span style="color:{ERROR}; font-weight:bold;">&#10007;</span>'
# An advisory failure: the machine is not missing anything, the session queued
# against it is large.
_ALERT = f'<span style="color:{WARNING}; font-weight:bold;">!</span>'


class SimpleSetupMixin(MixinBase):
    """DeepReefMapWindow methods for the first-run laptop setup step."""

    def _build_readiness_view(self) -> QWidget:
        """Readiness: four status rows, the actions that fix them, and where
        results are saved.

        The models row carries the two provisioning actions, so a diver never
        has to leave the page to get the models a survey needs.
        """
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        # A few rows of status is a short page, so it is capped at the width of
        # what it has to say. Pinned to the top left rather than centred:
        # the heading and the view switch above it start at the page margin, and
        # a card that floats away from the control that opened it reads as a
        # separate thing.
        row = QHBoxLayout()
        column = QWidget()
        column.setMaximumWidth(READING_WIDTH)
        layout = QVBoxLayout(column)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(GUTTER)
        row.addWidget(column, 1)
        row.addStretch(0)
        outer.addLayout(row)
        outer.addStretch(1)

        # Untitled: the page it sits on is already headed Setup, and the
        # segmented control above says which of its views this is.
        card, card_layout = section_card()
        intro = QLabel("What processing a dive needs, and whether it is here.")
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {TEXT_MUTED};")
        card_layout.addWidget(intro)

        self._setup_check_rows: dict[str, tuple[QLabel, QLabel, list[QWidget]]] = {}

        graphics_settings = QPushButton("Open settings")
        graphics_settings.clicked.connect(self._on_edit_run_settings)
        card_layout.addWidget(self._build_setup_row("graphics", [graphics_settings]))

        memory_settings = QPushButton("Open settings")
        memory_settings.clicked.connect(self._on_edit_run_settings)
        card_layout.addWidget(self._build_setup_row("memory", [memory_settings]))

        self._setup_usb_btn = QPushButton("Import from USB drive…")
        self._setup_usb_btn.clicked.connect(self._on_setup_import_pack)
        self._setup_download_btn = QPushButton("Download models (requires internet)")
        self._setup_download_btn.setProperty("cta", "true")
        self._setup_download_btn.clicked.connect(self._on_setup_download_models)
        card_layout.addWidget(
            self._build_setup_row("models", [self._setup_usb_btn, self._setup_download_btn])
        )

        space_browse = QPushButton("Open past surveys")
        space_browse.clicked.connect(lambda: self._go_to_section("browse"))
        card_layout.addWidget(self._build_setup_row("space", [space_browse]))

        self._setup_survey_btn = QPushButton("Repair…")
        self._setup_survey_btn.clicked.connect(self.check_survey_database)
        card_layout.addWidget(self._build_setup_row("survey", [self._setup_survey_btn]))

        # Its label changes with what the entry currently is, so the row carries
        # the verb rather than the button carrying a fixed one.
        self._setup_shortcut_btn = QPushButton("Add")
        self._setup_shortcut_btn.clicked.connect(self._on_toggle_shortcut)
        card_layout.addWidget(self._build_setup_row("shortcut", [self._setup_shortcut_btn]))

        # The folder every run lands in, directly under the row that measures
        # the disk it sits on. It decides which disk that is, and it is the
        # first thing a diver setting up a field laptop has to point somewhere
        # with room on it, so it belongs on the page that leads on launch rather
        # than a view along.
        card_layout.addWidget(self._build_out_root_block())

        # Inside the card and at its foot, so the summary and the way out sit
        # with the checks they describe. Loose underneath, with the page's empty
        # space below them, they read as a second unrelated thing.
        card_layout.addStretch(1)
        footer = QHBoxLayout()
        self._setup_summary = QLabel("")
        self._setup_summary.setWordWrap(True)
        self._setup_summary.setStyleSheet(f"color: {TEXT_MUTED};")
        footer.addWidget(self._setup_summary, 1)
        self._setup_continue_btn = QPushButton("Go to Transects →")
        self._setup_continue_btn.setProperty("cta", "true")
        self._setup_continue_btn.clicked.connect(self._on_setup_continue)
        footer.addWidget(self._setup_continue_btn)
        card_layout.addLayout(footer)

        layout.addWidget(card)

        self._refresh_readiness_view()
        return page

    def _build_out_root_block(self) -> QWidget:
        """The slot the output root controls are lent to, and a line saying what
        lands there.

        Indented to the depth of the row text above it so it reads as a
        continuation of disk space rather than a fifth check with a missing tick.
        """
        block = QWidget()
        layout = QVBoxLayout(block)
        layout.setContentsMargins(_ROW_TEXT_INDENT, SPACE_SM, 0, 0)
        layout.setSpacing(SPACE_XS)
        self._machine_out_root_host = QWidget()
        host_layout = QVBoxLayout(self._machine_out_root_host)
        host_layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._machine_out_root_host)
        caption = QLabel(
            "Every run, and the survey database that tracks them, is written under "
            "this folder."
        )
        caption.setWordWrap(True)
        caption.setStyleSheet(f"color: {TEXT_MUTED};")
        layout.addWidget(caption)
        return block

    def _build_setup_row(self, key: str, actions: list[QWidget]) -> QWidget:
        """One status row: a tick or cross, a heading and a line, then its actions."""
        row = QWidget()
        outer = QHBoxLayout(row)
        outer.setContentsMargins(0, SPACE_XS, 0, SPACE_XS)
        outer.setSpacing(_ROW_GAP)

        icon = QLabel(_TICK)
        icon.setFixedWidth(_ROW_ICON_WIDTH)
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
        title.setText(_ROW_TITLES[key])
        return row

    def _current_setup_checks(self) -> list[SetupCheck]:
        """Probe the machine and the current settings into the three verdicts."""
        from deepreefmap_gui.models.cache import _MIN_FREE_BYTES

        out_root = Path(self._out_root_input.text()).expanduser()
        profile = probe_system(out_root)
        gpu_name = profile.gpu.name if profile.gpu.kind != GPU_NONE else None
        # The form, not the preset. _gpu_only_mapper and _required_model_names
        # both read these widgets, and the batch runs from them via
        # _collect_run_settings, so reading the preset here left the readiness
        # rows judging a different configuration from the one the Run gate
        # blocks on. They agree today; nothing was keeping them that way.
        mapping = self._map_combo.currentText()
        return evaluate_setup(
            gpu_name=gpu_name,
            requires_gpu=mapping in GPU_ONLY_BACKENDS,
            missing_models=self._survey_missing_models(),
            free_bytes=profile.disk_free_bytes,
            min_free_bytes=_MIN_FREE_BYTES,
            total_ram_bytes=profile.total_ram_bytes,
            vram_bytes=profile.gpu.total_vram_bytes if profile.gpu.has_distinct_vram else None,
            memory_advisory=getattr(self, "_memory_advisory", ""),
            bytes_per_footage_minute=self._footage_size_rate(out_root),
            survey_health=self._survey_db_health(),
            shortcut_status=self._current_shortcut_status(),
        )

    def _current_shortcut_status(self) -> ShortcutStatus:
        """Re-read every repaint, so an entry removed outside the app is noticed.

        Reading is filesystem work on Linux and macOS. On Windows it shells out
        to PowerShell, so the answer is cached until something acts on it --
        repainting the page must not cost a subprocess each time.
        """
        cached = getattr(self, "_shortcut_status_cache", None)
        if cached is None:
            cached = shortcut_status()
            self._shortcut_status_cache = cached
        return cached

    def _on_toggle_shortcut(self) -> None:
        """Add, re-point or remove the applications-menu entry.

        On a worker thread: writing a Start Menu shortcut goes through
        PowerShell, whose cold start behind real-time antivirus scanning is
        routinely several seconds, and a window frozen that long reads as a
        crash.
        """
        status = self._current_shortcut_status()
        adding = status.state in (ShortcutState.ABSENT, ShortcutState.STALE)
        self._setup_shortcut_btn.setEnabled(False)
        self._setup_shortcut_btn.setText("Working…")

        def work() -> None:
            result = install_shortcut() if adding else remove_shortcut()
            self._sig_shortcut_done.emit(result)

        threading.Thread(target=work, daemon=True).start()

    def _on_shortcut_done(self, result: ShortcutResult) -> None:
        self._shortcut_status_cache = result.status
        self._setup_shortcut_btn.setEnabled(True)
        self._refresh_readiness_view()
        self._status_label.setText(result.message)
        if not result.ok:
            # Reported, not swallowed: a shortcut that silently fails to appear
            # is indistinguishable from one the desktop has not noticed yet.
            QMessageBox.warning(self, "Applications menu", result.message)

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

    def _refresh_readiness_view(self) -> None:
        """Repaint the rows from a fresh probe, and record readiness once reached."""
        if not hasattr(self, "_setup_check_rows"):
            return
        checks = self._current_setup_checks()
        by_key = {check.key: check for check in checks}
        for key, (icon, detail, actions) in self._setup_check_rows.items():
            check = by_key[key]
            if check.ok:
                icon.setText(_TICK)
            else:
                icon.setText(_ALERT if check.advisory else _CROSS)
            detail.setText(check.detail)
            # A row that names its action keeps it in both states: it toggles
            # something rather than fixing what the row reports.
            if check.action_label:
                for action in actions:
                    if isinstance(action, QPushButton):
                        action.setText(check.action_label)
                    action.setVisible(True)
                continue
            for action in actions:
                action.setVisible(not check.ok)
        ready = setup_ready(checks)
        unmet = sum(1 for check in checks if not check.ok and not check.advisory)
        # Says what the unmet count means for the button beside it. "1
        # requirement not met" next to a lit-up way onwards read as a
        # contradiction; the rest of the app is genuinely still available, and
        # processing genuinely is not, so the sentence says both.
        self._setup_summary.setText(
            "All requirements met."
            if ready
            else f"{unmet} requirement{'' if unmet == 1 else 's'} not met. You can still"
            " mark out transects and queue passes; processing will not run until this is fixed."
        )
        # When something is broken the loudest button on the page should be the
        # one that fixes it, not the one that carries on past it.
        self._setup_continue_btn.setProperty("cta", "true" if ready else None)
        self._setup_continue_btn.style().unpolish(self._setup_continue_btn)
        self._setup_continue_btn.style().polish(self._setup_continue_btn)
        if ready:
            # Once the machine can run, readiness stops leading on launch.
            self._settings.setValue("setup_complete", True)
        # The header button says the same thing from the other side of the
        # window, so it is repainted from the same verdict rather than its own.
        self._refresh_machine_button()

    def _initial_simple_section(self) -> str:
        """Lead to readiness on first launch, unless the laptop is already ready.

        Otherwise the footage: Videos is where a day starts, and it is the one
        destination that says something useful about a survey with nothing in
        it yet.
        """
        if str(self._settings.value("setup_complete", "false")).lower() == "true":
            return "videos"
        if setup_ready(self._current_setup_checks()):
            self._settings.setValue("setup_complete", True)
            return "videos"
        return "machine"

    def _on_setup_continue(self) -> None:
        # Acknowledging the check is itself a reason to stop leading with it:
        # the diver has seen the state and chosen to move on.
        self._settings.setValue("setup_complete", True)
        self._set_simple_section("videos")

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
            self._refresh_readiness_view()
            return
        from deepreefmap_gui.models.cache import all_known_models

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
