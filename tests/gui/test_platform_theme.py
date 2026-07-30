"""Native file dialogs come from the xdg-desktop-portal platform theme."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


@pytest.fixture
def bundled_themes(monkeypatch, tmp_path) -> Path:
    """Stand in for the plugin directory the PySide6 wheel ships."""
    import PySide6

    fake_pyside = tmp_path / "PySide6"
    themes = fake_pyside / "Qt" / "plugins" / "platformthemes"
    themes.mkdir(parents=True)
    monkeypatch.setattr(PySide6, "__file__", str(fake_pyside / "__init__.py"))
    return themes


def test_sets_portal_theme_when_wheel_plugin_present(
    monkeypatch, bundled_themes
) -> None:
    from deepreefmap_gui.app import prefer_portal_file_dialogs

    (bundled_themes / "libqxdgdesktopportal.so").write_bytes(b"")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("QT_QPA_PLATFORMTHEME", raising=False)

    prefer_portal_file_dialogs()

    assert os.environ["QT_QPA_PLATFORMTHEME"] == "xdgdesktopportal"


def test_leaves_an_explicit_theme_alone(monkeypatch, bundled_themes) -> None:
    from deepreefmap_gui.app import prefer_portal_file_dialogs

    (bundled_themes / "libqxdgdesktopportal.so").write_bytes(b"")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("QT_QPA_PLATFORMTHEME", "gtk3")

    prefer_portal_file_dialogs()

    assert os.environ["QT_QPA_PLATFORMTHEME"] == "gtk3"


def test_skips_distro_qt_without_bundled_plugin(monkeypatch, bundled_themes) -> None:
    # A distro-packaged PySide6 loads the system Qt plugins, which already carry
    # a working desktop theme.
    from deepreefmap_gui.app import prefer_portal_file_dialogs

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("QT_QPA_PLATFORMTHEME", raising=False)

    prefer_portal_file_dialogs()

    assert "QT_QPA_PLATFORMTHEME" not in os.environ


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_skips_non_linux(monkeypatch, bundled_themes, platform) -> None:
    from deepreefmap_gui.app import prefer_portal_file_dialogs

    (bundled_themes / "libqxdgdesktopportal.so").write_bytes(b"")
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.delenv("QT_QPA_PLATFORMTHEME", raising=False)

    prefer_portal_file_dialogs()

    assert "QT_QPA_PLATFORMTHEME" not in os.environ
