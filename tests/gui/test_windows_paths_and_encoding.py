"""Paths and encodings that only misbehave off a plain ASCII Linux box.

Scenario: the app runs on Windows under a profile name with a space or an accent
in it, or exports a class name outside the local code page.

Expected behaviour: the failures are either prevented or reported. None of these
need Windows to test -- a path with a space, a non-ASCII class name and a stubbed
imwrite reproduce all of them.
"""

from __future__ import annotations

import csv

import pytest


class _RefusingCv2:
    """Stands in for OpenCV refusing a path it cannot encode.

    imwrite signals that by returning False, not by raising, which is what made
    the failure silent.
    """

    IMREAD_COLOR = 1
    COLOR_RGB2BGR = 4

    @staticmethod
    def imwrite(_path, _image):
        return False

    @staticmethod
    def cvtColor(image, _code):
        return image


def test_a_refused_image_write_is_an_error(monkeypatch):
    """It used to report "Saved to ..." for a file that was never written."""
    import sys

    from deepreefmap_gui.runs.results import _write_image

    monkeypatch.setitem(sys.modules, "cv2", _RefusingCv2)

    with pytest.raises(OSError, match="could not write"):
        _write_image("/tmp/reef.png", object())


def test_a_successful_image_write_is_quiet(monkeypatch):
    import sys

    class _Accepting(_RefusingCv2):
        @staticmethod
        def imwrite(_path, _image):
            return True

    from deepreefmap_gui.runs.results import _write_image

    monkeypatch.setitem(sys.modules, "cv2", _Accepting)

    _write_image("/tmp/reef.png", object())


def test_the_arrow_stylesheet_quotes_its_paths(qapp, monkeypatch, tmp_path):
    """An unquoted url() stops parsing at the space, dropping every rule after
    it -- so a profile like "C:/Users/Jane Smith" loses all combo arrows."""
    from deepreefmap_gui.core import theme

    spaced = tmp_path / "Jane Smith" / "cache"
    spaced.mkdir(parents=True)
    monkeypatch.setattr(theme, "_chevron_file", lambda d, c: (spaced / f"{d}.png").as_posix())

    qss = theme._arrow_qss()

    assert "Jane Smith" in qss
    for fragment in qss.split("url(")[1:]:
        assert fragment.startswith('"'), f"unquoted url( in: {fragment[:60]}"


def test_cover_csvs_are_written_as_utf8(tmp_path):
    """Class names are not guaranteed ASCII, and the platform default on
    Windows is a code page that cannot represent most of them."""
    from deepreefmap_gui.cover import save_cover_csv

    path = tmp_path / "cover.csv"
    save_cover_csv(
        path,
        {"classes": {"1": {"name": "Acropora spp. \u2014 branching", "fraction": 0.5, "count": 2.0}}},
    )

    text = path.read_text(encoding="utf-8")
    assert "\u2014" in text


def _write_csv(path, encoding, name="reef"):
    rows = [
        ["videos", "timestamps", "transect_length", "crop_width"],
        [f"/tmp/{name}.mp4", "", "10", "2"],
    ]
    with path.open("w", newline="", encoding=encoding) as fh:
        csv.writer(fh).writerows(rows)


def test_a_csv_with_an_excel_bom_is_read(tmp_path):
    """Excel writes UTF-8 with a BOM; without utf-8-sig the BOM lands in the
    first column name and the required-columns check fails."""
    from deepreefmap_gui.form.batch import _load_batch_csv

    path = tmp_path / "jobs.csv"
    _write_csv(path, "utf-8-sig")

    jobs = _load_batch_csv(path)

    assert [j.name for j in jobs] == ["reef"]


def test_a_csv_in_another_encoding_is_not_refused(tmp_path):
    """One unrepresentable character should not cost the user the whole batch."""
    from deepreefmap_gui.form.batch import _load_batch_csv

    path = tmp_path / "jobs.csv"
    _write_csv(path, "cp1252", name="r\u00e9cif")

    jobs = _load_batch_csv(path)

    assert len(jobs) == 1


def test_no_text_file_is_read_or_written_without_an_encoding():
    """Scenario: ruff's PLW1514 covers open() and Path.open(), but not
    Path.read_text()/write_text() -- which is how most of this app touches JSON
    manifests and YAML presets.

    Expected behaviour: none of them fall back to the locale encoding, which on
    Windows is a code page that mangles any non-ASCII run name or video path.
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "deepreefmap_gui"
    offenders = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in ("read_text", "write_text"):
                continue
            if any(kw.arg == "encoding" for kw in node.keywords):
                continue
            offenders.append(f"{path.relative_to(root)}:{node.lineno} {node.func.attr}")

    assert offenders == [], "reads/writes using the platform encoding: " + ", ".join(offenders)
