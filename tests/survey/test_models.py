import uuid

import pytest
from _factories import make_transect, make_video

from deepreefmap_gui.survey.models import RunRecord, SurveyBatch, TransectPass
from deepreefmap_gui.survey.models.convert import (
    build_document,
    from_row,
    parse_document,
    survey_manifest_block,
    to_row,
)


def make_pass(transect, video, **overrides):
    values = {"transect_id": transect.id, "video_id": video.id, "begin_s": 0.0, "end_s": 60.0}
    values.update(overrides)
    return TransectPass(**values)


def test_transect_rejects_bad_coordinates():
    with pytest.raises(ValueError):
        make_transect(start_lat=91.0)
    with pytest.raises(ValueError):
        make_transect(end_lon=-181.0)


def test_transect_rejects_empty_name_and_negative_lengths():
    with pytest.raises(ValueError):
        make_transect(name="  ")
    with pytest.raises(ValueError):
        make_transect(length_m=-1.0)
    with pytest.raises(ValueError):
        make_transect(depth_m=-0.5)


def test_geodesic_length_close_to_metric():
    # 0.001 degrees of longitude at the equator is ~111.2 m.
    t = make_transect(start_lat=0.0, start_lon=0.0, end_lat=0.0, end_lon=0.001)
    assert 110.0 < t.geodesic_length_m() < 112.0


def test_pass_rejects_bad_trim_and_direction():
    transect, video = make_transect(), make_video()
    with pytest.raises(ValueError):
        make_pass(transect, video, begin_s=-1.0)
    with pytest.raises(ValueError):
        make_pass(transect, video, begin_s=30.0, end_s=30.0)
    with pytest.raises(ValueError):
        make_pass(transect, video, direction="sideways")


def test_pass_duration_is_the_trimmed_window():
    """What the ETA and the repeatability stats divide by."""
    transect, video = make_transect(), make_video()
    assert make_pass(transect, video, begin_s=12.5, end_s=42.5).duration_s() == 30.0


def test_batch_rejects_a_name_that_is_only_whitespace():
    """Batches are addressed by name in the queue and in batch_out/<name>/."""
    SurveyBatch(name=" Day 1 ")  # padding is fine, emptiness is not
    with pytest.raises(ValueError):
        SurveyBatch(name="   ")


def test_run_record_rejects_unknown_status():
    with pytest.raises(ValueError):
        RunRecord(pass_id=uuid.uuid4(), run_dir_name="run", status="exploded")
    with pytest.raises(ValueError):
        RunRecord(pass_id=uuid.uuid4(), run_dir_name=" ")


def test_row_round_trip_preserves_every_model():
    transect, video = make_transect(), make_video()
    batch = SurveyBatch(name="Day 1")
    pass_ = make_pass(transect, video, batch_id=batch.id, direction="reverse")
    run = RunRecord(pass_id=pass_.id, run_dir_name="t1__p01__20260720-0900")
    for model in (transect, video, batch, pass_, run):
        row = to_row(model)
        assert all(not isinstance(v, uuid.UUID) for v in row.values())
        assert from_row(type(model), row) == model


def test_document_round_trip():
    transect, video = make_transect(), make_video()
    batch = SurveyBatch(name="Day 1")
    pass_ = make_pass(transect, video, batch_id=batch.id)
    run = RunRecord(pass_id=pass_.id, run_dir_name="run")
    doc = build_document(
        transects=[transect], videos=[video], batches=[batch], passes=[pass_], runs=[run]
    )
    sections = parse_document(doc)
    assert sections["transects"] == [transect]
    assert sections["videos"] == [video]
    assert sections["batches"] == [batch]
    assert sections["passes"] == [pass_]
    assert sections["runs"] == [run]


def test_document_rejects_unknown_schema_version():
    with pytest.raises(ValueError):
        parse_document({"schema_version": 999})


def test_manifest_block_snapshots_pass_and_transect():
    transect, video = make_transect(), make_video()
    batch = SurveyBatch(name="Day 1")
    pass_ = make_pass(transect, video, batch_id=batch.id, direction="reverse")
    run = RunRecord(pass_id=pass_.id, run_dir_name="run")
    block = survey_manifest_block(run, pass_, transect, batch)
    assert block["run_id"] == str(run.id)
    assert block["batch_name"] == "Day 1"
    assert block["pass"] == {
        "id": str(pass_.id),
        "direction": "reverse",
        "begin_s": 0.0,
        "end_s": 60.0,
    }
    assert block["transect"]["name"] == "T1"
    assert block["transect"]["start_lat"] == -17.5
    assert survey_manifest_block(run, pass_, transect, None)["batch_id"] is None


def test_manifest_block_records_model_versions():
    transect, video = make_transect(), make_video()
    pass_ = make_pass(transect, video)
    run = RunRecord(pass_id=pass_.id, run_dir_name="run")
    versions = {"EPFL-ECEO/segformer-b5-finetuned-coralscapes-1024-1024": "a" * 40}

    recorded = survey_manifest_block(run, pass_, transect, None, model_versions=versions)
    assert recorded["provenance"]["model_versions"] == versions

    # Omitted, not written empty, when nothing was resolved.
    plain = survey_manifest_block(run, pass_, transect, None)
    assert "model_versions" not in plain["provenance"]


def test_pass_chapters_read_in_playing_order():
    """A GoPro splits a long swim at about 4 GB, so a pass may name several files."""
    transect, video = make_transect(), make_video()
    second = make_video("cd" * 16, file_name="GX020001.MP4")
    pass_ = make_pass(transect, video, extra_video_ids=[second.id], end_s=600.0)
    assert pass_.video_ids() == [video.id, second.id]

    row = to_row(pass_)
    # The chapters share one sqlite column, so they travel as a JSON array.
    assert isinstance(row["extra_video_ids"], str)
    assert from_row(TransectPass, row) == pass_


def test_pass_rejects_a_chapter_that_repeats_its_first_video():
    transect, video = make_transect(), make_video()
    with pytest.raises(ValueError):
        make_pass(transect, video, extra_video_ids=[video.id])


def test_a_document_written_before_chapters_still_parses():
    """A survey exported by an earlier build has no extra_video_ids to read."""
    transect, video = make_transect(), make_video()
    pass_ = make_pass(transect, video)
    doc = build_document(
        transects=[transect], videos=[video], batches=[], passes=[pass_], runs=[]
    )
    for row in doc["passes"]:
        del row["extra_video_ids"]
    assert parse_document(doc)["passes"] == [pass_]
