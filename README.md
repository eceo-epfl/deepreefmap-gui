# DeepReefMap GUI
The desktop application for [deepreefmap](https://github.com/eceo-epfl/deepreefmap): 3D semantic mapping of coral reefs from dive footage.

## Getting started

Download the build for your platform from the [releases page](https://github.com/eceo-epfl/deepreefmap-gui/releases).

| Platform | File | Variants |
|---|---|---|
| Windows | `deepreefmap-gui-setup-windows-x64-<version>.exe` | `-cu130` for RTX 50-series |
| macOS (Apple Silicon) | `deepreefmap-gui-macos-arm64-<version>.dmg` | |
| Linux | `deepreefmap-gui-linux-x64-<version>` | `-cu130` for RTX 50-series, `-rocm` for AMD |

macOS builds are unsigned, so the first launch needs System Settings > Privacy & Security > "Open Anyway"; Linux builds need `chmod +x`. The first launch provisions its own Python environment (several GB). Updates and rollbacks are in the System tab.

The app opens in survey mode: plan the transects, then run them as a batch. Advanced mode exposes the full run form and the viewer controls. Models are downloaded from the Environment step, or imported from a USB pack for machines that stay offline; the `coralscapes-*` models need a free Hugging Face account.

## Settings

Survey mode runs from a preset YAML, `deepreefmap_gui/resources/configs/survey_preset.yaml`. `segmentation_name` picks the segmentation model (`coralscapes-vit-{s,b,l}-dpt`, `segformer-b{2,5}`) and `mapping_name` the reconstruction backend (`loger_star` and `loger` need a GPU, `scsfmlearner` runs on CPU). Transect length and time trim come from the survey database, not the preset. To standardise several machines on one configuration, point them at a shared copy with `DEEPREEFMAP_SURVEY_PRESET`; the file's header comment covers the rest.

## Development

deepreefmap resolves from the commit pinned in `pyproject.toml`, so no separate checkout is needed.

```bash
uv sync --extra dev          # add --extra cu126, cu130 or rocm for GPU
uv run deepreefmap-gui
uv run pytest                # xvfb-run -a uv run pytest if the viewer tests crash headless
```

`scripts/build.sh` (Linux/macOS) and `scripts/build.ps1` (Windows) produce the release binaries.

## License

Apache-2.0. Qt bindings via PySide6 (LGPL); bundled fonts and third-party components are listed in `THIRD_PARTY_NOTICES.md`.
