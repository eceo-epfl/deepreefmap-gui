"""Readiness view of Setup: pass/fail logic, gating, and provisioning wiring."""

from __future__ import annotations

import json
from types import SimpleNamespace

from _factories import make_profile

from deepreefmap_gui.simple import setup as setup_mod

_GB = 1024**3


# --- pure verdict logic (no window) ------------------------------------------


def test_graphics_passes_with_a_card():
    check = setup_mod.graphics_check(gpu_name="NVIDIA RTX", requires_gpu=True)
    assert check.ok
    assert "NVIDIA RTX" in check.detail


def test_graphics_passes_on_cpu_when_the_method_allows_it():
    check = setup_mod.graphics_check(gpu_name=None, requires_gpu=False)
    assert check.ok
    assert "CPU" in check.detail


def test_graphics_fails_when_the_method_needs_a_card():
    check = setup_mod.graphics_check(gpu_name=None, requires_gpu=True)
    assert not check.ok
    assert "requires one" in check.detail


def test_models_row_names_what_is_missing():
    assert setup_mod.models_check([]).ok
    check = setup_mod.models_check(["scsfmlearner", "segformer-b2"])
    assert not check.ok
    assert "scsfmlearner" in check.detail and "segformer-b2" in check.detail


def test_space_row_compares_against_the_threshold():
    assert setup_mod.space_check(20 * _GB, 10 * _GB).ok
    assert not setup_mod.space_check(1 * _GB, 10 * _GB).ok


def test_space_row_quotes_capacity_only_once_runs_can_size_it():
    """Scenario: nothing has been processed on this machine yet.

    Expected behaviour: the row reports free space and says capacity is not yet
    known, rather than converting the unmeasured per-pass fallback into a
    footage figure the user would read as measured.
    """
    unmeasured = setup_mod.space_check(20 * _GB, 10 * _GB)
    assert "estimated once a run is recorded" in unmeasured.detail

    measured = setup_mod.space_check(20 * _GB, 10 * _GB, bytes_per_footage_minute=_GB / 6)
    assert "2 hours of footage" in measured.detail


def _write_run(root, name, *, size_bytes, frames, fps):
    run_dir = root / name
    run_dir.mkdir(parents=True)
    (run_dir / "run_manifest.json").write_text(
        json.dumps({"frames_processed": frames, "fps": fps}), encoding="utf-8"
    )
    (run_dir / "cloud.ply").write_bytes(b"x" * size_bytes)


def test_footage_rate_is_measured_from_run_output(tmp_path):
    # 600 frames at 5 fps is 2 minutes of footage; 4 MB of output is 2 MB/minute.
    _write_run(tmp_path, "run_a", size_bytes=4 * 1024**2, frames=600, fps=5)

    rate = setup_mod.measure_bytes_per_footage_minute(tmp_path)

    assert rate is not None
    # The manifest itself lands in the directory too, so allow for its bytes.
    assert 2 * 1024**2 <= rate < 2.1 * 1024**2


def test_footage_rate_is_unknown_without_runs(tmp_path):
    assert setup_mod.measure_bytes_per_footage_minute(tmp_path) is None


def test_a_run_with_no_usable_footage_length_is_skipped(tmp_path):
    """A zero frame count or fps would divide by zero, and says nothing anyway."""
    _write_run(tmp_path, "empty", size_bytes=1024, frames=0, fps=5)

    assert setup_mod.measure_bytes_per_footage_minute(tmp_path) is None


def test_setup_ready_needs_all_three():
    passing = setup_mod.evaluate_setup(
        gpu_name="G", requires_gpu=False, missing_models=[], free_bytes=20 * _GB, min_free_bytes=10 * _GB
    )
    assert setup_mod.setup_ready(passing)
    one_missing = setup_mod.evaluate_setup(
        gpu_name="G", requires_gpu=False, missing_models=["m"], free_bytes=20 * _GB, min_free_bytes=10 * _GB
    )
    assert not setup_mod.setup_ready(one_missing)


