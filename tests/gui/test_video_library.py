"""Videos: the footage, what has been cut from it, and whether it is still there."""

from pathlib import Path

from _factories import make_transect, write_test_mp4

from deepreefmap_gui.simple.mode import SIMPLE_SECTIONS
from deepreefmap_gui.survey.catalogue import (
    VIDEO_FAILED,
    VIDEO_PENDING,
    VIDEO_PROCESSED,
    VIDEO_UNPROCESSED,
)
from deepreefmap_gui.survey.models import RunRecord, TransectPass, VideoAsset


def _seed(store, name: str, *, passes: int = 0, statuses: tuple[str, ...] = ()) -> VideoAsset:
    video = store.upsert_video(
        VideoAsset(file_name=name, path=f"/data/{name}", hash=name * 4, duration_s=60.0)
    )
    transect = make_transect(name.replace(".", "_"))
    store.add_transect(transect)
    made = []
    for index in range(passes):
        pass_ = TransectPass(
            transect_id=transect.id, video_id=video.id, begin_s=index * 10.0, end_s=index * 10 + 5
        )
        store.add_pass(pass_)
        made.append(pass_)
    for pass_, status in zip(made, statuses, strict=False):
        run = RunRecord(pass_id=pass_.id, run_dir_name=f"{name}_{status}", status=status)
        store.add_run(run)
    return video


def _seed_at(store, name: str, path: Path) -> VideoAsset:
    return store.upsert_video(
        VideoAsset(file_name=name, path=str(path), hash=name * 4, duration_s=60.0)
    )


def show_videos(window) -> None:
    window._refresh_video_library()


def resolve_links(window) -> None:
    """Run the link scan's work inline, since the real one is on a worker."""
    from deepreefmap_gui.survey import catalogue

    window._apply_clip_link_states(catalogue.resolve_link_states(window._video_entries))


def select_clip(window, file_name: str) -> None:
    clip = next(c for c in window._video_entries if c.video.file_name == file_name)
    window._on_video_activated(str(clip.video.id))


def listed_names(window) -> list[str]:
    return [row.entry.video.file_name for row in window._video_list.rows().values()]


def test_videos_is_a_destination_of_its_own(window):
    """Scenario: clips used to be a grouping inside the page built for runs.

    Expected behaviour: footage has its own destination, and Browse no longer
    offers a video grouping, so a clip lives in exactly one place.
    """
    window._set_simple_section("videos")
    assert window._simple_stack.currentIndex() == SIMPLE_SECTIONS.index("videos")
    assert window._simple_nav_buttons["videos"].isChecked()
    assert "videos" not in window._data_facet_buttons


def test_every_imported_clip_is_listed(window):
    store = window._survey_store()
    _seed(store, "one.mp4")
    _seed(store, "two.mp4", passes=1)

    show_videos(window)
    assert sorted(listed_names(window)) == ["one.mp4", "two.mp4"]


def test_outcome_reads_what_is_still_owed(window):
    store = window._survey_store()
    _seed(store, "untouched.mp4")
    _seed(store, "part.mp4", passes=2, statuses=("succeeded",))
    _seed(store, "broken.mp4", passes=1, statuses=("failed",))
    _seed(store, "done.mp4", passes=1, statuses=("succeeded",))

    show_videos(window)
    outcomes = {c.video.file_name: c.outcome for c in window._video_entries}
    assert outcomes == {
        "untouched.mp4": VIDEO_UNPROCESSED,
        "part.mp4": VIDEO_PENDING,
        "broken.mp4": VIDEO_FAILED,
        "done.mp4": VIDEO_PROCESSED,
    }


def test_filters_and_search_narrow_the_list(window):
    store = window._survey_store()
    _seed(store, "alpha.mp4")
    _seed(store, "beta.mp4", passes=1, statuses=("succeeded",))

    show_videos(window)
    window._on_video_filter_changed(VIDEO_PROCESSED)
    assert listed_names(window) == ["beta.mp4"]

    window._on_video_filter_changed("all")
    window._video_search.setText("alph")
    assert listed_names(window) == ["alpha.mp4"]


