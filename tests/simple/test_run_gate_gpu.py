"""The simple wizard's Process button and GPU availability.

Scenario: a CPU-only laptop opens the wizard. The bundled preset maps with
loger_star, which is CUDA-only.

Expected behaviour: the wizard says so before the batch starts, and still lets it
run. It used to say nothing at all and fail every pass in turn; briefly it blocked
outright, which left a field laptop with no way to try at all.
"""

from __future__ import annotations

from deepreefmap_gui.simple.progress import ATTENTION, OK, run_gate


def gate(**overrides):
    kwargs = {
        "pass_count": 1,
        "unassigned": 0,
        "remaining": 1,
        "failed": 0,
        "has_preset": True,
        "missing_models": [],
    }
    kwargs.update(overrides)
    return run_gate(**kwargs)


def test_a_missing_gpu_warns_without_blocking():
    """ATTENTION, not BLOCKED: _refresh_survey_actions only disables the button
    on BLOCKED, so this warns and still lets the user try."""
    state = gate(gpu_missing=True)

    assert state.state == ATTENTION
    assert "GPU" in state.reason


def test_a_present_gpu_says_nothing():
    assert gate(gpu_missing=False).state == OK


def test_missing_models_are_reported_before_the_gpu():
    """Weights are a real blocker and the GPU is a warning, so the blocker wins
    regardless of order."""
    state = gate(gpu_missing=True, missing_models=["loger_star"])

    assert "Download" in state.reason


def test_unassigned_passes_still_come_first():
    state = gate(unassigned=2, gpu_missing=True)

    assert "transect" in state.reason


def test_a_failed_pass_is_reported_before_the_gpu_warning():
    """Both are ATTENTION and run_gate returns on the first match. `failed` is
    only non-zero after a run, so the GPU warning shows beforehand and the more
    specific failure count takes over once there is one."""
    state = gate(gpu_missing=True, failed=2)

    assert state.state == ATTENTION
    assert "failed" in state.reason
    assert "GPU" not in state.reason


class _Preset(dict):
    pass


class _Window:
    """The two attributes _survey_gpu_missing reads."""

    def __init__(self, mapping_name, gpu):
        self._survey_preset = _Preset(mapping_name=mapping_name)
        self._gpu = gpu

    def _gpu_available(self):
        return self._gpu

    _survey_gpu_missing = None  # replaced below


def _make(mapping_name, gpu):
    from deepreefmap_gui.simple.batch import SimpleBatchMixin

    window = _Window(mapping_name, gpu)
    return SimpleBatchMixin._survey_gpu_missing(window)


def test_a_cuda_only_backend_without_a_gpu_is_missing():
    assert _make("loger_star", gpu=False) is True
    assert _make("loger", gpu=False) is True


def test_a_cuda_only_backend_with_a_gpu_is_fine():
    assert _make("loger_star", gpu=True) is False


def test_a_cpu_backend_never_needs_one():
    assert _make("scsfmlearner", gpu=False) is False


def test_both_gpu_gates_read_one_list_of_backends():
    """The advanced form and the wizard disagreeing about which backends need a
    GPU is the failure this constant exists to prevent.

    Scoped to the two gates: panel.py also lists the same backends for a
    different question -- which ones the `loger` install extra provides -- and
    folding that into a GPU constant would be wrong the first time the two sets
    diverge.
    """
    import inspect

    from deepreefmap_gui.form.panel import FormPanelMixin
    from deepreefmap_gui.simple.batch import SimpleBatchMixin

    for func in (FormPanelMixin._recompute_submit_state, SimpleBatchMixin._survey_gpu_missing):
        source = inspect.getsource(func)
        assert "GPU_ONLY_BACKENDS" in source, f"{func.__qualname__} hardcodes its own list"
        assert '"loger_star"' not in source
