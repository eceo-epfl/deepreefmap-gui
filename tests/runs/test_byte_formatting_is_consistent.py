"""One byte formatter, so figures compared on screen are comparable.

Scenario: the Data panel shows what a run occupies; the System panel shows the
free space it has to fit into. Both were rendered from raw byte counts by
different functions in different units, both labelled "GB".

Expected behaviour: the same byte count renders identically wherever it appears.
"""

from __future__ import annotations

import pytest

from deepreefmap_gui.profiling.system_probe import format_bytes

FOUR_GIB = 4 * 1024**3


def test_the_data_panel_and_the_system_panel_agree():
    """These two sit on screen together while the user decides whether a run
    fits. Rendered by different functions they differed by 7%."""
    from deepreefmap_gui.runs import browse, run_cards

    assert run_cards.format_bytes is format_bytes
    assert browse.format_bytes is format_bytes


def test_run_cards_no_longer_defines_its_own():
    """The SI copy read 4 GiB as "4.29 GB" against the binary "4.0 GB"."""
    import inspect

    from deepreefmap_gui.runs import run_cards

    source = inspect.getsource(run_cards)
    assert "def format_bytes" not in source
    assert "1e9" not in source


def test_binary_units_throughout():
    assert format_bytes(FOUR_GIB) == "4.0 GB"
    assert format_bytes(2 * 1024**3) == "2.0 GB"
    assert format_bytes(512 * 1024**2) == "512 MB"


def test_an_unknown_size_is_a_dash():
    """The run-cards copy had no None handling; its callers pass Optional."""
    assert format_bytes(None) == "—"


def test_a_float_byte_count_is_accepted():
    """video_sizes comes out of the manifest as float."""
    assert format_bytes(float(FOUR_GIB)) == "4.0 GB"


@pytest.mark.parametrize("size", [0, 1, 1024, 1024**2 - 1])
def test_small_sizes_do_not_crash(size):
    assert format_bytes(size).endswith("MB")