def test_batch_disk_estimate_fits_or_not():
    fits = setup_mod.estimate_batch_disk(3, free_bytes=100, per_pass_bytes=10)
    assert fits.need_bytes == 30 and fits.fits
    tight = setup_mod.estimate_batch_disk(20, free_bytes=100, per_pass_bytes=10)
    assert not tight.fits


# --- window-level rows and gating --------------------------------------------


_prof = make_profile


def _force(window, monkeypatch, *, gpu_name="GPU", free=50 * _GB, missing=(), mapping="scsfmlearner"):
    monkeypatch.setattr(setup_mod, "probe_system", lambda *_a, **_k: _prof(gpu_name=gpu_name, free=free))
    monkeypatch.setattr(window, "_survey_missing_models", lambda: list(missing))
    # The form, which is what the readiness rows and the Run gate both read.
    # Setting only the preset let this helper describe a configuration the gate
    # would never see.
    window._map_combo.setCurrentText(mapping)
    window._survey_preset = {"mapping_name": mapping}
    window._refresh_readiness_view()


def test_this_machine_is_a_reachable_section(window):
    from deepreefmap_gui.simple.mode import SIMPLE_SECTIONS

    assert "machine" in SIMPLE_SECTIONS
    window._set_simple_section("machine")
    assert window._current_section() == "machine"


def test_ready_when_all_three_pass(window, monkeypatch):
    _force(window, monkeypatch, gpu_name="GPU", free=50 * _GB, missing=())
    assert window._setup_summary.text() == "All requirements met."
    checks = {c.key: c for c in window._current_setup_checks()}
    for key, (_icon, _detail, actions) in window._setup_check_rows.items():
        # A row whose action toggles something keeps it once the row passes:
        # "Remove from the applications menu" is only reachable while it is
        # there. Rows whose action fixes what they report hide it.
        if checks[key].action_label:
            continue
        assert all(a.isHidden() for a in actions)


def test_missing_models_crosses_the_row_and_offers_provisioning(window, monkeypatch):
    _force(window, monkeypatch, missing=["scsfmlearner"])
    _icon, detail, actions = window._setup_check_rows["models"]
    assert "scsfmlearner" in detail.text()
    assert all(not a.isHidden() for a in actions)
    assert window._setup_summary.text().startswith("1 requirement not met.")


def test_cpu_only_machine_still_reaches_ready_with_the_standard_method(window, monkeypatch):
    _force(window, monkeypatch, gpu_name=None, mapping="scsfmlearner", missing=())
    _icon, detail, actions = window._setup_check_rows["graphics"]
    assert "CPU" in detail.text()
    assert all(a.isHidden() for a in actions)
    assert window._setup_summary.text() == "All requirements met."


def test_gpu_only_method_without_a_card_fails_the_graphics_row(window, monkeypatch):
    _force(window, monkeypatch, gpu_name=None, mapping="loger", missing=())
    _icon, detail, actions = window._setup_check_rows["graphics"]
    assert "requires one" in detail.text()
    assert not actions[0].isHidden()
    assert window._setup_summary.text().startswith("1 requirement not met.")


def test_low_space_crosses_the_row(window, monkeypatch):
    _force(window, monkeypatch, free=1 * _GB, missing=())
    _icon, detail, actions = window._setup_check_rows["space"]
    assert "Delete old surveys" in detail.text()
    assert not actions[0].isHidden()


def test_no_jargon_on_the_readiness_view(window, monkeypatch):
    _force(window, monkeypatch, gpu_name=None, missing=["coralscapes-vit-s-dpt"], free=1 * _GB)
    texts = [window._setup_summary.text()]
    for _icon, detail, _actions in window._setup_check_rows.values():
        texts.append(detail.text())
    blob = " ".join(texts).lower()
    for banned in ("hugging face", "gated", "token"):
        assert banned not in blob


# --- launch gating -----------------------------------------------------------


