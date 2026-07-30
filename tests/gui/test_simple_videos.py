"""The Videos workspace: what footage exists, and what became of it."""

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


def test_outcome_reads_what_is_still_owed(simple_window):
    """A clip is only done once every pass cut from it has succeeded."""
    store = simple_window._survey_store()
    _seed(store, "orphan.mp4")
    _seed(store, "half.mp4", passes=2, statuses=("succeeded",))
    _seed(store, "broken.mp4", passes=1, statuses=("failed",))
    _seed(store, "done.mp4", passes=2, statuses=("succeeded", "succeeded"))

    assert _entry(store, "orphan.mp4").outcome == VIDEO_UNPROCESSED
    assert _entry(store, "half.mp4").outcome == VIDEO_PENDING
    assert _entry(store, "broken.mp4").outcome == VIDEO_FAILED
    assert _entry(store, "done.mp4").outcome == VIDEO_PROCESSED


def test_filters_and_search_narrow_the_clip_list(simple_window):
    window = simple_window
    store = window._survey_store()
    _seed(store, "orphan.mp4")
    _seed(store, "done.mp4", passes=1, statuses=("succeeded",))
    window._refresh_videos_page()

    assert window._video_list.count() == 2
    assert window._video_chips._buttons[VIDEO_UNPROCESSED].text().endswith("1")

    window._video_chips.set_current(VIDEO_UNPROCESSED)
    assert window._video_list.count() == 1
    assert window._video_list.item(0).text().startswith("orphan.mp4")

    window._video_chips.set_current("all")
    window._video_search.setText("done")
    assert window._video_list.count() == 1
    assert window._video_list.item(0).text().startswith("done.mp4")


def test_detail_lists_the_passes_cut_from_a_clip(simple_window):
    window = simple_window
    store = window._survey_store()
    _seed(store, "clip.mp4", passes=2, statuses=("succeeded", "failed"))
    window._refresh_videos_page()
    window._video_list.setCurrentRow(0)

    rows = [
        window._video_pass_list.item(i).text()
        for i in range(window._video_pass_list.count())
    ]
    assert len(rows) == 2
    assert any("succeeded" in row for row in rows)
    assert any("failed" in row for row in rows)
    assert "clip.mp4" in window._video_detail_name.text()


def test_empty_library_says_where_clips_come_from(simple_window):
    window = simple_window
    window._refresh_videos_page()
    assert window._video_stack.currentIndex() == 1
    assert window._video_queue_btn.isEnabled() is False


def test_show_in_folder_opens_the_parent(simple_window, monkeypatch):
    window = simple_window
    store = window._survey_store()
    _seed(store, "clip.mp4")
    window._refresh_videos_page()
    window._video_list.setCurrentRow(0)
    opened = []
    monkeypatch.setattr(
        "deepreefmap_gui.simple.videos.QDesktopServices.openUrl",
        staticmethod(lambda url: opened.append(url.toLocalFile())),
    )
    window._on_video_show_clicked()
    assert opened == [str(Path("/data"))]
