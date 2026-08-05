"""The batch CSV a survey queue can be imported from.

Parsing only: the Run step reads a file through here and turns each row into a
pass, which is the one way a CSV reaches the pipeline.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class BatchJob:
    """One row of a batch reconstruction CSV; name defaults to the video filename stem."""

    __slots__ = (
        "video",
        "begin_s",
        "end_s",
        "transect_length",
        "crop_width",
        "name",
        "transect",
    )

    def __init__(
        self,
        video: str,
        begin_s: float | None,
        end_s: float | None,
        transect_length: float | None,
        crop_width: float | None,
        name: str,
        transect: str = "",
    ) -> None:
        self.video = video
        self.begin_s = begin_s
        self.end_s = end_s
        self.transect_length = transect_length
        self.crop_width = crop_width
        self.name = name
        self.transect = transect


def _parse_optional_float(raw: str) -> float | None:
    s = (raw or "").strip()
    if not s:
        return None
    return float(s)


def _parse_timestamp_range(raw: str) -> tuple[float | None, float | None]:
    """Parse "<begin>-<end>" in seconds, splitting at the first dash.

    Either side may be empty, and a bare "30" is a begin with no end.
    """
    s = (raw or "").strip()
    if not s:
        return None, None
    if "-" not in s:
        return _parse_optional_float(s), None
    head, _, tail = s.partition("-")
    return _parse_optional_float(head), _parse_optional_float(tail)


def _open_csv(path: Path):
    """Open a batch CSV, naming an encoding rather than taking the platform's.

    Without this the default is the locale encoding, which on Windows is a code
    page: a path with an accent in it comes back as mojibake or raises. utf-8-sig
    covers plain UTF-8 and the BOM Excel writes. Anything else is decoded
    permissively rather than refused: a mangled character in one row should not
    cost the user the whole batch, and a wrong video path fails per row with a
    message naming it.
    """
    handle = path.open(newline="", encoding="utf-8-sig")
    try:
        handle.read()
    except UnicodeDecodeError:
        handle.close()
        logger.warning("Batch CSV %s is not UTF-8; decoding leniently", path)
        return path.open(newline="", encoding="utf-8", errors="replace")
    handle.seek(0)
    return handle


def load_batch_csv(path: Path) -> list[BatchJob]:
    """Read a CSV with case-insensitive columns and return parsed rows.

    Only `transect` is optional. It names a planned transect to assign the row's
    pass to, and a row without one lands unassigned rather than being refused.
    """
    import csv

    suffix = path.suffix.lower()
    if suffix in (".xls", ".xlsx"):
        raise ValueError(
            "Excel files aren't supported (pandas isn't a dependency). "
            "Save the sheet as CSV and try again."
        )
    required = {"videos", "timestamps", "transect_length", "crop_width"}
    jobs: list[BatchJob] = []
    with _open_csv(path) as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError("CSV has no header row.")
        norm = {fn.strip().lower(): fn for fn in reader.fieldnames}
        missing = required - set(norm.keys())
        if missing:
            raise ValueError(
                f"CSV is missing required columns: {', '.join(sorted(missing))}"
            )
        transect_col = norm.get("transect")
        for n, row in enumerate(reader, start=2):
            video = (row.get(norm["videos"], "") or "").strip()
            if not video:
                continue  # skip blank rows
            try:
                begin_s, end_s = _parse_timestamp_range(row.get(norm["timestamps"], ""))
                transect_length = _parse_optional_float(row.get(norm["transect_length"], ""))
                crop_width = _parse_optional_float(row.get(norm["crop_width"], ""))
            except ValueError as exc:
                raise ValueError(f"Row {n}: {exc}") from exc
            jobs.append(
                BatchJob(
                    video=video,
                    begin_s=begin_s,
                    end_s=end_s,
                    transect_length=transect_length,
                    crop_width=crop_width,
                    name=Path(video).stem or f"job_{n - 1}",
                    transect=(row.get(transect_col, "") or "").strip() if transect_col else "",
                )
            )
    if not jobs:
        raise ValueError("No usable rows in CSV.")
    return jobs
