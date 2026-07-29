"""The simple wizard's Process button and GPU availability.

Scenario: a CPU-only laptop opens the wizard. The bundled preset maps with
loger_star, which is CUDA-only.

Expected behaviour: the batch is blocked before it starts, with a reason. It used
to start and fail every pass in turn, having said nothing beforehand -- the
advanced form has checked this all along.
"""

from __future__ import annotations

from deepreefmap_gui.simple.progress import BLOCKED, OK, run_gate


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


def test_a_missing_gpu_blocks_the_batch():
    state = gate(gpu_missing=True)

    assert state.state == BLOCKED
    assert "GPU" in state.reason


def test_a_present_gpu_does_not_block():
    assert gate(gpu_missing=False).state == OK


def test_missing_models_are_reported_before_the_gpu():
    """Both are blockers and only the first is shown. Weights are the thing the
    user can act on without changing settings, so they come first."""
    state = gate(gpu_missing=True, missing_models=["loger_star"])

    assert "Download" in state.reason


def test_unassigned_passes_still_come_first():
    state = gate(unassigned=2, gpu_missing=True)

    assert "transect" in state.reason


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
