"""The in-app updater asks the GitHub Releases API for a per-platform binary by a
name built from `resolve_asset_name`. If `release.yml` stops publishing an asset the
updater will request, the update silently 404s. These tests pin the two files
together by driving the real `find_asset_url` against the names the release actually
publishes, so a rename on either side fails here instead of in the field.

The published names are derived from release.yml's build matrix, and the naming
*templates* used to derive them are asserted against the workflow's own commands
(`_assert_naming_templates_unchanged`). Deriving without that check was the hole
this file used to have: the test mirrored the rename scheme in Python, so changing
the scheme in CI left the test passing while real releases published names the
updater could not find.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from deepreefmap_gui.packaging import binary_swap

_FAKE_VERSION = "9.9.9"
_RELEASE_YML = Path(__file__).parents[2] / ".github" / "workflows" / "release.yml"

# The workflow expressions that produce each published name. Substituting the
# label with a literal version is the only transformation the test applies.
_LABEL = "${{ steps.label.outputs.value }}"
_BINARY_TEMPLATE = f'"dist/${{{{ matrix.artifact }}}}-{_LABEL}${{{{ matrix.ext }}}}"'
_SETUP_TEMPLATE = "-replace '^deepreefmap-gui-', 'deepreefmap-gui-setup-'"
_DMG_TEMPLATE = f'"dist/${{{{ matrix.artifact }}}}-{_LABEL}.dmg"'


def _workflow() -> dict:
    return yaml.safe_load(_RELEASE_YML.read_text())


def _assert_naming_templates_unchanged(text: str) -> None:
    """Fail loudly if CI's rename scheme moved out from under the derivation below."""
    for label, template in (
        ("binary rename", _BINARY_TEMPLATE),
        ("windows installer rename", _SETUP_TEMPLATE),
        ("macos dmg name", _DMG_TEMPLATE),
    ):
        assert template in text, (
            f"release.yml's {label} no longer matches {template!r}; "
            "_published_asset_names() below derives published names from it"
        )


def _published_asset_names() -> set[str]:
    """Basenames of every asset a release attaches, derived from the build matrix."""
    text = _RELEASE_YML.read_text()
    _assert_naming_templates_unchanged(text)

    data = _workflow()
    names: set[str] = set()
    for entry in data["jobs"]["build"]["strategy"]["matrix"]["include"]:
        artifact = entry["artifact"]
        ext = entry.get("ext") or ""
        names.add(f"{artifact}-{_FAKE_VERSION}{ext}")
        if str(entry["os"]).startswith("windows"):
            setup = artifact.replace("deepreefmap-gui-", "deepreefmap-gui-setup-", 1)
            names.add(f"{setup}-{_FAKE_VERSION}.exe")
        if str(entry["os"]).startswith("macos"):
            names.add(f"{artifact}-{_FAKE_VERSION}.dmg")
    assert names, "no build matrix entries found in release.yml"
    return names


# Base artifact per runner OS: the matrix entry carrying no variant suffix.
_PLATFORM_BY_OS = {"ubuntu": "linux", "windows": "win32", "macos": "darwin"}


def _run_variants() -> list[tuple[str, bool, str]]:
    """(platform, is_rocm, cuda suffix) for each matrix entry.

    Derived rather than hand-listed so a new matrix variant cannot be added
    without the updater being checked against it. The suffix comes from the
    artifact name -- the ground truth of what gets published -- rather than from
    a second copy of production's torch-version introspection.
    """
    entries = _workflow()["jobs"]["build"]["strategy"]["matrix"]["include"]
    by_os: dict[str, list[str]] = {}
    for entry in entries:
        os_name = str(entry["os"]).split("-")[0]
        assert os_name in _PLATFORM_BY_OS, f"unmapped runner OS {entry['os']!r}"
        by_os.setdefault(os_name, []).append(entry["artifact"])

    variants = []
    for os_name, artifacts in by_os.items():
        base = min(artifacts, key=len)
        for artifact in artifacts:
            suffix = artifact[len(base):]
            variants.append((_PLATFORM_BY_OS[os_name], suffix == "-rocm", "" if suffix == "-rocm" else suffix))
    return variants


def _synthetic_release(names: set[str]) -> dict:
    return {
        "tag_name": f"v{_FAKE_VERSION}",
        "assets": [{"name": n, "browser_download_url": f"https://example.invalid/{n}"} for n in names],
    }


@pytest.mark.parametrize("platform, is_rocm, suffix", _run_variants())
def test_updater_requested_asset_is_published(platform, is_rocm, suffix, monkeypatch) -> None:
    monkeypatch.setattr(binary_swap, "_is_rocm_build", lambda: is_rocm)
    monkeypatch.setattr(binary_swap, "_cuda_variant_suffix", lambda: suffix)

    release = _synthetic_release(_published_asset_names())
    asset_name = binary_swap.resolve_asset_name(platform)
    # Raises BinarySwapError if release.yml publishes nothing matching this request.
    binary_swap.find_asset_url(release, asset_name)


def test_release_glob_attaches_every_built_asset() -> None:
    """The release step uses a glob; check it actually covers the matrix.

    Matching is done by fnmatch against the real pattern. The previous version of
    this check could not fail: every derived name starts with the same prefix the
    glob does, so it matched by construction. Here the glob is required to be a
    prefix/suffix fit for each name *and* to be the pattern release.yml ships.
    """
    import fnmatch
    import os

    names = _published_asset_names()
    patterns: list[str] = []
    for step in _workflow()["jobs"]["release"]["steps"]:
        if not str(step.get("uses", "")).startswith("softprops/action-gh-release"):
            continue
        patterns = [
            os.path.basename(line.strip().replace(_LABEL, _FAKE_VERSION))
            for line in str(step["with"]["files"]).splitlines()
            if line.strip()
        ]
    assert patterns, "no softprops/action-gh-release step found in release.yml"

    unmatched = sorted(n for n in names if not any(fnmatch.fnmatch(n, p) for p in patterns))
    assert not unmatched, f"release.yml files glob does not attach: {unmatched}"

    # And it must not sweep in a different tag's artifacts.
    foreign = "deepreefmap-gui-linux-x64-1.2.3"
    assert not any(fnmatch.fnmatch(foreign, p) for p in patterns), (
        f"the release glob {patterns} also matches {foreign}, from another version"
    )


def test_first_install_assets_are_published() -> None:
    published = _published_asset_names()
    for expected in (
        f"deepreefmap-gui-setup-windows-x64-{_FAKE_VERSION}.exe",
        f"deepreefmap-gui-setup-windows-x64-cu130-{_FAKE_VERSION}.exe",
        f"deepreefmap-gui-macos-arm64-{_FAKE_VERSION}.dmg",
    ):
        assert expected in published, f"release.yml no longer attaches {expected}"
