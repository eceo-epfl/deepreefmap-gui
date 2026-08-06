"""Scenario: a dive's footage arrives as a card, or a folder of dated folders.

Expected behaviour: dropping the top of it finds every clip underneath, and
nothing that only looks like one.
"""

from deepreefmap_gui.io.video_files import RUN_MANIFEST_NAME, find_videos, is_run_dir, is_video


def _clip(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"footage")
    return path


def test_a_dropped_file_is_taken_as_it_is(tmp_path):
    clip = _clip(tmp_path / "GX010001.MP4")
    assert find_videos([clip]) == ([clip], False)


def test_a_folder_is_walked_to_the_bottom(tmp_path):
    """Footage comes off a card as dated folders of dated folders."""
    deep = _clip(tmp_path / "2024_08" / "Sunset" / "cam1" / "GX010028.MP4")
    shallow = _clip(tmp_path / "2024_08" / "GX010002.MP4")

    found, truncated = find_videos([tmp_path])
    assert set(found) == {deep, shallow}
    assert not truncated


def test_macos_sidecars_are_not_footage(tmp_path):
    """A card copied on macOS carries a ``._NAME.MP4`` beside every file.

    They have the suffix and a few KB of resource fork, so taking them at their
    name imports a library of clips that will not open.
    """
    real = _clip(tmp_path / "GX010004.MP4")
    _clip(tmp_path / "._GX010004.MP4")

    assert find_videos([tmp_path]) == ([real], False)


def test_files_that_are_not_video_are_left_alone(tmp_path):
    _clip(tmp_path / "notes.txt")
    _clip(tmp_path / "results.csv")
    assert find_videos([tmp_path]) == ([], False)


def test_a_run_directory_holds_no_footage(tmp_path):
    """An output root sits beside the videos often enough to be dropped by mistake."""
    run = tmp_path / "a_run"
    run.mkdir()
    (run / RUN_MANIFEST_NAME).write_text("{}")
    _clip(run / "preview.mp4")

    assert is_run_dir(run)
    assert find_videos([tmp_path]) == ([], False)


def test_the_same_clip_dropped_twice_arrives_once(tmp_path):
    clip = _clip(tmp_path / "GX010001.MP4")
    found, _ = find_videos([clip, tmp_path])
    assert found == [clip]


def test_a_folder_that_cannot_be_read_is_skipped(tmp_path):
    assert find_videos([tmp_path / "never_existed"]) == ([], False)


def test_suffixes_are_matched_whatever_their_case(tmp_path):
    assert is_video(tmp_path / "GX010001.MP4")
    assert is_video(tmp_path / "clip.mov")
    assert not is_video(tmp_path / "clip.png")
