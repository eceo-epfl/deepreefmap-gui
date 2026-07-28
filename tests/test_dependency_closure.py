"""Every module-level third-party import must be a declared runtime dependency.

Scenario: a dependency the app imports but does not declare resolves anyway,
because something else in the tree happens to pull it in. Expected behaviour: the
packaged binary must not depend on that coincidence — an upstream change should
break resolution at install time, not raise ImportError on a field laptop.

Only module-level imports are checked. Importing torch, vtkmodules and friends
inside a function is a deliberate pattern here: it keeps startup fast and lets the
app run with those absent, so those are exempt by design.
"""

from __future__ import annotations

import ast
import sys
from importlib.metadata import packages_distributions, requires
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent.parent / "deepreefmap_gui"


def _canonical(name: str) -> str:
    """PEP 503 normalisation: huggingface_hub and huggingface-hub are one name."""
    return name.lower().replace("_", "-").replace(".", "-")


def _declared_distributions() -> set[str]:
    """Distributions in [project.dependencies], read back from installed metadata.

    Reading the metadata rather than the TOML keeps this working on 3.10, which
    has no tomllib, and checks what a consumer would actually receive.
    """
    declared = set()
    for req in requires("deepreefmap-gui") or []:
        # "torch==2.12.1+cu126; extra == \"cu126\"" -> an extra, not a runtime dep
        base, _, marker = req.partition(";")
        if "extra ==" in marker:
            continue
        name = base.strip().split("[")[0]
        for sep in ("<", ">", "=", "!", "~", " ", "@"):
            name = name.split(sep)[0]
        if name:
            declared.add(_canonical(name))
    return declared


def _module_level_imports() -> dict[str, set[str]]:
    """Top-level import roots to the "file:line" sites that import them."""
    roots: dict[str, set[str]] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                root = name.split(".")[0]
                if root in sys.stdlib_module_names or root == PACKAGE.name:
                    continue
                roots.setdefault(root, set()).add(f"{path.name}:{node.lineno}")
    return roots


def test_module_level_imports_are_declared_dependencies() -> None:
    declared = _declared_distributions()
    to_distributions = packages_distributions()

    undeclared: list[str] = []
    for root, sites in sorted(_module_level_imports().items()):
        providers = to_distributions.get(root)
        if providers is None:
            pytest.skip(f"{root} is imported but not installed; cannot map it to a distribution")
        if not any(_canonical(dist) in declared for dist in providers):
            example = sorted(sites)[0]
            undeclared.append(f"{root} (provided by {providers}, e.g. {example})")

    assert not undeclared, (
        "These are imported at module level but are not declared in "
        "[project.dependencies], so they only resolve transitively:\n  "
        + "\n  ".join(undeclared)
    )
