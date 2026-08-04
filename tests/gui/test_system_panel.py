"""The system panel: its gauges, its recorded history, and the memory grade.

Colour assertions go through core.theme rather than hex literals, so a palette
change moves the theme test and these together instead of failing here for a
reason that has nothing to do with the panel.

Where the panel is shown, and when its 1 Hz poll runs, belongs to This machine
and is covered in tests/gui/test_machine_page.py.
"""

from __future__ import annotations

from types import SimpleNamespace

from deepreefmap_gui.core.theme import BLOCK, UPDATE


def _recorded_runs_text(window) -> str:
    """Caption + every child label text + every meter bar stylesheet, concatenated."""
    from PySide6.QtWidgets import QLabel, QProgressBar

    combo = window._recorded_runs_filter_combo
    parts = [window._recorded_runs_caption.text()]
    parts += [combo.itemText(i) for i in range(combo.count())]
    parts += [w.text() for w in window._recorded_runs_container.findChildren(QLabel)]
    parts += [w.styleSheet() for w in window._recorded_runs_container.findChildren(QProgressBar)]
    return " ".join(parts)


def test_gauges_reflect_a_sampled_utilisation(window, monkeypatch) -> None:
    import deepreefmap_gui.profiling.system_probe as probe

    monkeypatch.setattr(
        probe, "sample_utilisation",
        lambda: probe.Utilisation(
            ram_used_bytes=8 * 1024**3, ram_total_bytes=32 * 1024**3, ram_percent=25.0,
            cpu_percent=40.0, vram_used_bytes=None, vram_total_bytes=None,
            swap_used_bytes=2 * 1024**3, swap_total_bytes=8 * 1024**3,
        ),
    )
    window._refresh_system_gauges()
    ram_bar, ram_label = window._sys_gauges["ram"]
    assert ram_bar.value() == 25
    assert "8.0 GB" in ram_label.text()
    # No distinct VRAM -> the gauge reads as shared, not a fake percentage.
    assert "shared" in window._sys_gauges["vram"][1].text()
    # Swap gauge reflects the sample (2/8 GB = 25%).
    swap_bar, swap_label = window._sys_gauges["swap"]
    assert swap_bar.value() == 25
    assert "2.0 GB" in swap_label.text()


def test_machine_specs_line_reports_gpu_and_cores(window, monkeypatch) -> None:
    import deepreefmap_gui.profiling.system_probe as probe

    monkeypatch.setattr(
        probe, "probe_system",
        lambda *a, **k: probe.SystemProfile(
            os_name="Linux", os_release="x", cpu_logical=16, cpu_physical=8,
            total_ram_bytes=64 * 1024**3, available_ram_bytes=48 * 1024**3,
            total_swap_bytes=8 * 1024**3, free_swap_bytes=8 * 1024**3,
            gpu=probe.GpuInfo(probe.GPU_CUDA, "RTX 4090", 24 * 1024**3, 20 * 1024**3),
            disk_total_bytes=1000 * 1024**3, disk_free_bytes=400 * 1024**3, disk_path="/",
        ),
    )
    window._refresh_disk_gauge()  # also populates the static specs line
    text = window._machine_specs_label.text()
    assert "RTX 4090" in text
    assert "16 logical / 8 physical" in text
    # No inferred capacity claim: we report hardware, we do not benchmark.
    assert "should handle" not in text


def _low_ram_profile(probe):
    return probe.SystemProfile(
        os_name="Linux", os_release="x", cpu_logical=8, cpu_physical=4,
        total_ram_bytes=32 * 1024**3, available_ram_bytes=6 * 1024**3,
        total_swap_bytes=0, free_swap_bytes=0,
        gpu=probe.GpuInfo(probe.GPU_NONE, "CPU only", None, None),
        disk_total_bytes=0, disk_free_bytes=0, disk_path="/",
    )


def _queue_pass(window, *, seconds: float, fps: int) -> None:
    """Give the grade a pass to read: it sizes the longest one queued."""
    window._survey_rows = [SimpleNamespace(begin_s=0.0, end_s=seconds)]
    window._fps_spin.setValue(fps)


def test_a_memory_risk_shows_the_inline_notice(window, monkeypatch) -> None:
    import deepreefmap_gui.profiling.system_probe as probe

    monkeypatch.setattr(probe, "probe_system", lambda *a, **k: _low_ram_profile(probe))
    _queue_pass(window, seconds=378.0, fps=5)
    window._update_memory_profile_warning()

    assert not window._memory_notice.isHidden()
    # A whole sentence in a narrow column, so it wraps rather than clipping.
    assert window._memory_notice.wordWrap()
    # The same grade in plain words, for the readiness view and the header button.
    assert "exhaust memory" in window._memory_advisory


