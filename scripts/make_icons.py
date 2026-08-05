"""Generate icon files from the bundled PNG.

Writes dist/icon.ico (multi-resolution, consumed by the Inno Setup installer for
the Start Menu / desktop shortcuts and the Add/Remove Programs entry), and
deepreefmap_gui/resources/icon.icns, which ships as package data so the macOS
wrapper bundle can be written on any host. scripts/make_app_bundle.sh builds the
distributed .app's icon with sips + iconutil and still needs a macOS host; this
one only has to be regenerated when icon.png changes. Run with Pillow available:

    uvx --with pillow python scripts/make_icons.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

REPO = Path(__file__).resolve().parent.parent
SOURCE = REPO / "deepreefmap_gui" / "resources" / "icon.png"
SIZES = [16, 24, 32, 48, 64, 128, 256]


def main() -> None:
    image = Image.open(SOURCE).convert("RGBA")

    ico = REPO / "dist" / "icon.ico"
    ico.parent.mkdir(exist_ok=True)
    image.save(ico, format="ICO", sizes=[(s, s) for s in SIZES])
    print(f"Wrote {ico}")

    icns = SOURCE.with_suffix(".icns")
    image.save(icns, format="ICNS")
    print(f"Wrote {icns}")


if __name__ == "__main__":
    main()
