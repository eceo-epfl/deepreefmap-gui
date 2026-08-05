"""The system probe reads memory/CPU/GPU/disk on any OS, with torch mocked."""

from __future__ import annotations

import json
import sys
import types

from deepreefmap_gui.profiling import system_probe as sp


def _fake_torch(*, cuda=False, mps=False, name="GPU", free=1, total=2):
    return types.SimpleNamespace(
        cuda=types.SimpleNamespace(
            is_available=lambda: cuda,
            mem_get_info=lambda dev=0: (free, total),
            get_device_name=lambda dev=0: name,
        ),
        backends=types.SimpleNamespace(mps=types.SimpleNamespace(is_available=lambda: mps)),
    )


def test_probe_gpu_reports_cuda_vram(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(cuda=True, name="RTX 4090", free=100, total=1000))
    gpu = sp._probe_gpu()
    assert gpu.kind == sp.GPU_CUDA
    assert gpu.name == "RTX 4090"
    assert (gpu.total_vram_bytes, gpu.free_vram_bytes) == (1000, 100)
    assert gpu.has_distinct_vram


def test_a_card_that_cannot_report_its_vram_is_still_a_card(monkeypatch) -> None:
    """ROCm builds do not all implement mem_get_info. Readiness asks this probe
    whether a card exists, so a missing byte count must not read as no GPU."""
    torch = _fake_torch(cuda=True, name="Radeon RX 7900")

    def unsupported(dev=0):
        raise RuntimeError("mem_get_info is not supported on this device")

    torch.cuda.mem_get_info = unsupported
    monkeypatch.setitem(sys.modules, "torch", torch)

    gpu = sp._probe_gpu()
    assert gpu.kind == sp.GPU_CUDA
    assert gpu.name == "Radeon RX 7900"
    assert (gpu.total_vram_bytes, gpu.free_vram_bytes) == (None, None)
    assert not gpu.has_distinct_vram
    assert sp.gpu_present()


def test_a_card_that_cannot_name_itself_is_still_a_card(monkeypatch) -> None:
    torch = _fake_torch(cuda=True)

    def unnamed(dev=0):
        raise RuntimeError("no device name")

    torch.cuda.get_device_name = unnamed
    monkeypatch.setitem(sys.modules, "torch", torch)

    assert sp._probe_gpu().kind == sp.GPU_CUDA


def test_gpu_present_follows_the_probe(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "torch", _fake_torch())
    assert not sp.gpu_present()
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(mps=True))
    assert sp.gpu_present()


def test_probe_gpu_treats_mps_as_unified(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(mps=True))
    gpu = sp._probe_gpu()
    assert gpu.kind == sp.GPU_MPS
    assert gpu.total_vram_bytes is None
    assert not gpu.has_distinct_vram


def test_probe_gpu_cpu_only(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "torch", _fake_torch())
    assert sp._probe_gpu().kind == sp.GPU_NONE


def test_probe_gpu_tolerates_missing_torch(monkeypatch) -> None:
    # sys.modules[name] = None makes `import name` raise ImportError.
    monkeypatch.setitem(sys.modules, "torch", None)
    gpu = sp._probe_gpu()
    assert gpu.kind == sp.GPU_NONE


def test_probe_system_reports_positive_machine_figures() -> None:
    profile = sp.probe_system()
    assert profile.os_name  # "Linux" / "Windows" / "Darwin"
    assert profile.cpu_logical >= 1
    assert profile.total_ram_bytes > 0
    assert 0 <= profile.available_ram_bytes <= profile.total_ram_bytes
    assert profile.gpu.kind in (sp.GPU_CUDA, sp.GPU_MPS, sp.GPU_NONE)


def test_probe_system_bad_disk_path_is_zeroed(tmp_path) -> None:
    profile = sp.probe_system(tmp_path / "does" / "not" / "exist")
    assert profile.disk_free_bytes == 0
    assert profile.disk_total_bytes == 0


def test_profile_round_trips_through_json() -> None:
    profile = sp.probe_system()
    restored = json.loads(json.dumps(profile.to_dict()))
    assert restored["gpu"]["kind"] == profile.gpu.kind
    assert restored["total_ram_bytes"] == profile.total_ram_bytes


def test_utilisation_vram_percent() -> None:
    u = sp.Utilisation(1, 2, 50.0, 10.0, vram_used_bytes=250, vram_total_bytes=1000)
    assert u.vram_percent == 25.0
    assert sp.Utilisation(1, 2, 50.0, 10.0, None, None).vram_percent is None


def test_utilisation_swap_percent() -> None:
    u = sp.Utilisation(1, 2, 50.0, 10.0, None, None, swap_used_bytes=200, swap_total_bytes=1000)
    assert u.swap_percent == 20.0
    # No swap configured -> None, not a divide-by-zero.
    assert sp.Utilisation(1, 2, 50.0, 10.0, None, None).swap_percent is None


def test_sample_utilisation_reports_swap() -> None:
    u = sp.sample_utilisation()
    assert u.swap_total_bytes >= 0
    # Used can never exceed the pool it comes from. (The previous bound compared
    # used against max(total, used), which holds for any value.)
    assert 0 <= u.swap_used_bytes <= u.swap_total_bytes


def test_format_bytes() -> None:
    assert sp.format_bytes(None) == "\u2014"  # em dash placeholder for unknown
    assert sp.format_bytes(2 * 1024**3) == "2.0 GB"
    assert sp.format_bytes(512 * 1024**2) == "512 MB"
