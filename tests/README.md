# Tests

```bash
uv run pytest                      # everything (~45 s; most of it is tests/gui)
uv run pytest --ignore=tests/gui   # everything that needs no main window, ~2 s
uv run pytest --cov=deepreefmap_gui --cov-report=term-missing:skip-covered
```

The viewer tests need a real OpenGL context. If they crash under the `offscreen`
Qt platform, use `xvfb-run -a uv run pytest`. VTK logs shader errors there
without failing; the picking tests only read back geometry, not pixels.

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
- Shared survey builders live in `tests/_factories.py`, on the path via the
  `pythonpath` ini option. Reach for those before writing another `make_transect`.
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