def test_no_memory_notice_until_a_pass_is_queued(window) -> None:
    window._survey_rows = []  # no frame count is knowable yet
    window._update_memory_profile_warning()

    assert window._memory_notice.isHidden()
    assert window._memory_advisory == ""


def test_the_memory_notice_colour_tracks_warn_against_block(window, monkeypatch) -> None:
    """Amber and red have to stay distinguishable: one says the pass may spill
    into swap, the other says it is expected to run out and stop."""
    import deepreefmap_gui.profiling.system_probe as probe

    _queue_pass(window, seconds=378.0, fps=5)

    def profile(avail_gb, swap_gb):
        return probe.SystemProfile(
            os_name="Linux", os_release="x", cpu_logical=8, cpu_physical=4,
            total_ram_bytes=32 * 1024**3, available_ram_bytes=avail_gb * 1024**3,
            total_swap_bytes=swap_gb * 1024**3, free_swap_bytes=swap_gb * 1024**3,
            gpu=probe.GpuInfo(probe.GPU_NONE, "CPU only", None, None),
            disk_total_bytes=0, disk_free_bytes=0, disk_path="/",
        )

    # Fits only with swap -> amber warn.
    monkeypatch.setattr(probe, "probe_system", lambda *a, **k: profile(20, 30))
    window._update_memory_profile_warning()
    assert not window._memory_notice.isHidden()
    assert UPDATE in window._memory_notice.styleSheet()
    assert BLOCK not in window._memory_notice.styleSheet()

    # Exceeds RAM and swap -> red block.
    monkeypatch.setattr(probe, "probe_system", lambda *a, **k: profile(6, 0))
    window._update_memory_profile_warning()
    assert BLOCK in window._memory_notice.styleSheet()


def test_recorded_runs_summary_shows_peak_and_risk(window, monkeypatch) -> None:
    import deepreefmap_gui.profiling.run_history as history

    monkeypatch.setattr(
        history, "summarise_recorded_runs",
        lambda *a, **k: [{
            "key": "loger_star|seg|1376x768|3fps",
            "params": {"fps": 3, "processing_width": 1376, "processing_height": 768,
                       "mapping_backend": "loger_star", "segmentation_model": "coralscapes-vit-b-dpt"},
            "frames": 1134, "points": 14_000_000, "run_seconds": 430.0,
            "peak_ram_bytes": 30 * 1024**3, "peak_swap_bytes": 0, "swap_recorded": False,
            "peak_vram_bytes": 17 * 1024**3,
            "total_ram_bytes": 32 * 1024**3, "total_swap_bytes": 32 * 1024**3,
            "gpu_name": "RTX 4090", "gpu_total_vram_bytes": 24 * 1024**3,
        }],
    )
    window._refresh_recorded_runs()
    text = _recorded_runs_text(window)
    assert "1134 frames" in text
    assert "loger_star" in text
    # The segmentation model is now shown alongside the mapping backend.
    assert "coralscapes-vit-b-dpt" in text
    # Separate meters for RAM, swap and VRAM are rendered.
    assert "RAM" in text and "Swap" in text and "VRAM" in text
    # Swap predates capture on this run -> shown as "not recorded", not a fake 0%.
    assert "not recorded" in text
    # 30/32 GB = ~94% -> the RAM meter is coloured red, no separate text label.
    assert BLOCK in text


def test_recorded_runs_summary_shows_swap_spill(window, monkeypatch) -> None:
    import deepreefmap_gui.profiling.run_history as history

    monkeypatch.setattr(
        history, "summarise_recorded_runs",
        lambda *a, **k: [{
            "key": "loger_star|seg|1376x768|5fps",
            "params": {"fps": 5, "processing_width": 1376, "processing_height": 768,
                       "mapping_backend": "loger_star"},
            "frames": 1890, "points": 30_000_000, "run_seconds": 905.0,
            "peak_ram_bytes": 31 * 1024**3, "peak_swap_bytes": 8 * 1024**3, "swap_recorded": True,
            "peak_vram_bytes": 17 * 1024**3,
            "total_ram_bytes": 32 * 1024**3, "total_swap_bytes": 32 * 1024**3,
            "gpu_name": "RTX 4090", "gpu_total_vram_bytes": 24 * 1024**3,
        }],
    )
    window._refresh_recorded_runs()
    text = _recorded_runs_text(window)
    # Committed 39 GB > 32 GB RAM: the swap meter is populated and the tag is red.
    assert "swap" in text.lower()
    assert "not recorded" not in text
    assert BLOCK in text
    # The median wall-clock and its per-frame throughput are shown.
    assert "Time" in text
    assert "s/frame" in text


