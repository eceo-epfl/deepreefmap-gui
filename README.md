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

## Changing survey settings

Survey mode processes every pass with one set of run settings. To change them on
a machine, open the Run step and click **Edit settings…**, which opens the full
run form. Accepting it keeps the change; the Run step names anything that differs
from the standard.

To change them for a whole programme, edit the preset YAML. The models are set by
two keys:

| Key | Selects | Values |
|---|---|---|
| `segmentation_name` | coral identification model | `coralscapes-vit-{s,b,l}-dpt`, `segformer-b{2,5}` |
| `mapping_name` | processing method | `loger_star`, `loger`, `scsfmlearner` |

`loger` and `loger_star` need a graphics card; `scsfmlearner` runs on CPU. The
`coralscapes-*` models are gated on Hugging Face and need a free account to
download. `fps`, `camera_profile_name` and `transect_crop_width` sit alongside
these; per-pass values (transect length, time trim) come from the survey
database, never the preset.

Any model named here must also be downloaded. The Environment step lists what the
current settings need and offers both a download and a USB import.

## Administering survey settings

Survey mode runs from an **organisation preset**: the blessed run settings, named
and versioned, that every machine in a programme measures with. The shipped one is
`deepreefmap_gui/resources/configs/survey_preset.yaml`.

To publish your own, copy that file, set `preset_name` and `preset_version`, and
point every machine at it:

```bash
export DEEPREEFMAP_SURVEY_PRESET=/srv/reef/org_preset.yaml
```

Naming a file this way **locks** it. The Run step then shows the preset by name
and version, and a field machine may differ only on the settings that describe
the computer rather than the method:

| Setting | Why a machine may change it |
|---|---|
| Processing method (`mapping_name`) | a machine with no graphics card cannot run the standard method |
| Frames processed at once (`preprocess_batch_size`) | how much this machine's memory holds |
| Camera (`camera_profile_name`) | which camera this team dives with |
| `loger_model_path`, `scs_checkpoint_path` | where the weights sit on this disk |

Everything else moves the cover numbers, so it stays the organisation's call.
Editing those in advanced mode applies for the session, is named on the Run step
as a deviation, and is not written back. `MACHINE_OVERRIDABLE_KEYS` in
`deepreefmap_gui/survey/preset.py` is the allow-list.

A machine's own changes live in `<user data dir>/deepreefmap/survey_preset.yaml`
and hold only that short list. **Restore standard settings** on the Run step
deletes it. Every run records the preset name, version and content hash it used,
plus any deviation, under `survey.provenance.config` in `run_manifest.json`;
**Settings history…** lists them against the current standard.

### Deferred: signed presets and update-channel distribution

Two pieces of this are designed but not built.

**Signing.** `DEEPREEFMAP_SURVEY_PRESET` establishes intent, not authenticity: a
diver can point it at a file they wrote. The intended shape is a detached
signature beside the preset (`org_preset.yaml.sig`), verified against a public key
compiled into the binary, with `OrgPreset` carrying a `verified: bool` that the
Run step and the manifest's `preset_source` both report. That needs a key-custody
decision (who holds the private key, how it rotates) before any code, so the
verification hook is deliberately absent rather than stubbed.

**Update channel.** `packaging/` swaps binaries from GitHub Releases and does not
carry configuration. Shipping a preset through it would let an administrator
publish v3 of a preset the way they publish a new version of the app: attach the
signed preset to the release, have the release check fetch it alongside the
version list, and stage it into the user data dir for the next launch to adopt.
The preset's own `preset_version` already gives the adopt step something to
compare, and `manifest_config_block` already records which version produced a run,
so past numbers stay attributable across an update. What is missing is the fetch,
the staging file and the operator story for a machine that must stay on an older
preset mid-season.

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
