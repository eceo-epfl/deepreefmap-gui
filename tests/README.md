# Tests

```bash
uv run pytest                      # everything (~30 s across a desktop's cores)
uv run pytest -n0                  # one process, ~2 min, readable on a crash
uv run pytest --ignore=tests/gui   # everything that needs no main window, ~2 s
uv run pytest --cov=deepreefmap_gui --cov-report=term-missing:skip-covered
```

`-n auto` is in `addopts`, and CI passes `-n 2` over it. Use `-n0` to read a
worker crash, or to bisect an ordering bug.

Nothing appears on screen. `tests/conftest.py` starts a private `Xvfb` and points
`DISPLAY` at it, falling back to the `offscreen` platform where there is neither
Xvfb nor a display, which is the CI path. Xvfb first because the viewer tests
need a real OpenGL context and segfault under `offscreen` on a machine with no
software GL. `QT_QPA_PLATFORM=xcb uv run pytest` puts the windows back on the
real screen to watch a run.

The conftest also stubs the file-manager reveal helpers, so no click opens a real
file manager on `tmp_path`. `tests/core/test_reveal.py` opts back in with the
`real_reveal` marker.

## Layout

Directories mirror `deepreefmap_gui/` subpackages. The split that matters is
**`tests/gui/` versus everything else**: the autouse fixtures in
`tests/gui/conftest.py` build a `QApplication` and a full `DeepReefMapWindow`, so
a test only belongs there if it genuinely needs a window. Pure logic goes in the
directory named after its module and stays fast. A standalone widget or dialog
can take the root `qapp` fixture from wherever it lives, and that costs a
`QApplication`, not a window.

`tests/e2e/update_e2e.sh` is not run by pytest. It builds two real binaries and
swaps one for the other; CI runs it from `.github/workflows/e2e.yml` on every
push, and `--interactive` drives it locally.

## Conventions

- No `__init__.py` anywhere. `--import-mode=importlib` (see `pyproject.toml`)
  means subfolders need none and test basenames need not be globally unique.
- Builders more than one directory needs live in `tests/_factories.py`, on the
  path via the `pythonpath` ini option: survey rows, HF cache repos, scenes.
  Reach for those before writing another `make_transect`. A builder one file
  uses stays in that file; a fixture one directory uses goes in its `conftest.py`.
- `Scenario:` / `Expected behaviour:` docstrings when the intent isn't obvious
  from the code; nothing when it is.
- Assert against production constants (`theme.BLOCK`, `STAGE_SPANS`), not copies
  of their values, so the two move together.
- Don't reimplement the code under test in order to check it. If a test needs the
  production formula to compute its expectation, call the production code and
  assert on an observable instead.

## Things that would otherwise bite

- **torch gates the GUI suite.** `tests/gui/conftest.py::require_torch` skips
  locally when torch is absent, which would quietly skip most of the suite. CI
  sets `DEEPREEFMAP_REQUIRE_TORCH=1` to turn that skip into a failure.
- **Nothing may touch your real config or data dirs.** `tests/conftest.py`
  redirects `QSettings` and the run-timings profile; `tests/survey/conftest.py`
  redirects the survey preset. `tests/gui/test_qsettings_sandbox.py` guards the
  first of those: if it fails, tests are writing to your home directory.
- **No test may reach the network.** Release/update tests serve their own
  `HTTPServer` on loopback. A test that asserts a failure result would otherwise
  pass offline for the wrong reason.