def test_first_launch_leads_to_this_machine_when_not_ready(make_window, monkeypatch):
    # Too little space makes the machine not-ready regardless of models or card.
    monkeypatch.setattr(setup_mod, "probe_system", lambda *_a, **_k: _prof(gpu_name="GPU", free=1 * _GB))
    window = make_window()
    assert window._current_section() == "machine"
    assert window._machine_view == "readiness"


def test_first_launch_skips_setup_when_ready(make_window, monkeypatch):
    from PySide6.QtCore import QSettings

    monkeypatch.setattr(setup_mod, "probe_system", lambda *_a, **_k: _prof(gpu_name="GPU", free=50 * _GB))
    monkeypatch.setattr("deepreefmap_gui.models.cache.is_model_cached", lambda info: True)
    window = make_window()
    assert window._current_section() == "transects"
    assert str(QSettings("ECEO", "deepreefmap").value("setup_complete")).lower() == "true"


def test_completed_flag_goes_straight_to_plan(make_window):
    from PySide6.QtCore import QSettings

    QSettings("ECEO", "deepreefmap").setValue("setup_complete", True)
    window = make_window()
    assert window._current_section() == "transects"


def test_ready_records_the_flag_so_it_stops_leading(window, monkeypatch):
    from PySide6.QtCore import QSettings

    QSettings("ECEO", "deepreefmap").remove("setup_complete")
    _force(window, monkeypatch, missing=())
    assert str(QSettings("ECEO", "deepreefmap").value("setup_complete")).lower() == "true"


def test_this_machine_reopenable_from_the_header(window):
    window._set_simple_section("transects")
    window._machine_nav_button.click()
    assert window._current_section() == "machine"


# --- provisioning actions ----------------------------------------------------


def test_usb_button_wraps_the_pack_import(window, monkeypatch):
    called = []
    monkeypatch.setattr(window, "_on_import_model_pack", lambda: called.append(True))
    window._survey_worker_running = False
    window._on_setup_import_pack()
    assert called == [True]


def test_usb_import_waits_for_a_running_batch(window, monkeypatch):
    called = []
    monkeypatch.setattr(window, "_on_import_model_pack", lambda: called.append(True))
    window._survey_worker_running = True
    window._on_setup_import_pack()
    assert called == []
    assert "Unavailable while processing" in window._status_label.text()


def test_model_delete_waits_for_a_running_batch(window, monkeypatch):
    """A later pass may need the model, so deletion follows the same rule as
    downloads and imports."""
    deleted = []
    monkeypatch.setattr(window, "_execute_delete", deleted.append)
    window._survey_worker_running = True
    window._on_delete_click("segformer-b2")
    assert deleted == []
    assert window._delete_armed == {}
    assert "Unavailable while processing" in window._status_label.text()
    window._survey_worker_running = False


def test_download_starts_for_ungated_models(window, monkeypatch):
    monkeypatch.setattr(window, "_survey_missing_models", lambda: ["scsfmlearner"])
    window._hf_auth_user = None
    window._survey_worker_running = False
    downloaded: list[str] = []
    monkeypatch.setattr(window, "_download_model", downloaded.append)
    window._on_setup_download_models()
    assert downloaded == ["scsfmlearner"]


def test_download_signs_in_first_for_a_model_that_needs_an_account(window, monkeypatch):
    monkeypatch.setattr(window, "_survey_missing_models", lambda: ["coralscapes-vit-s-dpt"])
    window._hf_auth_user = None
    window._survey_worker_running = False
    auth, downloaded = [], []
    monkeypatch.setattr(window, "_on_hf_auth_button", lambda: auth.append(True))
    monkeypatch.setattr(window, "_download_model", downloaded.append)
    window._on_setup_download_models()
    assert auth == [True]
    assert downloaded == []


# --- where the memory warning leads ------------------------------------------


def test_the_memory_warning_links_through_to_the_readiness_view(window):
    """The warning points at a sentence on the readiness view, so the destination
    alone is not enough: whichever view was last open gives way to readiness."""
    window._set_simple_section("machine")
    window._set_machine_view("performance")

    window._capacity_advice.linkActivated.emit("#system")

    assert window._current_section() == "machine"
    assert window._machine_view == "readiness"


