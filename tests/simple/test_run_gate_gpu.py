"""One list of GPU-only backends, read by every gate.

The advanced form, the wizard's run gate and the setup step each decide whether
the chosen mapping backend needs a graphics card. Any of them keeping its own
list is the failure this guards against: the modes disagreeing about which
backends need a GPU the first time the sets diverge.
"""

from __future__ import annotations

import inspect


def test_the_gpu_gate_reads_the_shared_list():
    from deepreefmap_gui.form.panel import FormPanelMixin

    source = inspect.getsource(FormPanelMixin._gpu_only_mapper)
    assert "GPU_ONLY_BACKENDS" in source, "_gpu_only_mapper hardcodes its own list"
    assert '"loger_star"' not in source


def test_the_setup_step_reads_the_shared_list():
    from deepreefmap_gui.simple import setup

    source = inspect.getsource(setup)
    assert "GPU_ONLY_BACKENDS" in source
    assert '"loger_star"' not in source