def test_an_empty_library_says_so(window):
    show_videos(window)
    assert window._video_stack.currentIndex() == 1


def test_clips_are_grouped_by_the_day_they_were_shot(window):
    store = window._survey_store()
    store.upsert_video(
        VideoAsset(
            file_name="may.mp4",
            path="/data/may.mp4",
            hash="a" * 32,
            captured_at="2023-05-17T08:27:16+00:00",
        )
    )
    store.upsert_video(
        VideoAsset(
            file_name="june.mp4",
            path="/data/june.mp4",
            hash="b" * 32,
            captured_at="2023-06-02T09:00:00+00:00",
        )
    )

    show_videos(window)
    from deepreefmap_gui.survey.video_groups import group_by_period

    groups = group_by_period(window._video_entries, window._video_period)
    assert [g.entries[0].video.file_name for g in groups] == ["june.mp4", "may.mp4"]


def test_the_grouping_period_is_remembered(window):
    show_videos(window)
    window._on_video_period_changed("month")

    assert window._video_period == "month"
    assert window._settings.value("video_group_period") == "month"


def test_link_state_says_whether_the_footage_is_still_there(window, tmp_path):
    """Scenario: footage lives on a card that gets pulled between dives.

    Expected behaviour: the clip stays, and says it cannot be found rather than
    disappearing or claiming to be fine.
    """
    store = window._survey_store()
    present = tmp_path / "here.mp4"
    present.write_bytes(b"x" * 32)
    _seed_at(store, "here.mp4", present)
    _seed_at(store, "gone.mp4", tmp_path / "gone.mp4")

    show_videos(window)
    resolve_links(window)
    states = {c.video.file_name: c.link_state for c in window._video_entries}
    assert states == {"here.mp4": "linked", "gone.mp4": "missing"}


def test_missing_footage_is_reported_in_the_header(window, tmp_path):
    store = window._survey_store()
    _seed_at(store, "gone.mp4", tmp_path / "gone.mp4")
    show_videos(window)
    resolve_links(window)

    verdict = window._videos_verdict()
    assert verdict.state == "attention"
    assert "1 clip" in verdict.reason


def test_a_missing_clip_offers_to_be_relocated(window, tmp_path):
    store = window._survey_store()
    _seed_at(store, "gone.mp4", tmp_path / "gone.mp4")
    show_videos(window)
    resolve_links(window)

    select_clip(window, "gone.mp4")
    assert window._video_detail.relocate_btn.isVisibleTo(window._video_detail)


def test_relocating_refuses_footage_that_is_not_the_same_recording(window, tmp_path, monkeypatch):
    """A GoPro names every card's first clip GX010001.MP4, so the name proves
    nothing and the checksum has to be what decides."""
    from PySide6.QtWidgets import QMessageBox

    store = window._survey_store()
    original = _seed_at(store, "gone.mp4", tmp_path / "gone.mp4")
    other = tmp_path / "different.mp4"
    other.write_bytes(b"different footage" * 64)

    monkeypatch.setattr(
        "deepreefmap_gui.runs.videos.QFileDialog.getOpenFileName",
        staticmethod(lambda *a, **k: (str(other), "")),
    )
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: warned.append(a)))

    show_videos(window)
    resolve_links(window)
    select_clip(window, "gone.mp4")
    window._on_video_relocate()

    assert warned, "the mismatch has to be reported, not silently ignored"
    assert store.get_video(original.id).path == str(tmp_path / "gone.mp4")


