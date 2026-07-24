# DeepReefMap GUI

**Native desktop app for [deepreefmap](https://github.com/eceo-epfl/deepreefmap): rapid 3D semantic mapping of coral reefs.**

A PySide6 application with a VTK point cloud viewer, live reconstruction progress, survey planning, batch processing, model management, and in-app updates. All reconstruction runs in the deepreefmap library; this repository is the interface.

## Install

Grab the latest build for your platform from the [releases page](https://github.com/eceo-epfl/deepreefmap-gui/releases).

| Platform | File | Variants |
|---|---|---|
| Windows | `deepreefmap-gui-setup-windows-x64-<version>.exe` | `-cu130` for RTX 50-series |
| macOS (Apple Silicon) | `deepreefmap-gui-macos-arm64-<version>.dmg` | |
| Linux | `deepreefmap-gui-linux-x64-<version>` | `-cu130` for RTX 50-series, `-rocm` for AMD |

On macOS the app is unsigned, so the first launch needs System Settings > Privacy & Security > "Open Anyway". On Linux, `chmod +x` the binary and run it. The first launch installs a private Python environment (several GB); after that, update or roll back from the Updates tab.

## Development

deepreefmap resolves from the git commit pinned in `pyproject.toml`, so no separate checkout is needed.

```bash
uv sync --extra dev
uv run deepreefmap-gui
```

GPU variants: `--extra cu126` (up to RTX 40-series), `--extra cu130` (RTX 50-series), `--extra rocm` (AMD, Linux only).

Run the tests with `uv run pytest`. The viewer tests need a real OpenGL context; if they crash under the `offscreen` Qt platform, use `xvfb-run -a uv run pytest`.

## Build

`scripts/build.sh` (Linux/macOS) and `scripts/build.ps1` (Windows) wrap `uv build` and [PyApp](https://github.com/ofek/pyapp) into a self-provisioning binary. Shared settings live in `scripts/build_config.env`.

## License

Apache-2.0. Qt bindings via PySide6 (LGPL); bundled fonts and third-party components are listed in `THIRD_PARTY_NOTICES.md`.