def test_recorded_runs_group_shows_run_count(window, monkeypatch) -> None:
    import deepreefmap_gui.profiling.run_history as history

    monkeypatch.setattr(
        history, "group_recorded_runs",
        lambda *a, **k: [{
            "params": {"fps": 5, "processing_width": 1376, "processing_height": 768,
                       "mapping_backend": "loger_star", "segmentation_model": "seg"},
            "frames": 1890, "count": 3, "run_seconds": 905, "seconds_per_frame": 905 / 1890,
            "peak_ram_bytes": 30 * 1024**3, "peak_swap_bytes": 0, "swap_recorded": True,
            "peak_vram_bytes": 17 * 1024**3,
            "total_ram_bytes": 32 * 1024**3, "total_swap_bytes": 32 * 1024**3,
            "gpu_name": "RTX 4090", "gpu_total_vram_bytes": 24 * 1024**3,
        }],
    )
    window._refresh_recorded_runs()
    assert "3 runs" in _recorded_runs_text(window)


def test_recorded_runs_summary_empty_state(window, monkeypatch) -> None:
    import deepreefmap_gui.profiling.run_history as history

    monkeypatch.setattr(history, "group_recorded_runs", lambda *a, **k: [])
    window._refresh_recorded_runs()
    assert "None yet" in window._recorded_runs_caption.text()
    assert window._recorded_runs_filter_row.isHidden()


def _group(mapping, seg, fps, frames):
    return {
        "params": {"fps": fps, "processing_width": 1376, "processing_height": 768,
                   "mapping_backend": mapping, "segmentation_model": seg},
        "frames": frames, "count": 1, "run_seconds": 100, "seconds_per_frame": 0.1,
        "peak_ram_bytes": 20 * 1024**3, "peak_swap_bytes": 0, "swap_recorded": True,
        "peak_vram_bytes": 10 * 1024**3,
        "total_ram_bytes": 32 * 1024**3, "total_swap_bytes": 32 * 1024**3,
        "gpu_name": "RTX 4090", "gpu_total_vram_bytes": 24 * 1024**3,
    }


def _group_titles(window):
    from PySide6.QtWidgets import QLabel

    return [w.text() for w in window._recorded_runs_container.findChildren(QLabel)]


def test_recorded_runs_filter_defaults_to_most_recent_combination(make_window, monkeypatch) -> None:
    import deepreefmap_gui.profiling.run_history as history

    # Patch before building: the window populates the filter from real machine
    # history during construction, and a later reload preserves that selection.
    monkeypatch.setattr(
        history, "group_recorded_runs",
        lambda *a, **k: [
            _group("loger_star", "coralscapes-vit-b-dpt", 1, 378),
            _group("scsfmlearner", "coralscapes-vit-b-dpt", 3, 785),
        ],
    )
    window = make_window()

    # Default selection is the newest combination; only its group renders and the
    # redundant per-group model subtitle is dropped.
    assert window._recorded_runs_filter_combo.currentData() == ("loger_star", "coralscapes-vit-b-dpt")
    titles = " ".join(_group_titles(window))
    assert "378 frames" in titles
    assert "785 frames" not in titles
    assert "scsfmlearner" not in titles


def test_recorded_runs_filter_all_shows_every_group_with_subtitle(window, monkeypatch) -> None:
    import deepreefmap_gui.profiling.run_history as history

    monkeypatch.setattr(
        history, "group_recorded_runs",
        lambda *a, **k: [
            _group("loger_star", "coralscapes-vit-b-dpt", 1, 378),
            _group("scsfmlearner", "coralscapes-vit-b-dpt", 3, 785),
        ],
    )
    window._refresh_recorded_runs()
    window._recorded_runs_filter_combo.setCurrentIndex(0)  # "All combinations"

    titles = " ".join(_group_titles(window))
    assert "378 frames" in titles and "785 frames" in titles
    # Under "All" the model subtitle returns so groups stay distinguishable.
    assert "scsfmlearner" in titles
