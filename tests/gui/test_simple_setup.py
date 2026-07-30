"""First-run laptop setup step: pass/fail logic, gating, and provisioning wiring."""

from __future__ import annotations

from types import SimpleNamespace

from deepreefmap_gui.profiling.system_probe import GPU_CUDA, GPU_NONE
from deepreefmap_gui.simple import setup as S

_GB = 1024**3


# --- pure verdict logic (no window) ------------------------------------------


def test_graphics_passes_with_a_card():
    check = S.graphics_check(gpu_name="NVIDIA RTX", requires_gpu=True)
    assert check.ok
    assert "NVIDIA RTX" in check.detail


def test_graphics_passes_on_cpu_when_the_method_allows_it():
    check = S.graphics_check(gpu_name=None, requires_gpu=False)
    assert check.ok
    assert "main processor" in check.detail


def test_graphics_fails_when_the_method_needs_a_card():
    check = S.graphics_check(gpu_name=None, requires_gpu=True)
    assert not check.ok
    assert "graphics card" in check.detail


def test_models_row_names_what_is_missing():
    assert S.models_check([]).ok
    check = S.models_check(["scsfmlearner", "segformer-b2"])
    assert not check.ok
    assert "scsfmlearner" in check.detail and "segformer-b2" in check.detail


def test_space_row_compares_against_the_threshold():
    assert S.space_check(20 * _GB, 10 * _GB).ok
    assert not S.space_check(1 * _GB, 10 * _GB).ok


def test_setup_ready_needs_all_three():
    passing = S.evaluate_setup(
        gpu_name="G", requires_gpu=False, missing_models=[], free_bytes=20 * _GB, min_free_bytes=10 * _GB
    )
    assert S.setup_ready(passing)
    one_missing = S.evaluate_setup(
        gpu_name="G", requires_gpu=False, missing_models=["m"], free_bytes=20 * _GB, min_free_bytes=10 * _GB
    )
    assert not S.setup_ready(one_missing)


def test_batch_disk_estimate_fits_or_not():
    fits = S.estimate_batch_disk(3, free_bytes=100, per_pass_bytes=10)
    assert fits.need_bytes == 30 and fits.fits
    tight = S.estimate_batch_disk(20, free_bytes=100, per_pass_bytes=10)
    assert not tight.fits


# --- window-level rows and gating --------------------------------------------


def _prof(*, gpu_name: str | None = "GPU", free: int = 50 * _GB):
    """A stand-in for probe_system's result, exposing only what setup reads."""
    if gpu_name:
        gpu = SimpleNamespace(kind=GPU_CUDA, name=gpu_name)
    else:
        gpu = SimpleNamespace(kind=GPU_NONE, name="CPU only")
    return SimpleNamespace(gpu=gpu, disk_free_bytes=free)


def _force(window, monkeypatch, *, gpu_name="GPU", free=50 * _GB, missing=(), mapping="scsfmlearner"):
    monkeypatch.setattr(S, "probe_system", lambda *_a, **_k: _prof(gpu_name=gpu_name, free=free))
    monkeypatch.setattr(window, "_survey_missing_models", lambda: list(missing))
    window._survey_preset = {"mapping_name": mapping}
    window._refresh_setup_page()


def test_setup_is_a_reachable_section(window):
    from deepreefmap_gui.simple.mode import SIMPLE_SECTIONS

    assert "setup" in SIMPLE_SECTIONS
    window._set_simple_section("setup")
    assert window._current_section() == "setup"


def test_ready_when_all_three_pass(window, monkeypatch):
    _force(window, monkeypatch, gpu_name="GPU", free=50 * _GB, missing=())
    assert window._setup_summary.text() == "Ready to survey."
    for _icon, _detail, actions in window._setup_check_rows.values():
        assert all(a.isHidden() for a in actions)


def test_missing_models_crosses_the_row_and_offers_provisioning(window, monkeypatch):
    _force(window, monkeypatch, missing=["scsfmlearner"])
    _icon, detail, actions = window._setup_check_rows["models"]
    assert "scsfmlearner" in detail.text()
    assert all(not a.isHidden() for a in actions)
    assert window._setup_summary.text() != "Ready to survey."


def test_cpu_only_laptop_still_reaches_ready_with_the_standard_method(window, monkeypatch):
    _force(window, monkeypatch, gpu_name=None, mapping="scsfmlearner", missing=())
    _icon, detail, actions = window._setup_check_rows["graphics"]
    assert "main processor" in detail.text()
    assert all(a.isHidden() for a in actions)
    assert window._setup_summary.text() == "Ready to survey."