def test_relocating_repoints_the_clip_when_the_checksum_agrees(window, tmp_path, monkeypatch):
    store = window._survey_store()
    moved = tmp_path / "new_home" / "GX010001.MP4"
    moved.parent.mkdir()
    moved.write_bytes(b"the same footage" * 64)
    real_hash = VideoAsset.from_path(moved).hash
    original = store.upsert_video(
        VideoAsset(file_name="GX010001.MP4", path=str(tmp_path / "gone.mp4"), hash=real_hash)
    )

    monkeypatch.setattr(
        "deepreefmap_gui.runs.videos.QFileDialog.getOpenFileName",
        staticmethod(lambda *a, **k: (str(moved), "")),
    )
    show_videos(window)
    resolve_links(window)
    select_clip(window, "GX010001.MP4")
    window._on_video_relocate()

    assert store.get_video(original.id).path == str(moved)


def test_revealing_a_clip_selects_the_file_itself(window, tmp_path, monkeypatch):
    """The folder was never the point: a card holds hundreds of clips."""
    store = window._survey_store()
    clip = tmp_path / "here.mp4"
    clip.write_bytes(b"x" * 32)
    _seed_at(store, "here.mp4", clip)

    revealed = []
    monkeypatch.setattr(
        "deepreefmap_gui.runs.videos.reveal_in_file_manager",
        lambda path: revealed.append(path) or True,
    )
    show_videos(window)
    entry = next(c for c in window._video_entries if c.video.file_name == "here.mp4")
    window._on_video_reveal(str(entry.video.id))

    assert revealed == [clip]


def test_a_new_section_is_cut_scrubbed_assigned_and_carted(window, tmp_path, monkeypatch):
    """The whole reason the page exists: footage in, a queued section out."""
    from PySide6.QtWidgets import QDialog

    store = window._survey_store()
    clip_path = write_test_mp4(tmp_path / "GX010099.MP4")
    _seed_at(store, "GX010099.MP4", clip_path)
    transect = make_transect("Reef A")
    store.add_transect(transect)

    monkeypatch.setattr(
        "deepreefmap_gui.form.video_scrub.VideoScrubDialog.exec",
        lambda self: QDialog.DialogCode.Accepted,
    )
    monkeypatch.setattr(
        "deepreefmap_gui.form.video_scrub.VideoScrubDialog.time_range",
        lambda self: (5.0, 25.0),
    )
    monkeypatch.setattr(
        "deepreefmap_gui.simple.section_dialog.SectionAssignDialog.exec",
        lambda self: QDialog.DialogCode.Accepted,
    )
    monkeypatch.setattr(
        "deepreefmap_gui.simple.section_dialog.SectionAssignDialog.choice",
        lambda self: (transect.id, "forward"),
    )

    show_videos(window)
    resolve_links(window)
    select_clip(window, "GX010099.MP4")
    window._on_video_new_section()

    passes = store.list_passes()
    assert [(p.begin_s, p.end_s, p.transect_id) for p in passes] == [(5.0, 25.0, transect.id)]
    assert window._pass_in_current_cart(passes[0].id)


def _seed_run(store, pass_, status: str = "succeeded", batch=None):
    from deepreefmap_gui.survey.models import RunRecord

    run = RunRecord(
        pass_id=pass_.id,
        run_dir_name=f"run_{status}_{pass_.begin_s:g}",
        status=status,
        batch_id=batch.id if batch is not None else None,
    )
    store.add_run(run)
    return run


def _cut(store, video, begin=0.0, end=30.0, transect=None):
    from deepreefmap_gui.survey.models import TransectPass

    pass_ = TransectPass(
        transect_id=transect.id if transect is not None else None,
        video_id=video.id,
        begin_s=begin,
        end_s=end,
    )
    store.add_pass(pass_)
    return pass_


def test_a_section_shows_the_sessions_it_has_run_in(window):
    """Scenario: the clip pane used to read "Evan #1 · forwar…14 · interrupted",
    which is a cut and a run's outcome on one line.

    Expected behaviour: the cut is its own thing, and picking it says which
    sessions have run it.
    """
    from _factories import make_batch

    store = window._survey_store()
    video = _seed(store, "GX010050.MP4")
    pass_ = _cut(store, video)
    batch = make_batch(store, "2026-07-21")
    _seed_run(store, pass_, "succeeded", batch)

    show_videos(window)
    window._select_section(str(pass_.id))

    assert not window._section_detail.isHidden()
    assert window._section_detail.pass_ is not None
    assert window._section_detail.run_list.count() == 1
    assert "2026-07-21" in window._section_detail.run_list.item(0).text()


