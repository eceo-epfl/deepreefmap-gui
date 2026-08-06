"""Videos: the footage itself, grouped by the day it was shot.

The destination a dive day starts and ends at. Clips arrive here, are cut into
sections, get a transect, and go in the cart; afterwards the same rows show what
came of them. Browse is for finished runs, so a clip appears in exactly one
place in the app and this is it.

The library it lists comes from ``survey/catalogue.py::video_library``, the
grouping and the section strips from ``survey/video_groups.py``, and the widgets
from ``runs/video_rows.py``. What lives here is the page: which clips are shown,
what the buttons do, and keeping the answers off the thread that paints them.
"""

from __future__ import annotations

import logging
import threading
import uuid
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from deepreefmap_gui.core.reveal import reveal_in_file_manager
from deepreefmap_gui.core.theme import GUTTER, PRIMARY, SPACE_SM
from deepreefmap_gui.core.widgets import (
    EmptyState,
    FilterChips,
    clip_outcome_color,
    confirm,
    section_column,
)
from deepreefmap_gui.core.window_protocol import MixinBase
from deepreefmap_gui.runs.section_detail import SectionDetailPanel, section_window
from deepreefmap_gui.runs.video_detail import VideoDetailPanel
from deepreefmap_gui.runs.video_rows import VideoLibraryList
from deepreefmap_gui.survey import catalogue, statuses
from deepreefmap_gui.survey.catalogue import LINK_LINKED, VideoLibraryEntry
from deepreefmap_gui.survey.models import TransectPass
from deepreefmap_gui.survey.models.video_asset import VideoAsset
from deepreefmap_gui.survey.video_groups import (
    DEFAULT_PERIOD,
    PERIODS,
    group_by_period,
    pass_status,
)

logger = logging.getLogger(__name__)

# Chip order is the order work moves through: nothing done, part done, broken,
# finished. "All" first so the unfiltered view is where the eye starts.
_CLIP_FILTERS = (
    ("all", "All", PRIMARY),
    *((spec.key, spec.label, clip_outcome_color(spec.key)) for spec in statuses.CLIP_OUTCOMES),
)

_SEARCH_WIDTH = 240

# The clip list is the page; the detail pane describes whichever row is picked.
_DETAIL_SHARE = 0.30
_DETAIL_MIN_WIDTH = 260

_PERIOD_TOOLTIP = (
    "How far apart two clips have to be shot to be filed separately. A card off "
    "one dive day reads as one group by day."
)


