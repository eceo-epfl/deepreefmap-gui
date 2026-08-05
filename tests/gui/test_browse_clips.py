"""Browse grouped By video: what footage exists, and what became of it.

The clip library is this grouping's rail and detail pane, so these tests reach
it through Browse.
"""

from pathlib import Path

from _factories import make_transect

from deepreefmap_gui.survey.catalogue import (
    VIDEO_FAILED,
    VIDEO_PENDING,
    VIDEO_PROCESSED,
    VIDEO_UNPROCESSED,
    video_library,
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


def _entry(store, file_name):
    entries = video_library(store.list_videos(), store.list_passes(), store.list_runs())
    return next(e for e in entries if e.video.file_name == file_name)


def show_clips(window) -> None:
    """Re-read the library, then group Browse by video."""
    window._refresh_data_manager()
    window._data_facet_buttons["videos"].click()


def clip_titles(window) -> list[str]:
    tree = window._data_tree
    return [tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())]


def select_clip(window, file_name: str) -> None:
    tree = window._data_tree
    for index in range(tree.topLevelItemCount()):
        item = tree.topLevelItem(index)
        if item.text(0).startswith(file_name):
            tree.setCurrentItem(item)
            return
    raise AssertionError(f"{file_name} is not listed: {clip_titles(window)}")


def test_outcome_reads_what_is_still_owed(window):
    """A clip is only done once every pass cut from it has succeeded."""
    store = window._survey_store()
    _seed(store, "orphan.mp4")
    _seed(store, "half.mp4", passes=2, statuses=("succeeded",))
    _seed(store, "broken.mp4", passes=1, statuses=("failed",))
    _seed(store, "done.mp4", passes=2, statuses=("succeeded", "succeeded"))

    assert _entry(store, "orphan.mp4").outcome == VIDEO_UNPROCESSED
    assert _entry(store, "half.mp4").outcome == VIDEO_PENDING
    assert _entry(store, "broken.mp4").outcome == VIDEO_FAILED
    assert _entry(store, "done.mp4").outcome == VIDEO_PROCESSED


def test_outcome_chips_only_appear_under_the_clip_grouping(window):
    """They filter clips, so they say nothing while the rail lists transects."""
    _seed(window._survey_store(), "orphan.mp4")
    show_clips(window)
    assert window._data_clip_chips.isVisibleTo(window._data_panel)

    window._data_facet_buttons["transects"].click()
    assert not window._data_clip_chips.isVisibleTo(window._data_panel)


def test_filters_and_search_narrow_the_clip_list(window):
    store = window._survey_store()
    _seed(store, "orphan.mp4")
    _seed(store, "done.mp4", passes=1, statuses=("succeeded",))
    show_clips(window)

    assert len(clip_titles(window)) == 2
    assert window._data_clip_chips._buttons[VIDEO_UNPROCESSED].text().endswith("1")

    window._data_clip_chips.set_current(VIDEO_UNPROCESSED)
    assert clip_titles(window) == [t for t in clip_titles(window) if t.startswith("orphan.mp4")]
    assert len(clip_titles(window)) == 1

    window._data_clip_chips.set_current("all")
    window._data_search.setText("done")
    window._rebuild_data_tree()
    assert len(clip_titles(window)) == 1
    assert clip_titles(window)[0].startswith("done.mp4")


def test_detail_lists_the_passes_cut_from_a_clip(window):
    store = window._survey_store()
    _seed(store, "clip.mp4", passes=2, statuses=("succeeded", "failed"))
    show_clips(window)
    select_clip(window, "clip.mp4")

    detail = window._video_detail
    rows = [detail.pass_list.item(i).text() for i in range(detail.pass_list.count())]
    assert len(rows) == 2
    assert any("succeeded" in row for row in rows)
    assert any("failed" in row for row in rows)
    assert "clip.mp4" in detail.title.text()


def test_a_clip_nobody_processed_is_still_listed(out_root, make_window):
    """Scenario: a card is copied off the camera and nothing is run from it.

    Expected behaviour: it appears under By video. The grouping is built from
    runs, so without the library behind it the one clip that most needs doing
    would be the one clip that never showed up.
    """
    from deepreefmap_gui.survey.store import SurveyStore

    out_root.mkdir(parents=True)
    store = SurveyStore(out_root / "survey.db")
    store.upsert_video(VideoAsset(file_name="orphan.mp4", path="/data/orphan.mp4", hash="ff" * 16))
    store.close()

    window = make_window()
    show_clips(window)
    # The trailing count is runs, and none have been cut from it yet. No
    # checksum: nothing else is called orphan.mp4, so there is nothing to tell
    # it apart from.
    assert clip_titles(window) == ["orphan.mp4  (0)"]


