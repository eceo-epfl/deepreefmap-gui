# DeepReefMap GUI

**Native desktop application for [deepreefmap](https://github.com/EPFL-ECEO/deepreefmap), rapid 3D semantic mapping of coral reefs.**

A PySide6 application with a VTK point cloud viewer, live reconstruction progress, survey planning on a map, batch processing, model management, and in-app updates. All reconstruction happens in the deepreefmap library; this repository is the interface.

## Install

CI builds packages for every release. Grab yours from the [releases page](https://github.com/eceo-epfl/deepreefmap-gui/releases).

| Platform | File | Variants |
|---|---|---|
| Windows | `deepreefmap-gui-setup-windows-x64-<version>.exe` | `-cu130` for RTX 50-series |
| macOS (Apple Silicon) | `deepreefmap-gui-macos-arm64-<version>.dmg` | |
| Linux | `deepreefmap-gui-linux-x64-<version>` | `-cu130` for RTX 50-series, `-rocm` for AMD |

On Windows, run the installer. It installs per-user (no admin needed) and adds a Start Menu entry plus an uninstaller. Uninstalling keeps your outputs in `Documents\DeepReefMap` and asks before deleting downloaded models.

On macOS, open the dmg and drag DeepReefMap to Applications. The app is not signed yet, so the first launch needs System Settings > Privacy & Security > "Open Anyway".

On Linux, `chmod +x` the binary and run it. Use "Add to applications menu" in the Updates tab to register it in your launcher.

The first launch installs a private Python environment (several GB, takes a few minutes). After that, install newer versions or roll back from the Updates tab; the binary is swapped in place, so shortcuts keep working. The plain binaries on the release page are what the updater downloads, and also run standalone without the installer. The installed binary exposes the full deepreefmap CLI (ie. `deepreefmap-gui.exe reconstruct --help`).

## Development

The dev dependency on deepreefmap is an editable path source, so keep a checkout of [deepreefmap](https://github.com/EPFL-ECEO/deepreefmap) at `../deepreefmap`.

```bash
uv sync --extra dev
uv run deepreefmap-gui
```

GPU variants:

```bash
uv sync --extra cu126   # NVIDIA, up to RTX 40-series
uv sync --extra cu130   # RTX 50-series (Blackwell)
uv sync --extra rocm    # AMD, Linux only
```

Run the tests with `uv run pytest`.

## Build

`scripts/build.sh` (Linux/macOS) and `scripts/build.ps1` (Windows) wrap `uv build` and [PyApp](https://github.com/ofek/pyapp) to produce a self-provisioning binary. Shared settings live in `scripts/build_config.env`; `scripts/make_app_bundle.sh` produces the macOS app bundle and dmg.

## License

Apache-2.0. Qt bindings are provided by PySide6 under the LGPL; bundled fonts and other third-party components are listed in `THIRD_PARTY_NOTICES.md`.