class VideoLibraryMixin(MixinBase):
    """DeepReefMapWindow methods that build and drive the Videos destination."""

    _video_clip_filter: str = "all"
    _video_period: str = DEFAULT_PERIOD
    _selected_pass_id: str | None = None

    def _build_video_library(self) -> QWidget:
        self._video_entries: list[VideoLibraryEntry] = []
        # Keyed by path rather than clip id, so an answer survives the library
        # being rebuilt on the next scan.
        self._clip_link_cache: dict[str, str] = {}
        self._clip_link_scan_running = False
        self._video_period = str(
            self._settings.value("video_group_period", DEFAULT_PERIOD) or DEFAULT_PERIOD
        )

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPACE_SM)

        top_row = QHBoxLayout()
        top_row.setSpacing(GUTTER)
        top_row.addWidget(QLabel("Group"))
        self._video_period_chips = FilterChips(PERIODS)
        self._video_period_chips.setToolTip(_PERIOD_TOOLTIP)
        self._video_period_chips.set_current(self._video_period)
        self._video_period_chips.changed.connect(self._on_video_period_changed)
        top_row.addWidget(self._video_period_chips)
        self._video_search = QLineEdit()
        self._video_search.setPlaceholderText("Search clips…")
        self._video_search.setClearButtonEnabled(True)
        self._video_search.setMaximumWidth(_SEARCH_WIDTH)
        self._video_search.textChanged.connect(lambda *_: self._rebuild_video_list())
        top_row.addWidget(self._video_search)
        self._video_chips = FilterChips(_CLIP_FILTERS)
        self._video_chips.setToolTip(
            "Where each clip stands: whether every section cut from it has been "
            "processed, and whether any of them failed."
        )
        self._video_chips.changed.connect(self._on_video_filter_changed)
        top_row.addWidget(self._video_chips)
        top_row.addStretch(1)
        self._video_add_btn = QPushButton("Add videos…")
        self._video_add_btn.clicked.connect(self._on_video_add_clicked)
        top_row.addWidget(self._video_add_btn)
        layout.addLayout(top_row)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.setHandleWidth(SPACE_SM)

        column, column_layout = section_column("Footage")
        self._video_list = VideoLibraryList()
        self._video_list.activated.connect(self._on_video_activated)
        self._video_list.play_requested.connect(self._on_video_play)
        self._video_list.reveal_requested.connect(self._on_video_reveal)
        self._video_list.new_section_requested.connect(self._on_video_new_section_for)
        self._video_list.span_clicked.connect(self._select_section)
        self._video_list.section_activated.connect(self._select_section)
        self._video_list.section_add_to_cart.connect(self._on_video_pass_to_cart)
        self._video_list.section_retrim.connect(self._on_section_retrim)
        self._video_list.section_reassign.connect(self._on_section_reassign)
        self._video_list.section_delete.connect(self._on_section_delete)
        # Dropping footage in is the fastest way to fill a library, and the one
        # feature nothing else on the page advertises, so the list says so.
        self._video_list.setAcceptDrops(True)
        self._video_list.installEventFilter(self)
        self._video_list.viewport().setAcceptDrops(True)
        self._video_list.viewport().installEventFilter(self)
        self._video_stack = QStackedWidget()
        self._video_stack.addWidget(self._video_list)
        self._video_stack.addWidget(
            EmptyState("No footage yet", "Drop clips here, or use Add videos…")
        )
        column_layout.addWidget(self._video_stack, 1)
        split.addWidget(column)

        # The clip above the section it holds, so drilling in never costs sight
        # of what was drilled into: the cut list stays on screen beside the runs
        # made from the cut.
        detail = QSplitter(Qt.Orientation.Vertical)
        detail.setHandleWidth(SPACE_SM)
        detail.setMinimumWidth(_DETAIL_MIN_WIDTH)

        self._video_detail = VideoDetailPanel()
        self._video_detail.queue_requested.connect(self._on_video_new_section)
        self._video_detail.pass_activated.connect(self._select_section)
        self._video_detail.add_to_cart_requested.connect(self._on_video_pass_to_cart)
        self._video_detail.relocate_requested.connect(self._on_video_relocate)
        detail.addWidget(self._video_detail)

        self._section_detail = SectionDetailPanel()
        self._section_detail.add_to_cart_requested.connect(self._on_video_pass_to_cart)
        self._section_detail.retrim_requested.connect(self._on_section_retrim)
        self._section_detail.reassign_requested.connect(self._on_section_reassign)
        self._section_detail.delete_requested.connect(self._on_section_delete)
        self._section_detail.run_activated.connect(self._on_section_run_activated)
        self._section_detail.setVisible(False)
        detail.addWidget(self._section_detail)
        split.addWidget(detail)

        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 0)
        self._video_split = split
        layout.addWidget(split, 1)
        return page

    # --- the library ---------------------------------------------------------

    def _refresh_video_library(self, store=None) -> None:
        """Re-read the library and repaint. Called by whatever rescanned the root."""
        if store is None:
            store = self._try_survey_store()
        self._video_entries = self._load_video_entries(store)
        for clip in self._video_entries:
            clip.link_state = self._clip_link_cache.get(clip.video.path, catalogue.LINK_UNKNOWN)
        self._start_clip_link_scan()
        self._rebuild_video_list()
        self._refresh_storage_bars()

    def _load_video_entries(self, store) -> list[VideoLibraryEntry]:
        """Every clip the survey has imported, runs or no runs."""
        if store is None:
            return []
        try:
            return catalogue.video_library(
                store.list_videos(), store.list_passes(), store.list_runs()
            )
        except Exception:
            logger.exception("Could not list the video library")
            return []

    def _visible_clips(self) -> list[VideoLibraryEntry]:
        clips = getattr(self, "_video_entries", [])
        if self._video_clip_filter != "all":
            clips = [c for c in clips if c.outcome == self._video_clip_filter]
        needle = self._video_search.text().strip().lower()
        if needle:
            clips = [c for c in clips if needle in c.video.file_name.lower()]
        return clips

    def _rebuild_video_list(self) -> None:
        if getattr(self, "_video_list", None) is None:
            return
        clips = self._visible_clips()
        groups = group_by_period(clips, self._video_period)
        self._video_list.set_groups(
            groups, self._transect_name_for, in_cart=self._pass_in_current_cart
        )
        self._video_stack.setCurrentIndex(0 if clips else 1)
        self._refresh_video_chips()
        self._refresh_video_detail()
        self._refresh_section_state()

    def _refresh_video_chips(self) -> None:
        clips = getattr(self, "_video_entries", [])
        counts = {option[0]: 0 for option in _CLIP_FILTERS}
        counts["all"] = len(clips)
        for clip in clips:
            counts[clip.outcome] = counts.get(clip.outcome, 0) + 1
        self._video_chips.set_counts(counts)

    def _refresh_video_detail(self) -> None:
        if self._selected_pass_id:
            clip = self._clip_for_pass(self._selected_pass_id)
            if clip is not None:
                self._video_detail.setVisible(True)
                self._video_detail.show_entry(clip, self._transect_name_for)
                self._fill_section_detail(clip, self._selected_pass_id)
                return
            # The cut it was showing is gone, so nothing below the clip stands.
            self._selected_pass_id = None
        self._section_detail.setVisible(False)
        clip = self._selected_clip()
        if clip is None:
            self._video_detail.clear()
            self._video_detail.setVisible(False)
            return
        self._video_detail.setVisible(True)
        self._video_detail.show_entry(clip, self._transect_name_for)

    def _selected_clip(self) -> VideoLibraryEntry | None:
        return self._clip_by_id(self._video_list.selected)

    def _clip_by_id(self, video_id: str | None) -> VideoLibraryEntry | None:
        if not video_id:
            return None
        for clip in getattr(self, "_video_entries", []):
            if str(clip.video.id) == video_id:
                return clip
        return None

    def _transect_name_for(self, transect_id) -> str | None:
        if transect_id is None:
            return None
        store = self._try_survey_store()
        if store is None:
            return None
        try:
            transect = store.get_transect(transect_id)
        except Exception:
            logger.exception("Could not read the transect for a section")
            return None
        return transect.name if transect is not None else None

    def _start_clip_link_scan(self) -> None:
        """Ask, off the paint path, whether each clip's file is still there.

        A stat is cheap until the drive is asleep or the mount is gone, and both
        are ordinary in the field, so this never runs on the GUI thread. Until it
        answers, a clip's state is "unknown" and no link icon is painted at all
        rather than a hopeful one.
        """
        if self._clip_link_scan_running:
            return
        paths = {clip.video.path for clip in getattr(self, "_video_entries", [])}
        todo = [p for p in paths if p not in self._clip_link_cache]
        if not todo:
            return
        self._clip_link_scan_running = True
        entries = list(self._video_entries)

        def worker() -> None:
            states = catalogue.resolve_link_states(entries)
            # Widgets are off limits here; the Signal hands over to the GUI thread.
            self._sig_clip_links_done.emit(states)

        threading.Thread(target=worker, daemon=True, name="clip-link-scan").start()

    def _apply_clip_link_states(self, states: dict) -> None:
        self._clip_link_scan_running = False
        self._clip_link_cache.update(states)
        for clip in getattr(self, "_video_entries", []):
            clip.link_state = self._clip_link_cache.get(clip.video.path, catalogue.LINK_UNKNOWN)
        self._rebuild_video_list()
        self._refresh_storage_bars()

    def _repair_video_identity(self, store) -> None:
        """Give each clip one row, its hash, and a reading of its container.

        Inline rather than on a worker thread, beside the rebuild_from_scan that
        already reads every manifest: it runs once per root, only touches rows
        that are missing something, and both imohash and the container read
        sample a file rather than reading it through, so the cost is a stat and a
        few seeks per clip. A thread would buy little and would put a second
        writer on a database the window is reading, which SurveyStore's
        thread-local connections make exactly the kind of thing worth not having.
        """
        from deepreefmap_gui.survey.video_repair import repair_video_identity

        try:
            report = repair_video_identity(store)
        except Exception:
            logger.exception("Could not repair the video library")
            return
        if report.summary():
            self._status_label.setText(report.summary())

    # --- filters -------------------------------------------------------------

    def _on_video_period_changed(self, key: str) -> None:
        self._video_period = key
        # A reader's preference on this machine, not a fact about the survey, so
        # it goes to QSettings rather than the database.
        self._settings.setValue("video_group_period", key)
        self._rebuild_video_list()

    def _on_video_filter_changed(self, key: str) -> None:
        self._video_clip_filter = key
        self._rebuild_video_list()

    # --- one clip ------------------------------------------------------------

    def _on_video_activated(self, video_id: str) -> None:
        self._video_list.set_selected(video_id)
        self._selected_pass_id = None
        self._section_detail.setVisible(False)
        self._refresh_video_detail()

    def _select_section(self, pass_id: str) -> None:
        """Show one cut, wherever it was picked: the tree, the clip card, the strip.

        One entry point for all three so they cannot disagree about what is
        selected, and so a span click opens the clip it belongs to.
        """
        clip = self._clip_for_pass(pass_id)
        if clip is None:
            return
        self._selected_pass_id = pass_id
        self._video_list.expand(str(clip.video.id))
        self._video_list.set_selected_section(pass_id)
        self._video_detail.setVisible(True)
        self._video_detail.show_entry(clip, self._transect_name_for)
        self._fill_section_detail(clip, pass_id)

    def _clip_for_pass(self, pass_id: str) -> VideoLibraryEntry | None:
        for clip in getattr(self, "_video_entries", []):
            if any(str(p.id) == pass_id for p in clip.passes):
                return clip
        return None

    def _fill_section_detail(self, clip: VideoLibraryEntry, pass_id: str) -> None:
        pass_ = next((p for p in clip.passes if str(p.id) == pass_id), None)
        if pass_ is None:
            self._section_detail.setVisible(False)
            return
        runs = [r for r in clip.runs if str(r.pass_id) == pass_id]
        sizes = getattr(self, "_run_size_cache", {})
        self._section_detail.show_section(
            pass_,
            clip_name=clip.video.file_name,
            transect_name=self._transect_name_for(pass_.transect_id),
            status=pass_status(runs),
            runs=runs,
            session_name=self._session_name_for,
            in_cart=self._pass_in_current_cart(pass_id),
            output_bytes=sum(sizes.get(r.run_dir_name, 0) for r in runs),
        )
        self._section_detail.setVisible(True)

    def _session_name_for(self, batch_id) -> str | None:
        if batch_id is None:
            return None
        store = self._try_survey_store()
        if store is None:
            return None
        try:
            batch = store.get_batch(batch_id)
        except Exception:
            logger.exception("Could not read the session for a run")
            return None
        return batch.name if batch is not None else None

    def _on_video_add_clicked(self) -> None:
        self._on_survey_add_videos()

    def _on_video_new_section(self) -> None:
        clip = self._video_detail.entry
        if clip is not None:
            self._new_section_from_clip(clip)

    def _on_video_new_section_for(self, video_id: str) -> None:
        clip = self._clip_by_id(video_id)
        if clip is not None:
            self._new_section_from_clip(clip)

    def _new_section_from_clip(self, clip: VideoLibraryEntry) -> None:
        """Cut a section from a clip, file it, and drop it in the cart.

        Scrub first (the window is the section's identity), then the assign step,
        where a transect is a choice rather than a requirement. A clip with a
        missing file or unknown duration cannot be scrubbed and falls back to
        queueing the whole clip.
        """
        from deepreefmap_gui.form.video_scrub import VideoScrubDialog
        from deepreefmap_gui.simple.section_dialog import SectionAssignDialog

        duration = clip.video.duration_s or 0.0
        if clip.link_state != LINK_LINKED or duration <= 0.0:
            self._queue_video_path(clip.video.path)
            return
        store = self._try_survey_store()
        if store is None:
            self._status_label.setText("Survey database unavailable; cannot queue.")
            return
        scrub = VideoScrubDialog(
            clip.video.path, duration, 0.0, duration, parent=self, fps=clip.video.fps
        )
        if scrub.exec() != QDialog.DialogCode.Accepted:
            return
        begin_s, end_s = scrub.time_range()
        assign = SectionAssignDialog(self, store)
        if assign.exec() != QDialog.DialogCode.Accepted:
            return
        transect_id, direction = assign.choice()
        pass_ = TransectPass(
            transect_id=transect_id,
            video_id=clip.video.id,
            begin_s=begin_s,
            end_s=end_s,
            direction=direction,
        )
        store.add_pass(pass_)
        self._add_pass_to_cart(pass_.id)
        self._refresh_video_library()

    def _pass_in_current_cart(self, pass_id_str: object) -> bool:
        store = self._try_survey_store()
        if store is None:
            return False
        try:
            pass_id = uuid.UUID(str(pass_id_str))
        except (ValueError, TypeError):
            return False
        cart = store.current_cart()
        if cart is None:
            return False
        return any(item.pass_id == pass_id for item in store.list_batch_items(cart.id))

    def _on_video_pass_to_cart(self, pass_id_str: str) -> None:
        """A section asked for the cart, from the detail pane's list."""
        try:
            pass_id = uuid.UUID(pass_id_str)
        except (ValueError, AttributeError, TypeError):
            return
        store = self._try_survey_store()
        if store is None:
            self._status_label.setText("Survey database unavailable; cannot queue.")
            return
        if store.get_pass(pass_id) is None:
            return
        self._add_pass_to_cart(pass_id)

    def _on_section_retrim(self, pass_id: str) -> None:
        """Move a section's window. The trim is metadata, so it stays editable.

        A section that has already run keeps its runs: each one recorded the
        window it actually processed, so the history stays true even once the
        section is asking for a different one next time.
        """
        from deepreefmap_gui.form.video_scrub import VideoScrubDialog

        clip = self._clip_for_pass(pass_id)
        store = self._try_survey_store()
        if clip is None or store is None:
            return
        pass_ = store.get_pass(uuid.UUID(pass_id))
        if pass_ is None:
            return
        duration = clip.video.duration_s or 0.0
        if clip.link_state != LINK_LINKED or duration <= 0.0:
            self._status_label.setText(f"{clip.video.file_name} cannot be scrubbed.")
            return
        dialog = VideoScrubDialog(
            clip.video.path,
            duration,
            pass_.begin_s,
            pass_.end_s,
            parent=self,
            fps=clip.video.fps,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        pass_.begin_s, pass_.end_s = dialog.time_range()
        store.update_pass(pass_)
        self._refresh_video_library()
        self._select_section(pass_id)

    def _on_section_reassign(self, pass_id: str) -> None:
        """Change which transect a section belongs to, or its direction."""
        from deepreefmap_gui.simple.section_dialog import SectionAssignDialog

        store = self._try_survey_store()
        if store is None:
            return
        pass_ = store.get_pass(uuid.UUID(pass_id))
        if pass_ is None:
            return
        dialog = SectionAssignDialog(
            self,
            store,
            transect_id=pass_.transect_id,
            direction=pass_.direction,
            ok_label="Save",
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        pass_.transect_id, pass_.direction = dialog.choice()
        store.update_pass(pass_)
        self._refresh_video_library()
        self._select_section(pass_id)
        self._refresh_survey_batch_tab()

    def _on_section_delete(self, pass_id: str) -> None:
        """Remove a section, unless something was made from it.

        A section with runs is the record of what those runs processed, so it
        cannot quietly disappear from under them. Deleting the runs is Browse's
        job, and doing it there leaves the section deletable here.
        """
        store = self._try_survey_store()
        if store is None:
            return
        pass_ = store.get_pass(uuid.UUID(pass_id))
        if pass_ is None:
            return
        runs = store.runs_for_pass(pass_.id)
        if runs:
            count = f"{len(runs)} run{'' if len(runs) == 1 else 's'}"
            self._status_label.setText(
                f"This section has {count}. Delete them in Browse first."
            )
            return
        if not confirm(
            self,
            "Delete section",
            f"Remove the {section_window(pass_)} section from "
            f"{self._video_detail.entry.video.file_name if self._video_detail.entry else 'this clip'}?",
        ):
            return
        store.delete_pass(pass_.id)
        self._selected_pass_id = None
        self._section_detail.setVisible(False)
        self._refresh_video_library()
        self._refresh_survey_batch_tab()
        self._status_label.setText("Section deleted.")

    def _on_section_run_activated(self, run_dir_name: str) -> None:
        """Open what this section produced, the same as Open does in Browse."""
        root = Path(self._out_root_input.text()).expanduser()
        self._load_run_from_dir(root / run_dir_name)

    def _on_video_reveal(self, video_id: str) -> None:
        """Show the clip itself in the file manager, selected rather than merely near."""
        clip = self._clip_by_id(video_id)
        if clip is None:
            return
        if not reveal_in_file_manager(Path(clip.video.path)):
            self._status_label.setText("The file manager could not be opened.")

    def _on_video_play(self, video_id: str) -> None:
        clip = self._clip_by_id(video_id)
        if clip is not None:
            self._play_clip(clip)

    def _play_clip(self, clip: VideoLibraryEntry) -> None:
        """Play the footage. The trim dialog already decodes and seeks this file."""
        from deepreefmap_gui.form.video_scrub import VideoScrubDialog

        if clip.link_state != LINK_LINKED:
            self._status_label.setText(f"{clip.video.file_name} cannot be found.")
            return
        duration = clip.video.duration_s or 0.0
        dialog = VideoScrubDialog(
            clip.video.path,
            duration,
            0.0,
            duration,
            self,
            fps=clip.video.fps,
            trim=False,
        )
        dialog.exec()

    def _on_video_relocate(self) -> None:
        """Point a clip at the file's new home, once it is shown to be the same one.

        Verified against the checksum rather than the name: a GoPro names every
        card's first clip GX010001.MP4, so a filename match is close to no
        evidence at all, and repointing a clip at different footage would leave
        every run made from it describing a video it did not come from.

        A clip with no checksum cannot be verified, so the user is asked to
        confirm rather than being refused: some libraries predate hashing, and
        refusing outright would leave those clips permanently broken.
        """
        clip = self._video_detail.entry
        if clip is None:
            return
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            f"Locate {clip.video.file_name}",
            str(Path(clip.video.path).parent),
            "Video files (*.mp4 *.mov *.avi *.mkv);;All files (*)",
        )
        if not path_str:
            return
        chosen = Path(path_str)
        described = VideoAsset.from_path(chosen)
        if clip.video.hash and described.hash and described.hash != clip.video.hash:
            QMessageBox.warning(
                self,
                "Different footage",
                f"{chosen.name} is not the same recording as {clip.video.file_name}. "
                "The clip has been left pointing where it was.",
            )
            return
        if not clip.video.hash and not confirm(
            self,
            "Relocate clip",
            f"{clip.video.file_name} has no checksum, so this cannot be checked to "
            f"be the same footage. Point it at {chosen.name} anyway?",
        ):
            return
        self._apply_clip_relocation(clip, chosen, described)

    def _apply_clip_relocation(self, clip, chosen: Path, described: VideoAsset) -> None:
        video = clip.video
        video.overlay_from(described)
        video.path = str(chosen)
        video.file_name = chosen.name
        store = self._try_survey_store()
        if store is None:
            self._status_label.setText("The clip could not be relocated.")
            return
        try:
            store.update_video(video)
        except Exception:
            logger.exception("Could not relocate %s", video.id)
            self._status_label.setText("The clip could not be relocated.")
            return
        # The cache is keyed by the old path, which now describes nothing.
        self._clip_link_cache.pop(str(chosen), None)
        self._status_label.setText(f"{video.file_name} now points at {chosen}.")
        self._refresh_video_library()

    def _queue_video_path(self, path: str) -> None:
        """Import a whole clip as one section, for footage that cannot be scrubbed."""
        self._add_video_paths([path])