def test_the_memory_grade_sizes_the_longest_queued_pass(window):
    """Passes run one at a time, so the longest is the one that peaks memory."""
    window._survey_rows = [
        SimpleNamespace(begin_s=0.0, end_s=10.0),
        SimpleNamespace(begin_s=0.0, end_s=40.0),
    ]
    assert window._simple_peak_frames(5) == 200


# --- batch pre-flight --------------------------------------------------------


def test_preflight_proceeds_silently_when_there_is_room(window, monkeypatch):
    import shutil

    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(
        shutil, "disk_usage", lambda _p: SimpleNamespace(free=200 * _GB, total=0, used=0)
    )

    def _no_prompt(*_a, **_k):
        raise AssertionError("prompted despite plenty of room")

    monkeypatch.setattr(QMessageBox, "question", staticmethod(_no_prompt))
    assert window._confirm_batch_space(3) is True


def test_preflight_asks_before_a_batch_that_may_not_fit(window, monkeypatch):
    import shutil

    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(shutil, "disk_usage", lambda _p: SimpleNamespace(free=1 * _GB, total=0, used=0))
    seen = {}

    def _decline(*args, **_k):
        seen["text"] = args[2]
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", staticmethod(_decline))
    assert window._confirm_batch_space(5) is False
    assert "5 passes" in seen["text"]


def test_the_readiness_row_and_the_run_gate_read_one_machine(window, monkeypatch):
    """Both ask system_probe, so a card that cannot report its VRAM (ROCm) cannot
    fail the graphics row while the gate lets the same run start."""
    import sys
    import types

    torch = types.SimpleNamespace(
        cuda=types.SimpleNamespace(
            is_available=lambda: True,
            mem_get_info=_unsupported,
            get_device_name=lambda dev=0: "Radeon RX 7900",
        ),
        backends=types.SimpleNamespace(mps=types.SimpleNamespace(is_available=lambda: False)),
    )
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setattr(window, "_survey_missing_models", list)
    window._gpu_available_cache = None
    window._map_combo.setCurrentText("loger")

    graphics = next(c for c in window._current_setup_checks() if c.key == "graphics")
    assert graphics.ok
    assert "Radeon RX 7900" in graphics.detail
    assert window._gpu_only_mapper() == ""


def _unsupported(dev=0):
    raise RuntimeError("mem_get_info is not supported on this device")


def test_memory_is_a_row_like_the_others():
    check = setup_mod.memory_check(total_ram_bytes=16 * _GB, vram_bytes=8 * _GB)
    assert check.ok
    assert "16.0 GB" in check.detail and "8.0 GB" in check.detail

    without_card = setup_mod.memory_check(total_ram_bytes=16 * _GB, vram_bytes=None)
    assert without_card.detail == "16.0 GB of memory."


def test_a_memory_advisory_marks_its_row_without_holding_the_machine_back():
    """The row fails against the session queued, not against the machine, so
    readiness stays met and processing still starts."""
    advisory = "This session may exhaust memory on this machine."
    checks = setup_mod.evaluate_setup(
        gpu_name="GPU",
        requires_gpu=True,
        missing_models=[],
        free_bytes=50 * _GB,
        min_free_bytes=10 * _GB,
        total_ram_bytes=8 * _GB,
        memory_advisory=advisory,
    )
    memory = next(c for c in checks if c.key == "memory")
    assert not memory.ok
    assert memory.advisory
    assert memory.detail == advisory
    assert setup_mod.setup_ready(checks)


def test_the_readiness_view_paints_the_memory_row(window, monkeypatch):
    _force(window, monkeypatch, missing=())
    window._memory_advisory = "This session may exhaust memory on this machine."
    window._refresh_readiness_view()

    icon, detail, actions = window._setup_check_rows["memory"]
    assert "!" in icon.text()
    assert detail.text() == window._memory_advisory
    # The row that fails carries the action that fixes it: settings is where a
    # smaller resolution or frame rate is chosen.
    assert not actions[0].isHidden()
    assert window._setup_summary.text() == "All requirements met."
