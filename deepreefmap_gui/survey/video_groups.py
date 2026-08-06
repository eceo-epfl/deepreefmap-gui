"""Grouping clips by the date they were shot, and placing their sections on a strip.

The Videos page reads a card as a day's diving, so the grouping date is the
capture date rather than the import date: footage copied off a card weeks later
still belongs to the day it was filmed. Qt-free on purpose, both of these are
list-building rules and the widgets only paint what comes back.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from deepreefmap_gui.survey.catalogue import VideoLibraryEntry
from deepreefmap_gui.survey.models.run_record import RunRecord
from deepreefmap_gui.survey.models.transect_pass import TransectPass
from deepreefmap_gui.survey.models.video_asset import VideoAsset

# The periods the page can group by, key first then what the control shows.
PERIODS: tuple[tuple[str, str], ...] = (
    ("day", "Day"),
    ("week", "Week"),
    ("month", "Month"),
    ("year", "Year"),
    ("all", "All"),
)

DEFAULT_PERIOD = "day"

PERIOD_KEYS = tuple(key for key, _ in PERIODS)

ALL_KEY, ALL_TITLE = "all", "All videos"

# Clips whose capture date cannot be worked out. Kept as their own trailing
# group: filing them under an invented date would put footage in a day it was
# not shot, which is the one thing this grouping exists to get right.
UNDATED_KEY, UNDATED_TITLE = "undated", "Date unknown"

_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


@dataclass(frozen=True, slots=True)
class DateGroup:
    """One period's clips, newest first within the group."""

    key: str
    title: str
    entries: list[VideoLibraryEntry] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Span:
    """Where one section sits along its clip, as fractions of the clip's length."""

    pass_id: str
    begin: float
    end: float
    status: str
    run_count: int


def pass_status(runs: Sequence[RunRecord], *, held: bool = False) -> str:
    """The status shown for a pass: its latest run's, or ``queued`` with none.

    The single rule for "what became of this section". ``runs/video_detail.py``
    derives the same thing for its pass list and must call this rather than
    repeat it, or a section reads one way in the detail pane and another on the
    timeline beside it. Latest is by ``created_at``, ties keeping the given
    order, which is what a plain ``runs[-1]`` gives on rows read back in
    insertion order.
    """
    if not runs:
        return "held" if held else "queued"
    return sorted(runs, key=lambda run: run.created_at or "")[-1].status


def capture_moment(video: VideoAsset) -> datetime | None:
    """When the clip was shot, falling back to the file's mtime.

    A clip imported before the container probe existed carries no
    ``captured_at``, and mtime is the closest thing left: a card copied off the
    camera keeps the recording's own timestamps.

    Named apart from ``video_probe.capture_datetime``, which answers the same
    question off a file rather than off a row and returns the source with it.
    """
    for value in (video.captured_at, video.mtime):
        parsed = _parse_stamp(value)
        if parsed is not None:
            return parsed
    return None


def capture_date(entry: VideoLibraryEntry) -> date | None:
    """The calendar date a clip is filed under, or None when nothing says."""
    stamp = capture_moment(entry.video)
    return None if stamp is None else stamp.date()


def group_by_period(
    entries: Iterable[VideoLibraryEntry],
    period: str = DEFAULT_PERIOD,
    *,
    today: date | datetime | None = None,
) -> list[DateGroup]:
    """Clips bucketed by capture date, newest group first, undated ones last.

    Titles read relatively for the recent past ("Today", "Last week") and
    absolutely beyond it, so a scan down the page needs no date arithmetic.
    ``today`` is injectable to keep the wording testable off the clock.
    """
    if period not in PERIOD_KEYS:
        raise ValueError(f"period must be one of {PERIOD_KEYS}, got {period!r}")
    now = _as_date(today) if today is not None else _local_today()

    dated: list[tuple[date, VideoLibraryEntry]] = []
    undated: list[VideoLibraryEntry] = []
    for entry in entries:
        shot = capture_date(entry)
        if shot is None:
            undated.append(entry)
        else:
            dated.append((shot, entry))

    buckets: dict[date, list[VideoLibraryEntry]] = {}
    for shot, entry in sorted(dated, key=_clip_order):
        buckets.setdefault(_period_start(shot, period), []).append(entry)

    groups = [
        DateGroup(
            key=_period_key(start, period),
            title=_period_title(start, period, now),
            entries=clips,
        )
        for start, clips in sorted(buckets.items(), reverse=True)
    ]
    if undated:
        undated.sort(key=lambda e: e.video.file_name)
        groups.append(DateGroup(key=UNDATED_KEY, title=UNDATED_TITLE, entries=undated))
    return groups


