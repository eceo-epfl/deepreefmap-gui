"""Register the bundled fonts and pin the app's base font, before any widget exists.

Layouts here are tuned against a known metric. Left to the platform, the app gets a different
family at a different base size on each OS (macOS .AppleSystemUIFont at 13pt against Linux "Sans
Serif" at 9pt), so a panel that fits on the machine it was built on overflows on the next one, and
a bare "monospace" request resolves to nothing at all on macOS.

Failure is deliberately soft: a font the packaged build did not ship is logged and skipped, and
the app falls back to the platform default rather than refusing to start over typography.
"""

from __future__ import annotations

import logging
from importlib import resources

from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication

logger = logging.getLogger(__name__)

# Bundled so the UI renders identically on Linux/Windows/macOS. Without this the
# app inherits each OS's default UI font at a different base size (macOS
# .AppleSystemUIFont 13pt vs Linux "Sans Serif" 9pt), so layouts tuned on one
# platform overflow on another. A bare "monospace" family has no match on macOS.
UI_FONT_FAMILY = "Inter"
MONO_FONT_FAMILY = "JetBrains Mono"
BASE_POINT_SIZE = 10

_FONT_FILES = (
    "Inter-Regular.ttf",
    "Inter-Medium.ttf",
    "Inter-SemiBold.ttf",
    "Inter-Bold.ttf",
    "JetBrainsMono-Regular.ttf",
    "JetBrainsMono-Bold.ttf",
)


def apply_app_fonts(app: QApplication) -> None:
    """Register the bundled fonts and pin a consistent global UI font."""
    fonts_dir = resources.files("deepreefmap_gui.resources").joinpath("fonts")
    loaded_any = False
    for name in _FONT_FILES:
        try:
            with resources.as_file(fonts_dir.joinpath(name)) as path:
                font_id = QFontDatabase.addApplicationFont(str(path))
        except Exception:
            logger.warning("Could not load bundled font %s", name, exc_info=True)
            continue
        if font_id < 0:
            logger.warning("QFontDatabase rejected bundled font %s", name)
        else:
            loaded_any = True

    if not loaded_any:
        logger.warning("No bundled fonts loaded; keeping system default font")
        return
    app.setFont(QFont(UI_FONT_FAMILY, BASE_POINT_SIZE))
