"""The Videos page's row widgets: the section strip, a clip row, a section row,
and the list they sit in."""

from __future__ import annotations

import uuid

import pytest
from _factories import make_video

from deepreefmap_gui.core.theme import SPACE_SM
from deepreefmap_gui.runs.video_rows import (
    DELETE_BLOCKED_TOOLTIP,
    DROP_HINT,
    GRAVITY_UNKNOWN_TOOLTIP,
    MENU_ADD_TO_CART,
    MENU_DELETE,
    MENU_DELETE_UNUSED,
    MENU_HIDE,
    MENU_REASSIGN,
    MENU_RETRIM,
    MENU_UNHIDE,
    NO_SECTIONS_TOOLTIP,
    SORT_ASC_GLYPH,
    SORT_DESC_GLYPH,
    UNKNOWN_LENGTH_TOOLTIP,
    SectionRow,
    SectionStrip,
    VideoLibraryList,
    VideoListHeader,
    VideoRow,
    capture_label,
)
from deepreefmap_gui.survey.catalogue import LINK_LINKED, VideoLibraryEntry
from deepreefmap_gui.survey.models.run_record import RunRecord
from deepreefmap_gui.survey.models.transect_pass import TransectPass
from deepreefmap_gui.survey.video_groups import (
    SORT_GRAVITY,
    SORT_NAME,
    SORT_SIZE,
    DateGroup,
    sort_clips,
    sort_groups,
    timeline_spans,
)
from deepreefmap_gui.survey.video_probe import NO, SOURCE_CONTAINER, SOURCE_MTIME, UNKNOWN, YES

STRIP_WIDTH = 400

pytestmark = pytest.mark.usefixtures("qapp")


def make_entry(*, windows=((0.0, 30.0),), runs_per_pass=(), duration_s=120.0, asset=None, **video):
    """One clip with a section per window, and however many runs each has had.

    ``asset`` reuses an existing clip, so a later entry can stand for the same
    footage after another section has been cut from it.
    """
    asset = asset if asset is not None else make_video(duration_s=duration_s, **video)
    passes = [
        TransectPass(transect_id=None, video_id=asset.id, begin_s=begin, end_s=end)
        for begin, end in windows
    ]
    runs = []
    # Not strict: a clip's later sections may have had no runs at all.
    for pass_, statuses in zip(passes, runs_per_pass, strict=False):
        runs.extend(
            RunRecord(pass_id=pass_.id, run_dir_name=f"run{n}", status=status)
            for n, status in enumerate(statuses)
        )
    return VideoLibraryEntry(
        video=asset,
        pass_count=len(passes),
        run_count=len(runs),
        passes=passes,
        runs=runs,
        link_state=LINK_LINKED,
    )


def make_strip(entry) -> SectionStrip:
    strip = SectionStrip()
    strip.resize(STRIP_WIDTH, strip.height())
    strip.set_spans(timeline_spans(entry), entry.video.duration_s)
    return strip


def no_name(_transect_id) -> str | None:
    return None


def test_spans_land_in_time_order_along_the_strip() -> None:
    entry = make_entry(windows=((60.0, 90.0), (0.0, 30.0)))
    strip = make_strip(entry)

    assert [span.pass_id for span in strip.spans] == [
        str(entry.passes[1].id),
        str(entry.passes[0].id),
    ]


def test_span_at_answers_with_the_pass_under_that_point() -> None:
    entry = make_entry(windows=((0.0, 30.0), (60.0, 90.0)))
    first, second = (str(p.id) for p in entry.passes)
    strip = make_strip(entry)

    # A quarter along is inside the first section, five eighths inside the
    # second, and the gap between them is bare groove.
    assert strip.span_at(STRIP_WIDTH * 0.125) == first
    assert strip.span_at(STRIP_WIDTH * 0.625) == second
    assert strip.span_at(STRIP_WIDTH * 0.45) is None


def test_a_clip_nothing_has_been_cut_from_says_so_rather_than_sitting_empty() -> None:
    entry = make_entry(windows=())
    strip = make_strip(entry)

    assert strip.spans == []
    assert strip.toolTip() == NO_SECTIONS_TOOLTIP
    strip.grab()


def test_a_clip_of_unknown_length_gets_no_spans_and_says_so() -> None:
    """Expected behaviour: no section is invented from a length nobody read."""
    entry = make_entry(duration_s=None)
    strip = make_strip(entry)

    assert strip.spans == []
    assert strip.toolTip() == UNKNOWN_LENGTH_TOOLTIP
    strip.grab()