def timeline_spans(entry: VideoLibraryEntry) -> list[Span]:
    """Each section of a clip as a 0..1 range along it, in time order.

    Empty when the clip's length is unknown: the widget then paints a bare
    groove. Normalising against a guessed length would draw sections in places
    the footage never had them, which is worse than drawing none.
    """
    duration = entry.video.duration_s
    if not duration or duration <= 0:
        return []
    runs_by_pass: dict[object, list[RunRecord]] = {}
    for run in entry.runs:
        runs_by_pass.setdefault(run.pass_id, []).append(run)
    spans = []
    for pass_ in sorted(entry.passes, key=_pass_order):
        runs = runs_by_pass.get(pass_.id, [])
        begin = _fraction(pass_.begin_s, duration, 0.0)
        # A section with no end runs to the end of the clip, which is also what
        # the pipeline does with an unset end_s.
        end = max(begin, _fraction(pass_.end_s, duration, 1.0))
        spans.append(
            Span(
                pass_id=str(pass_.id),
                begin=begin,
                end=end,
                status=pass_status(runs, held=getattr(pass_, "held", False)),
                run_count=len(runs),
            )
        )
    return spans


def _pass_order(pass_: TransectPass) -> tuple[float, float]:
    return (pass_.begin_s, pass_.end_s if pass_.end_s is not None else float("inf"))


def _clip_order(pair: tuple[date, VideoLibraryEntry]) -> tuple[int, str]:
    shot, entry = pair
    return (-shot.toordinal(), entry.video.file_name)


def _fraction(seconds: float | None, duration: float, default: float) -> float:
    if seconds is None:
        return default
    return min(1.0, max(0.0, seconds / duration))


def _parse_stamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    try:
        # Python 3.10's fromisoformat does not take a trailing Z.
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _local_today() -> date:
    # "Today" is the diver's today, so the machine's own zone, not UTC.
    return datetime.now(timezone.utc).astimezone().date()


def _as_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


def _monday(value: date) -> date:
    return value - timedelta(days=value.weekday())


def _period_start(shot: date, period: str) -> date:
    if period == "day":
        return shot
    if period == "week":
        return _monday(shot)
    if period == "month":
        return shot.replace(day=1)
    if period == "year":
        return shot.replace(month=1, day=1)
    # Every clip in one bucket, so the start is only there to sort against.
    return date.min


def _period_key(start: date, period: str) -> str:
    if period == "day":
        return start.isoformat()
    if period == "week":
        iso = start.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    if period == "month":
        return f"{start.year}-{start.month:02d}"
    if period == "year":
        return str(start.year)
    return ALL_KEY


def _period_title(start: date, period: str, today: date) -> str:
    if period == "day":
        return _day_title(start, today)
    if period == "week":
        return _week_title(start, today)
    if period == "month":
        return _month_title(start, today)
    if period == "year":
        return _year_title(start, today)
    return ALL_TITLE


def _day_title(start: date, today: date) -> str:
    days = (today - start).days
    if days == 0:
        return "Today"
    if days == 1:
        return "Yesterday"
    # Within the week just gone a weekday name is enough to place it, and it is
    # how a diver talks about the dive anyway.
    if 2 <= days <= 6:
        return start.strftime("%A")
    return _long_date(start)


def _week_title(start: date, today: date) -> str:
    weeks = (_monday(today) - start).days // 7
    if weeks == 0:
        return "This week"
    if weeks == 1:
        return "Last week"
    return f"Week of {_long_date(start)}"


def _month_title(start: date, today: date) -> str:
    months = (today.year - start.year) * 12 + (today.month - start.month)
    if months == 0:
        return "This month"
    if months == 1:
        return "Last month"
    return f"{_MONTHS[start.month - 1]} {start.year}"


def _year_title(start: date, today: date) -> str:
    years = today.year - start.year
    if years == 0:
        return "This year"
    if years == 1:
        return "Last year"
    return str(start.year)


def _long_date(value: date) -> str:
    # Spelled out rather than strftime: %-d is glibc-only and the app ships on
    # Windows too.
    return f"{value.day} {_MONTHS[value.month - 1]} {value.year}"
