"""Browse grouped By video: what footage exists, and what became of it.

The clip library used to be a workspace of its own. It is now this grouping's
rail and detail pane, so these tests reach it through Browse.
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
    # The trailing count is runs, and none have been cut from it yet.
    assert clip_titles(window) == ["orphan.mp4 · #ffffffff  (0)"]


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
        "deepreefmap_gui.runs.data_manager.QDesktopServices.openUrl",
        staticmethod(lambda url: opened.append(url.toLocalFile())),
    )
    window._on_data_show_clip_folder()
    assert opened == [str(Path("/data"))]