def test_picking_a_section_keeps_its_clip_on_screen(window):
    """The cut list stays visible beside the runs, which is the drill-down."""
    store = window._survey_store()
    video = _seed(store, "GX010051.MP4")
    pass_ = _cut(store, video)

    show_videos(window)
    window._select_section(str(pass_.id))

    assert not window._video_detail.isHidden()
    assert window._video_detail.entry.video.file_name == "GX010051.MP4"


def test_a_section_with_runs_refuses_to_be_deleted(window):
    """It records what those runs processed, so it cannot go out from under them."""
    store = window._survey_store()
    video = _seed(store, "GX010052.MP4")
    pass_ = _cut(store, video)
    _seed_run(store, pass_)

    show_videos(window)
    window._on_section_delete(str(pass_.id))

    assert store.get_pass(pass_.id) is not None
    assert "Browse" in window._status_label.text()


def test_a_section_nothing_was_made_from_can_be_deleted(window, monkeypatch):
    store = window._survey_store()
    video = _seed(store, "GX010053.MP4")
    pass_ = _cut(store, video)
    monkeypatch.setattr("deepreefmap_gui.runs.videos.confirm", lambda *a, **k: True)

    show_videos(window)
    window._on_section_delete(str(pass_.id))

    assert store.get_pass(pass_.id) is None


def test_retrimming_writes_the_new_window_back(window, tmp_path, monkeypatch):
    """The trim is metadata, so it stays editable after a section has been run."""
    from PySide6.QtWidgets import QDialog

    store = window._survey_store()
    clip_path = write_test_mp4(tmp_path / "GX010054.MP4")
    video = _seed_at(store, "GX010054.MP4", clip_path)
    pass_ = _cut(store, video, 0.0, 30.0)
    _seed_run(store, pass_)

    monkeypatch.setattr(
        "deepreefmap_gui.form.video_scrub.VideoScrubDialog.exec",
        lambda self: QDialog.DialogCode.Accepted,
    )
    monkeypatch.setattr(
        "deepreefmap_gui.form.video_scrub.VideoScrubDialog.time_range",
        lambda self: (4.0, 9.0),
    )
    show_videos(window)
    resolve_links(window)
    window._on_section_retrim(str(pass_.id))

    moved = store.get_pass(pass_.id)
    assert (moved.begin_s, moved.end_s) == (4.0, 9.0)


def test_reassigning_moves_the_section_to_another_transect(window, monkeypatch):
    from PySide6.QtWidgets import QDialog

    store = window._survey_store()
    video = _seed(store, "GX010055.MP4")
    pass_ = _cut(store, video)
    other = make_transect("Reef B")
    store.add_transect(other)

    monkeypatch.setattr(
        "deepreefmap_gui.simple.section_dialog.SectionAssignDialog.exec",
        lambda self: QDialog.DialogCode.Accepted,
    )
    monkeypatch.setattr(
        "deepreefmap_gui.simple.section_dialog.SectionAssignDialog.choice",
        lambda self: (other.id, "reverse"),
    )
    show_videos(window)
    window._on_section_reassign(str(pass_.id))

    moved = store.get_pass(pass_.id)
    assert moved.transect_id == other.id
    assert moved.direction == "reverse"


def test_dropping_a_clip_on_the_list_imports_it(window, tmp_path, monkeypatch):
    """Nothing else on the page advertises this, so it had better work."""
    clip = write_test_mp4(tmp_path / "GX010056.MP4")
    added = []
    monkeypatch.setattr(window, "_add_video_paths", added.extend)

    window._handle_data_drop([clip])
    assert added == [str(clip)]