def test_clicking_a_span_emits_its_pass_and_bare_groove_emits_nothing() -> None:
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    entry = make_entry(windows=((0.0, 30.0),))
    strip = make_strip(entry)
    seen: list[str] = []
    strip.span_clicked.connect(seen.append)

    def click(x: float) -> None:
        strip.mousePressEvent(
            QMouseEvent(
                QMouseEvent.Type.MouseButtonPress,
                QPointF(x, strip.height() / 2),
                QPointF(x, strip.height() / 2),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
        )

    click(STRIP_WIDTH * 0.1)
    click(STRIP_WIDTH * 0.9)

    assert seen == [str(entry.passes[0].id)]


def test_a_reprocessed_section_paints_without_running_off_its_own_span() -> None:
    entry = make_entry(
        windows=((0.0, 30.0),), runs_per_pass=(("succeeded", "failed", "succeeded"),)
    )
    strip = make_strip(entry)

    assert strip.spans[0].run_count == 3
    strip.grab()


def test_a_very_short_section_is_still_wide_enough_to_hit() -> None:
    entry = make_entry(windows=((0.0, 0.5),), duration_s=3600.0)
    strip = make_strip(entry)

    assert strip.span_at(SPACE_SM / 2) == str(entry.passes[0].id)


def test_gravity_reads_as_a_fact_only_once_it_has_been_read() -> None:
    """A dot for each of the two answers, and no dot at all for no answer."""
    row = VideoRow()

    row.set_entry(make_entry(gravity=YES), no_name)
    recorded = row.gravity_tooltip
    assert row.gravity_dot is not None

    row.set_entry(make_entry(gravity=NO), no_name)
    assert row.gravity_dot is not None
    assert row.gravity_tooltip != recorded

    row.set_entry(make_entry(gravity=UNKNOWN), no_name)
    assert row.gravity_dot is None
    assert row.gravity_tooltip == GRAVITY_UNKNOWN_TOOLTIP


def test_gravity_sorts_the_clips_without_one_to_the_top() -> None:
    without = make_entry(gravity=NO)
    with_ = make_entry(gravity=YES)
    unread = make_entry(gravity=UNKNOWN)

    ordered = sort_clips([with_, unread, without], SORT_GRAVITY)
    assert [e.video.gravity for e in ordered] == [NO, YES, UNKNOWN]

    # A clip nobody has read has no answer to sort on, so it sinks either way.
    reversed_ = sort_clips([with_, unread, without], SORT_GRAVITY, descending=True)
    assert [e.video.gravity for e in reversed_] == [YES, NO, UNKNOWN]


def test_a_capture_time_standing_in_for_a_missing_one_is_marked() -> None:
    stamp = "2026-07-01T14:32:00+00:00"
    embedded = make_video(captured_at=stamp, captured_source=SOURCE_CONTAINER)
    estimated = make_video(captured_at=stamp, captured_source=SOURCE_MTIME)

    assert not capture_label(embedded).startswith("~")
    assert capture_label(estimated).startswith("~")
    assert capture_label(estimated) == f"~{capture_label(embedded)}"


def test_a_row_carries_the_whole_path_even_when_the_name_is_elided() -> None:
    row = VideoRow()
    entry = make_entry(file_name="GX010001.MP4", path="/very/long/card/path/GX010001.MP4")

    row.set_entry(entry, no_name)

    assert entry.video.path in row.toolTip()
    row.grab()


def test_the_list_keeps_its_rows_when_nothing_about_them_changed() -> None:
    """Expected behaviour: a refresh under the cursor does not replace the row
    being clicked, or scroll the list back to the top."""
    entry = make_entry()
    groups = [DateGroup(key="2026-07-01", title="Today", entries=[entry])]
    listing = VideoLibraryList()

    listing.set_groups(groups, no_name)
    before = listing.rows()[str(entry.video.id)]
    listing.set_groups(groups, no_name)

    assert listing.rows()[str(entry.video.id)] is before


def test_the_list_rebuilds_when_a_clip_arrives() -> None:
    first, second = make_entry(), make_entry()
    listing = VideoLibraryList()

    listing.set_groups([DateGroup(key="d", title="Today", entries=[first])], no_name)
    listing.set_groups(
        [DateGroup(key="d", title="Today", entries=[first, second])], no_name
    )

    assert set(listing.rows()) == {str(first.video.id), str(second.video.id)}


def test_activating_a_row_selects_it_and_reaches_the_list() -> None:
    entry = make_entry()
    listing = VideoLibraryList()
    listing.set_groups([DateGroup(key="d", title="Today", entries=[entry])], no_name)
    seen: list[str] = []
    listing.activated.connect(seen.append)

    listing.rows()[str(entry.video.id)].activated.emit(str(entry.video.id))

    assert seen == [str(entry.video.id)]
    assert listing.selected == str(entry.video.id)


def test_a_span_clicked_on_a_row_reaches_the_list() -> None:
    entry = make_entry()
    listing = VideoLibraryList()
    listing.set_groups([DateGroup(key="d", title="Today", entries=[entry])], no_name)
    seen: list[str] = []
    listing.span_clicked.connect(seen.append)

    listing.rows()[str(entry.video.id)].strip.span_clicked.emit(str(entry.passes[0].id))

    assert seen == [str(entry.passes[0].id)]


def test_a_section_names_its_transect_in_the_hover_text() -> None:
    entry = make_entry(windows=((0.0, 30.0),))
    row = VideoRow()
    row.strip.resize(STRIP_WIDTH, row.strip.height())

    row.set_entry(entry, lambda _id: "North reef")

    assert "North reef" in row.strip._span_tooltip(row.strip.spans[0])


def test_an_unfiled_section_says_so_rather_than_naming_nothing() -> None:
    entry = make_entry(windows=((0.0, 30.0),))
    row = VideoRow()
    row.set_entry(entry, no_name)

    assert "Unassigned" in row.strip._span_tooltip(row.strip.spans[0])


def test_transect_ids_reach_the_resolver_unchanged() -> None:
    asked: list[object] = []
    transect_id = uuid.uuid4()
    entry = make_entry()
    entry.passes[0].transect_id = transect_id
    row = VideoRow()

    row.set_entry(entry, lambda tid: asked.append(tid) or "T1")

    assert asked == [transect_id]


# --- sections under their clip ---------------------------------------------


def one_group(*entries):
    return [DateGroup(key="d", title="Today", entries=list(entries))]


def make_list(*entries, **kwargs) -> VideoLibraryList:
    listing = VideoLibraryList()
    listing.set_groups(one_group(*entries), no_name, **kwargs)
    return listing


def label_texts(widget) -> list[str]:
    from PySide6.QtWidgets import QLabel

    return [label.text() for label in widget.findChildren(QLabel)]


def action(menu, text):
    return next(entry for entry in menu.actions() if entry.text() == text)


def test_sections_stay_hidden_until_their_clip_is_opened() -> None:
    entry = make_entry(windows=((0.0, 30.0), (60.0, 90.0)))
    listing = make_list(entry)
    listing.show()

    assert [row.isVisible() for row in listing.sections().values()] == [False, False]

    listing.expand(str(entry.video.id))

    assert [row.isVisible() for row in listing.sections().values()] == [True, True]


def test_an_open_clip_stays_open_across_a_refresh() -> None:
    """Expected behaviour: a scan lands every few seconds, and a section list
    that shut itself each time could never be read."""
    entry = make_entry(windows=((0.0, 30.0),))
    listing = make_list(entry)
    video_id = str(entry.video.id)
    listing.rows()[video_id].chevron.setChecked(True)
    before = listing.rows()[video_id]

    listing.set_groups(one_group(entry), no_name)

    assert listing.rows()[video_id] is before
    assert listing.rows()[video_id].expanded


def test_a_section_cut_a_moment_ago_gets_a_row_on_the_next_scan() -> None:
    first = make_entry(windows=((0.0, 30.0),))
    listing = make_list(first)
    second = make_entry(windows=((0.0, 30.0), (60.0, 90.0)), asset=first.video)

    listing.set_groups(one_group(second), no_name)

    assert set(listing.sections()) == {str(p.id) for p in second.passes}


def test_a_clip_with_nothing_cut_from_it_keeps_the_space_but_not_the_chevron() -> None:
    """Expected behaviour: the disclosure column holds its width, so the file
    names stay in one column down the list."""
    row = VideoRow()
    row.set_entry(make_entry(windows=()), no_name)

    assert not row.chevron.isEnabled()
    assert row.chevron.icon().isNull()
    assert row.chevron.width() > 0

    row.set_entry(make_entry(windows=((0.0, 30.0),)), no_name)
    assert not row.chevron.icon().isNull()


def test_a_section_row_leads_with_its_window_then_says_where_it_stands() -> None:
    entry = make_entry(windows=((0.0, 30.0),), runs_per_pass=(("succeeded", "succeeded"),))
    listing = VideoLibraryList()
    listing.set_groups(one_group(entry), lambda _id: "North reef")
    row = listing.sections()[str(entry.passes[0].id)]

    texts = label_texts(row)
    assert "0:00–0:30" in texts
    assert "North reef" in texts
    assert "2 runs" in texts
    assert "Forward" in texts
    row.grab()


def test_an_unfiled_section_says_so_rather_than_showing_a_blank() -> None:
    entry = make_entry(windows=((0.0, 30.0),))
    listing = make_list(entry)

    assert "Unassigned" in label_texts(listing.sections()[str(entry.passes[0].id)])


def test_a_section_already_in_the_cart_cannot_be_added_twice() -> None:
    entry = make_entry(windows=((0.0, 30.0),))
    pass_id = str(entry.passes[0].id)
    listing = make_list(entry, in_cart=lambda pid: pid == pass_id)

    menu = listing.sections()[pass_id].menu()

    assert not action(menu, MENU_ADD_TO_CART).isEnabled()
    assert action(menu, MENU_RETRIM).isEnabled()


def test_a_section_that_has_been_run_cannot_be_deleted_from_here() -> None:
    """Expected behaviour: the runs are what makes it undeletable, and the
    tooltip says where to go and delete those."""
    entry = make_entry(windows=((0.0, 30.0),), runs_per_pass=(("succeeded",),))

    listing = make_list(entry)
    delete = action(listing.sections()[str(entry.passes[0].id)].menu(), MENU_DELETE)

    assert not delete.isEnabled()
    assert delete.toolTip() == DELETE_BLOCKED_TOOLTIP


def test_a_section_with_no_runs_can_be_deleted() -> None:
    entry = make_entry(windows=((0.0, 30.0),))
    listing = make_list(entry)

    assert action(listing.sections()[str(entry.passes[0].id)].menu(), MENU_DELETE).isEnabled()


@pytest.mark.parametrize(
    ("label", "signal_name"),
    [
        (MENU_ADD_TO_CART, "section_add_to_cart"),
        (MENU_RETRIM, "section_retrim"),
        (MENU_REASSIGN, "section_reassign"),
        (MENU_DELETE, "section_delete"),
    ],
)
def test_a_section_action_names_the_section_it_was_chosen_on(label, signal_name) -> None:
    entry = make_entry(windows=((0.0, 30.0), (60.0, 90.0)))
    listing = make_list(entry)
    wanted = str(entry.passes[1].id)
    seen: list[str] = []
    getattr(listing, signal_name).connect(seen.append)

    action(listing.sections()[wanted].menu(), label).trigger()

    assert seen == [wanted]


def test_clicking_a_section_selects_it_and_lets_go_of_the_clip() -> None:
    entry = make_entry(windows=((0.0, 30.0),))
    listing = make_list(entry)
    pass_id = str(entry.passes[0].id)
    listing.set_selected(str(entry.video.id))
    seen: list[str] = []
    listing.section_activated.connect(seen.append)

    listing.sections()[pass_id].activated.emit(pass_id)

    assert seen == [pass_id]
    assert listing.selected_section == pass_id
    assert listing.selected is None


def test_selecting_a_clip_lets_go_of_the_section() -> None:
    entry = make_entry(windows=((0.0, 30.0),))
    listing = make_list(entry)
    listing.set_selected_section(str(entry.passes[0].id))

    listing.set_selected(str(entry.video.id))

    assert listing.selected_section is None


def test_a_clip_row_names_itself_in_the_buttons_beside_it() -> None:
    entry = make_entry()
    listing = make_list(entry)
    video_id = str(entry.video.id)
    row = listing.rows()[video_id]
    revealed: list[str] = []
    cut: list[str] = []
    listing.reveal_requested.connect(revealed.append)
    listing.new_section_requested.connect(cut.append)

    row.link_btn.click()
    row.new_section_btn.click()

    assert revealed == [video_id]
    assert cut == [video_id]


def test_the_clip_menu_offers_to_put_back_a_clip_that_is_already_hidden() -> None:
    row = VideoRow()
    entry = make_entry()

    row.set_entry(entry, no_name)
    assert [action.text() for action in row.menu().actions()][0] == MENU_HIDE

    row.set_entry(entry, no_name, hidden=True)
    assert [action.text() for action in row.menu().actions()][0] == MENU_UNHIDE


def test_only_the_sections_nothing_was_made_from_can_be_swept_up() -> None:
    """Expected behaviour: a section standing for a run that happened stays."""
    row = VideoRow()

    row.set_entry(make_entry(windows=((0.0, 30.0),), runs_per_pass=(("succeeded",),)), no_name)
    assert row.unused_sections() == 0
    swept = next(a for a in row.menu().actions() if a.text() == MENU_DELETE_UNUSED)
    assert not swept.isEnabled()

    row.set_entry(make_entry(windows=((0.0, 30.0), (40.0, 60.0))), no_name)
    assert row.unused_sections() == 2
    swept = next(a for a in row.menu().actions() if a.text() == MENU_DELETE_UNUSED)
    assert swept.isEnabled()


def test_the_list_says_it_takes_a_drop() -> None:
    listing = make_list(make_entry())

    assert listing.drop_hint.text() == DROP_HINT
    listing.grab()


def test_a_section_of_a_clip_of_unknown_length_still_gets_a_row() -> None:
    """Expected behaviour: the strip cannot place it, which is no reason to
    leave the section itself off the page."""
    entry = make_entry(windows=((0.0, 30.0),), duration_s=None)

    listing = make_list(entry)

    assert set(listing.sections()) == {str(entry.passes[0].id)}
    assert "Queued" in listing.sections()[str(entry.passes[0].id)].toolTip()


def test_a_bare_section_row_paints_before_it_has_been_told_anything() -> None:
    SectionRow().grab()


# --- the header row ----------------------------------------------------------


def click_cell(cell) -> None:
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    centre = QPointF(cell.width() / 2, cell.height() / 2)
    cell.mousePressEvent(
        QMouseEvent(
            QMouseEvent.Type.MouseButtonPress,
            centre,
            centre,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
    )


def test_the_clip_facts_sit_in_their_own_columns() -> None:
    """Expected behaviour: recorded, length and size are aligned columns under
    the header cells that name them, not one composite line."""
    entry = make_entry(
        duration_s=723.0,
        size_bytes=int(3.4 * 1024**3),
        captured_at="2026-07-01T14:32:00+00:00",
        captured_source=SOURCE_CONTAINER,
    )
    row = VideoRow()
    row.set_entry(entry, no_name)

    texts = label_texts(row)
    assert capture_label(entry.video) in texts
    assert "12m 03s" in texts
    assert "3.4 GB" in texts
    assert not any("·" in text for text in texts)
    row.grab()


def test_a_header_click_sorts_and_a_second_click_reverses() -> None:
    header = VideoListHeader()
    seen: list[tuple[str, bool]] = []
    header.sort_changed.connect(lambda column, descending: seen.append((column, descending)))

    click_cell(header.cell(SORT_NAME))
    click_cell(header.cell(SORT_NAME))
    click_cell(header.cell(SORT_SIZE))

    assert seen == [(SORT_NAME, False), (SORT_NAME, True), (SORT_SIZE, False)]


def test_the_header_marks_the_active_column_and_its_direction() -> None:
    header = VideoListHeader()
    header.set_sort(SORT_SIZE, True)

    assert header.cell(SORT_SIZE).text() == f"Size {SORT_DESC_GLYPH}"
    assert header.cell(SORT_NAME).text() == "Name"

    header.sort_by(SORT_SIZE)
    assert header.cell(SORT_SIZE).text() == f"Size {SORT_ASC_GLYPH}"
    header.grab()


def test_sorted_groups_rebuild_the_list_in_their_order() -> None:
    """Expected behaviour: the sort reorders clips within their date groups,
    and a group's clips never cross into another group."""
    big = make_entry(file_name="big.mp4", size_bytes=300)
    small = make_entry(file_name="small.mp4", size_bytes=100)
    late = make_entry(file_name="late.mp4", size_bytes=200)
    groups = [
        DateGroup(key="new", title="Today", entries=[big, small]),
        DateGroup(key="old", title="Yesterday", entries=[late]),
    ]
    listing = VideoLibraryList()

    listing.set_groups(sort_groups(groups, SORT_SIZE), no_name)
    assert [row.entry.video.file_name for row in listing.rows().values()] == [
        "small.mp4",
        "big.mp4",
        "late.mp4",
    ]

    listing.set_groups(sort_groups(groups, SORT_SIZE, descending=True), no_name)
    assert [row.entry.video.file_name for row in listing.rows().values()] == [
        "big.mp4",
        "small.mp4",
        "late.mp4",
    ]
