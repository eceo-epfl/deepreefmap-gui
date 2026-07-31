"""No two mixins may define the same method name.

Scenario: DeepReefMapWindow fuses 16 mixins. When two define the same name, MRO
silently picks one and the other becomes dead code that looks live -- editing it
changes nothing, and the reader has no signal that it is not the one running.

Expected behaviour: every method has exactly one definition. This has gone wrong
twice: `_cancel_load` existed identically in ViewerControlsMixin and
RunLoadingMixin, and `_run_in_flight` was added to the window itself while
RunLoadingMixin already had it with four other callers. The second was caught
only because a test passed when it was expected to fail.
"""

from __future__ import annotations

import inspect


def _gui_mixins():
    from deepreefmap_gui.app import DeepReefMapWindow

    return [
        base
        for base in DeepReefMapWindow.__mro__
        if base.__module__.startswith("deepreefmap_gui") and base is not DeepReefMapWindow
    ]


def _methods(cls) -> dict[str, object]:
    return {
        name: value
        for name, value in vars(cls).items()
        if inspect.isfunction(value) and not name.startswith("__")
    }


def test_no_two_mixins_define_the_same_method():
    owner: dict[str, str] = {}
    clashes: dict[str, list[str]] = {}
    for base in _gui_mixins():
        for name in _methods(base):
            if name in owner:
                clashes.setdefault(name, [owner[name]]).append(base.__name__)
            else:
                owner[name] = base.__name__

    assert clashes == {}, (
        "MRO picks one and silently discards the rest: "
        + "; ".join(f"{n} in {', '.join(w)}" for n, w in sorted(clashes.items()))
    )


def test_the_window_does_not_shadow_a_mixin_method():
    """A class's own method beats every mixin's, so defining one that already
    exists overrides it app-wide with no error and no warning."""
    from deepreefmap_gui.app import DeepReefMapWindow

    mixin_names = {name for base in _gui_mixins() for name in _methods(base)}
    shadowed = sorted(set(_methods(DeepReefMapWindow)) & mixin_names)

    assert shadowed == [], f"window methods overriding a mixin: {shadowed}"