def test_gpu_only_method_without_a_card_fails_the_graphics_row(window, monkeypatch):
    _force(window, monkeypatch, gpu_name=None, mapping="loger", missing=())
    _icon, detail, actions = window._setup_check_rows["graphics"]
    assert "graphics card" in detail.text()
    assert not actions[0].isHidden()
    assert window._setup_summary.text() != "Ready to survey."


def test_low_space_crosses_the_row(window, monkeypatch):
    _force(window, monkeypatch, free=1 * _GB, missing=())
    _icon, detail, actions = window._setup_check_rows["space"]
    assert "Delete old surveys" in detail.text()
    assert not actions[0].isHidden()


def test_no_jargon_on_the_setup_step(window, monkeypatch):
    _force(window, monkeypatch, gpu_name=None, missing=["coralscapes-vit-s-dpt"], free=1 * _GB)
    texts = [window._setup_summary.text()]
    for _icon, detail, _actions in window._setup_check_rows.values():
        texts.append(detail.text())
    blob = " ".join(texts).lower()
    for banned in ("hugging face", "gated", "token"):
        assert banned not in blob


# --- launch gating -----------------------------------------------------------


def test_first_launch_leads_to_setup_when_not_ready(make_window, monkeypatch):
    # Too little space makes the laptop not-ready regardless of models or card.
    monkeypatch.setattr(S, "probe_system", lambda *_a, **_k: _prof(gpu_name="GPU", free=1 * _GB))
    window = make_window()
    assert window._current_section() == "setup"


def test_first_launch_skips_setup_when_ready(make_window, monkeypatch):
    from PySide6.QtCore import QSettings

    monkeypatch.setattr(S, "probe_system", lambda *_a, **_k: _prof(gpu_name="GPU", free=50 * _GB))
    monkeypatch.setattr("deepreefmap_gui.models.manager.is_model_cached", lambda info: True)
    window = make_window()
    assert window._current_section() == "plan"
    assert str(QSettings("ECEO", "deepreefmap").value("setup_complete")).lower() == "true"


def test_completed_flag_goes_straight_to_plan(make_window):
    from PySide6.QtCore import QSettings

    QSettings("ECEO", "deepreefmap").setValue("setup_complete", True)
    window = make_window()
    assert window._current_section() == "plan"


def test_ready_records_the_flag_so_it_stops_leading(window, monkeypatch):
    from PySide6.QtCore import QSettings

    QSettings("ECEO", "deepreefmap").remove("setup_complete")
    _force(window, monkeypatch, missing=())
    assert str(QSettings("ECEO", "deepreefmap").value("setup_complete")).lower() == "true"


def test_setup_reopenable_from_the_header(window):
    window._set_simple_section("plan")
    window._setup_nav_button.click()
    assert window._current_section() == "setup"


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
    assert "Wait for processing" in window._status_label.text()


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


# --- memory warning surfaced in simple mode ----------------------------------


def test_memory_icon_routes_to_setup_in_simple_mode(window):
    window._set_ui_mode("simple")
    window._reveal_memory_detail()
    assert window._current_section() == "setup"


def test_memory_icon_routes_to_system_tab_in_advanced_mode(window):
    window._mode_buttons["advanced"].click()
    window._reveal_memory_detail()
    assert window._sidebar_tabs.currentIndex() == window._TAB_SYSTEM


def test_memory_grade_uses_the_longest_pass_in_simple_mode(window):
    window._set_ui_mode("simple")
    window._survey_rows = [
        SimpleNamespace(begin_s=0.0, end_s=10.0),
        SimpleNamespace(begin_s=0.0, end_s=40.0),
    ]
    assert window._simple_peak_frames(5) == 200


# --- batch pre-flight --------------------------------------------------------


def test_preflight_proceeds_silently_when_there_is_room(simple_window, monkeypatch):
    import shutil

    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(
        shutil, "disk_usage", lambda _p: SimpleNamespace(free=200 * _GB, total=0, used=0)
    )

    def _no_prompt(*_a, **_k):
        raise AssertionError("prompted despite plenty of room")

    monkeypatch.setattr(QMessageBox, "question", staticmethod(_no_prompt))
    assert simple_window._confirm_batch_space(3) is True


def test_preflight_asks_before_a_batch_that_may_not_fit(simple_window, monkeypatch):
    import shutil

    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(shutil, "disk_usage", lambda _p: SimpleNamespace(free=1 * _GB, total=0, used=0))
    seen = {}

    def _decline(*args, **_k):
        seen["text"] = args[2]
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", staticmethod(_decline))
    assert simple_window._confirm_batch_space(5) is False
    assert "5 passes" in seen["text"]
