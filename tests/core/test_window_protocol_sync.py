"""core/window_protocol.py must keep describing the window app.py actually builds.

The protocol is `TYPE_CHECKING`-only (`MixinBase = object` at runtime), so
nothing executes it and nothing compares it to the real class. mypy checks that
the mixins satisfy it, but it cannot notice a declaration that has quietly
stopped matching, which is why these are AST comparisons over the two sources
rather than assertions against a built window.
"""

from __future__ import annotations

import ast
import collections
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2] / "deepreefmap_gui"
PROTOCOL = PACKAGE / "core" / "window_protocol.py"

# Attributes a mixin assigns and another mixin reads, yet the protocol leaves
# undeclared on purpose. Each entry needs a reason; an empty set is the goal.
_ALLOWED_UNDECLARED: frozenset[str] = frozenset()


def _signals(source: Path, class_name: str) -> dict[str, str]:
    """Every `_sig_* = Signal(...)` in the named class, name to unparsed call."""
    tree = ast.parse(source.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        found = {}
        for stmt in node.body:
            if not isinstance(stmt, ast.Assign) or not isinstance(stmt.value, ast.Call):
                continue
            func = stmt.value.func
            if not (isinstance(func, ast.Name) and func.id == "Signal"):
                continue
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id.startswith("_sig_"):
                    found[target.id] = ast.unparse(stmt.value)
        return found
    raise AssertionError(f"{class_name} not found in {source}")


def test_the_window_and_the_protocol_declare_the_same_signals() -> None:
    """Scenario: a worker needs a new cross-thread signal.

    Expected behaviour: it is declared in both places, with the same argument
    types. PySide6 refuses a second QObject base, so the protocol cannot inherit
    the real declarations and has to restate them; a signal added to one side
    only type-checks fine and fails at the call site instead.
    """
    window = _signals(PACKAGE / "app.py", "DeepReefMapWindow")
    protocol = _signals(PACKAGE / "core" / "window_protocol.py", "MixinBase")

    assert window, "no signals found on DeepReefMapWindow -- the parser is looking in the wrong place"
    assert window == protocol


def _declared_attributes() -> set[str]:
    tree = ast.parse(PROTOCOL.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "MixinBase":
            return {
                s.target.id
                for s in node.body
                if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)
            }
    raise AssertionError("MixinBase not found")


def _is_window_mixin(cls: ast.ClassDef) -> bool:
    """True for the mixin classes fused into DeepReefMapWindow.

    Deliberately excludes the viewer: QtPointCloudViewer is a widget with its own
    _ViewerPickingHost protocol, not part of the window's shared namespace.
    """
    return any(
        isinstance(b, ast.Name) and b.id in {"MixinBase", "QMainWindow"} for b in cls.bases
    )


def _self_attribute_use() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Per attribute, which mixin files assign it and which read it."""
    assigned: dict[str, set[str]] = collections.defaultdict(set)
    read: dict[str, set[str]] = collections.defaultdict(set)
    for path in sorted(PACKAGE.rglob("*.py")):
        if path == PROTOCOL:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
            if not _is_window_mixin(cls):
                continue
            for node in ast.walk(cls):
                if not isinstance(node, ast.Attribute):
                    continue
                if not (isinstance(node.value, ast.Name) and node.value.id == "self"):
                    continue
                if not node.attr.startswith("_"):
                    continue
                bucket = assigned if isinstance(node.ctx, ast.Store) else read
                bucket[node.attr].add(path.relative_to(PACKAGE).as_posix())
    return assigned, read


def test_every_cross_mixin_attribute_is_declared() -> None:
    """Scenario: one mixin assigns `self._foo`, another reads it.

    Expected behaviour: the protocol declares it, so the two agree on its type.
    Undeclared, each mixin infers its own, and mypy never compares them -- which
    is how three model-status widgets and a threading.Event ended up with no
    shared declaration at all, the Event independently assigned in three files.

    hasattr guards at the reading site do not substitute: they narrow the type
    away, so mypy cannot see the gap even in principle.
    """
    declared = _declared_attributes()
    assigned, read = _self_attribute_use()

    cross_mixin = {a for a in assigned if read.get(a, set()) - assigned[a]}
    undeclared = sorted(cross_mixin - declared - _ALLOWED_UNDECLARED)

    assert undeclared == []