def test_clips_sharing_a_file_name_are_told_apart_by_checksum(out_root, make_window):
    """Scenario: two cards each hold their own GX010001.MP4.

    Expected behaviour: both rows carry a checksum. The file name is a weak
    identity, so it is only worth spending rail width on the hash at the point
    two clips actually collide on it.
    """
    from deepreefmap_gui.survey.store import SurveyStore

    out_root.mkdir(parents=True)
    store = SurveyStore(out_root / "survey.db")
    for digest in ("aa" * 16, "bb" * 16):
        store.upsert_video(
            VideoAsset(file_name="GX010001.MP4", path=f"/data/{digest}.MP4", hash=digest)
        )
    store.close()

    window = make_window()
    show_clips(window)
    assert clip_titles(window) == [
        "GX010001.MP4 · #aaaaaaaa  (0)",
        "GX010001.MP4 · #bbbbbbbb  (0)",
    ]


def test_empty_library_says_nothing_is_grouped(window):
    show_clips(window)
    assert clip_titles(window) == []
    assert window._data_detail_stack.currentIndex() == 0


def test_show_in_folder_opens_the_parent(window, monkeypatch):
    _seed(window._survey_store(), "clip.mp4")
    show_clips(window)
    select_clip(window, "clip.mp4")

    opened = []
    monkeypatch.setattr(
        "deepreefmap_gui.runs.browse.QDesktopServices.openUrl",
        staticmethod(lambda url: opened.append(url.toLocalFile())),
    )
    window._on_data_show_clip_folder()
    assert opened == [str(Path("/data"))]


def _seed_at(store, name: str, path: Path) -> VideoAsset:
    """A clip pointing at a real file, so its link state can be resolved."""
    return store.upsert_video(
        VideoAsset(file_name=name, path=str(path), hash=name * 4, duration_s=60.0)
    )


def resolve_links(window) -> None:
    """Run the link scan to completion. It is threaded in the app; here the
    states are computed directly so the test does not race a worker."""
    from deepreefmap_gui.survey.catalogue import resolve_link_states

    window._apply_clip_link_states(resolve_link_states(window._video_entries))


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

    show_clips(window)
    resolve_links(window)
    states = {c.video.file_name: c.link_state for c in window._video_entries}
    assert states == {"here.mp4": "linked", "gone.mp4": "missing"}


def test_a_missing_clip_offers_to_be_relocated(window, tmp_path):
    store = window._survey_store()
    _seed_at(store, "gone.mp4", tmp_path / "gone.mp4")
    show_clips(window)
    resolve_links(window)

    select_clip(window, "gone.mp4")
    assert window._video_detail.relocate_btn.isVisibleTo(window._video_detail)
    # Nothing to decode, so previewing is not on offer.
    assert not window._video_detail.preview_btn.isEnabled()


def test_relocating_refuses_footage_that_is_not_the_same_recording(
    window, tmp_path, monkeypatch
):
    """A GoPro names every card's first clip GX010001.MP4, so the name proves
    nothing and the checksum has to be what decides."""
    from PySide6.QtWidgets import QMessageBox

    store = window._survey_store()
    original = _seed_at(store, "gone.mp4", tmp_path / "gone.mp4")
    other = tmp_path / "different.mp4"
    other.write_bytes(b"different footage" * 64)

    monkeypatch.setattr(
        "deepreefmap_gui.runs.browse.QFileDialog.getOpenFileName",
        staticmethod(lambda *a, **k: (str(other), "")),
    )
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: warned.append(a)))

    show_clips(window)
    resolve_links(window)
    select_clip(window, "gone.mp4")
    window._on_data_relocate_clip()

    assert warned, "the mismatch has to be reported, not silently ignored"
    assert store.get_video(original.id).path == str(tmp_path / "gone.mp4")


def test_relocating_repoints_the_clip_when_the_checksum_agrees(
    window, tmp_path, monkeypatch
):
    from deepreefmap_gui.survey.models.video_asset import VideoAsset as Asset

    store = window._survey_store()
    moved = tmp_path / "new_home" / "GX010001.MP4"
    moved.parent.mkdir()
    moved.write_bytes(b"the same footage" * 64)
    # Recorded under its real checksum, at a path it has since left.
    real_hash = Asset.from_path(moved).hash
    original = store.upsert_video(
        VideoAsset(file_name="GX010001.MP4", path=str(tmp_path / "gone.mp4"), hash=real_hash)
    )

    monkeypatch.setattr(
        "deepreefmap_gui.runs.browse.QFileDialog.getOpenFileName",
        staticmethod(lambda *a, **k: (str(moved), "")),
    )
    show_clips(window)
    resolve_links(window)
    select_clip(window, "GX010001.MP4")
    window._on_data_relocate_clip()

    assert store.get_video(original.id).path == str(moved)
