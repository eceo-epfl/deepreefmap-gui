from deepreefmap.camera.intrinsics import CameraProfile, available_profile_names
from deepreefmap.config.classes import load_classes


def test_default_classes_and_camera_profiles_load_outside_repo_root(tmp_path, monkeypatch) -> None:
    # The packaged binary runs from an arbitrary working directory; the
    # library's bundled resources must resolve without a repo checkout.
    monkeypatch.chdir(tmp_path)

    classes = load_classes()
    profile = CameraProfile.load("gopro_hero_10")

    assert classes.classes
    assert profile.image_size == (1920, 1080)
    assert "gopro_hero_10" in available_profile_names()


def test_bundled_fonts_are_present() -> None:
    # The GUI pins a global Inter font and a JetBrains Mono monospace; both
    # must ship as package data or the app silently falls back to per-OS system
    # fonts (the macOS/Linux size mismatch this was meant to fix).
    from importlib import resources

    from deepreefmap_gui.core.fonts import _FONT_FILES

    fonts_dir = resources.files("deepreefmap_gui.resources").joinpath("fonts")
    for name in _FONT_FILES:
        assert fonts_dir.joinpath(name).is_file(), f"missing bundled font {name}"
