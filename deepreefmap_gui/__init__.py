"""DeepReefMap desktop application: plan a reef survey, process the day's videos, browse the results.

`cli.py::main` takes an optional run directory and calls `app.py::launch`, which prepares the Qt
platform and builds one window. There is one QApplication, one window and no second process:
everything below hangs off `app.py::DeepReefMapWindow`.

The reconstruction itself is not here. It lives in the `deepreefmap` library, called in-process
from `runs/loading.py`; this package is the interface around it, plus everything the library has
no opinion about (which runs exist, which transect they belong to, whether this laptop can run
another one).

## Where the code for a feature is

The window is a single class fused from 17 mixins, so a feature is a file rather than a widget
subtree, and this table is the index. `class <Mixin>` is the only grep you need after it:

| What you see                                          | Mixin                | File                 |
| ----------------------------------------------------- | -------------------- | -------------------- |
| Transects: the lines, the map, imports                | SimplePlanMixin      | simple/plan.py       |
| Process: videos become passes, and run                | SimpleBatchMixin     | simple/batch.py      |
| Repeat-pass comparison, shown under Transects         | SimpleAnalysisMixin  | simple/analysis.py   |
| Browse: the run archive, its rail, table and detail   | BrowseMixin          | runs/browse.py       |
| The shell: destinations, settings dialog, the store   | InterfaceShellMixin  | simple/mode.py       |
| Setup: the destination and its header button          | SimpleMachineMixin   | simple/machine.py    |
| Readiness rows: graphics card, models, disk           | SimpleSetupMixin     | simple/setup.py      |
| System gauges and the no-video benchmark              | SystemPanelMixin     | system/system_tab.py |
| Model status, download, delete, HuggingFace login     | ModelManagementMixin | models/cache_ui.py   |
| Model packs: export, import, reveal the cache         | ModelLibraryMixin    | models/packs_ui.py   |
| The run settings form and the bottom status strip     | FormPanelMixin       | form/panel.py        |
| Progress bars, the ETA, the status line               | ProgressBarsMixin    | runs/progress.py     |
| Opening a cached run, pause and stop                  | RunLoadingMixin      | runs/loading.py      |
| The run banner and the workspace reset                | PastRunsMixin        | runs/past_runs.py    |
| Results: ortho preview, transect crop, exports, cover | ResultsMixin         | runs/results.py      |
| Viewer controls: playback, legend, picking, app mode  | ViewerControlsMixin  | viewer/controls.py   |
| Update check and install, the desktop entry toggle    | VersionCheckMixin    | update/version.py    |

Widgets those mixins build but do not own are their own modules beside them (`runs/run_table.py`,
`runs/run_detail.py`, `viewer/legend.py`, and so on). Each subpackage's `__init__.py` states its
contract in two lines; read that before adding a file to it.

## The mixin contract

The 17 mixins share one `self`. None of them has an `__init__`: state appears when whichever
`_build_*` method needs it first runs, which is why so many readers go through `getattr` and why
timers are created lazily. Three things hold that arrangement together.

- `core/window_protocol.py` types it. `MixinBase` declares every attribute and method one mixin
  assigns and another reads, so mypy can resolve a cross-file `self._foo`. It is
  `TYPE_CHECKING`-only: at runtime `MixinBase is object`, and PySide6 would refuse a second
  QObject base anyway, which is why the signals are restated there rather than inherited.
- `tests/core/test_window_protocol_sync.py` keeps it honest, by AST comparison rather than against
  a built window (nothing executes the protocol, so nothing else would notice it drifting). An
  attribute assigned in one mixin and read in another, but undeclared, fails it.
- `tests/gui/test_no_mixin_shadowing.py` forbids two mixins defining the same method name. The MRO
  picks one and silently discards the rest, so the loser is dead code that reads as live.

Cross-thread work goes through the `_sig_*` signals declared on `DeepReefMapWindow`; see `app.py`
for why they are declared there and what connects to what. `QTimer.singleShot` from a worker
thread does nothing at all, which is the failure that convention exists to prevent.

## Qt-free modules and their Qt layer

`x.py` holds logic with no Qt import; `x_ui.py` is the layer that puts it on screen. `models/`
follows it literally: `packs.py` is the model-pack format and the file copying, `packs_ui.py`
is the dialogs and the mixin that drive them. Prefer the suffix for new splits.

The same split exists under older names elsewhere, and those stay: `survey/` is the Qt-free domain
layer under `simple/`'s UI, `simple/section_state.py` is the pure verdict behind the header badges,
`models/cache.py` is the pure side of `models/cache_ui.py`. `io/`, `packaging/`, `profiling/`,
`cover.py` and `paths.py` are pure throughout.

The point is not purity. It is that a pure module is tested in `tests/<package>/` in milliseconds,
where a Qt one needs `tests/gui/` and a real window.

## Names for the same thing

Several features answer to two or three names. Prefer the first in prose, comments and UI text.

- **Destination**, not workspace, step or tab. There are three (Transects, Process, Browse) and
  none gates another, so none of them is a step. `DESTINATIONS` in `simple/mode.py` is the list;
  Setup and View are sections of the same stack but are not destinations, which is why
  `SIMPLE_SECTIONS` is the longer tuple.
- **Survey**, not simple. `simple/` is the historical package name from when there were Simple and
  Advanced interfaces; the toggle is gone and the package was not renamed. `survey/` is a
  different thing again: the Qt-free domain layer (transects, passes, the store, the preset) that
  `simple/` draws.
- **Browse**, not data. The destination, the file and the mixin all say Browse; only the widget
  attributes are still `_data_*`, which is the last of an older name and not worth the churn of a
  rename on its own. Nothing user-facing says "data".
- **Setup**, for the destination holding everything about this computer: whether it can process a
  dive, which models are on it, what it is doing while it runs. Its first view is **Readiness**,
  three rows on whether this laptop can process a dive. The file is `simple/setup.py`; the mixin
  is `SimpleMachineMixin` in `simple/machine.py`, which is the label's old name.
- **Pass**, not clip or video, for one traversal of a transect. A **run** is one reconstruction of
  one pass (a rerun makes a second run of the same pass), and a **session** is the set of passes
  queued together. `survey/models/` names all three, though the session's class is still
  `SurveyBatch` and its column is still `survey_batch`: the schema name is load-bearing for
  `rebuild_from_scan`, so only the UI says session.
- **Run** is only ever a finished output. The destination that produces one is Process, so the
  word never names a queue.

## Things that would otherwise bite

- **The run form is never on screen.** `form/panel.py` builds it into a hidden holder and lends it
  to the run settings dialog. `_collect_run_settings()` reads exactly those widgets, so a setting
  with no widget there cannot reach a run, whether or not the dialog is open.
- **Panels are lent, not rebuilt.** Browse, the model library, the system panel and the results
  panel are each one widget re-parented into whichever destination shows them. Two copies would
  disagree the moment a download or a path edit landed against only one.
- **Adding a shared attribute is a two-file change.** The mixin that assigns it, and
  `core/window_protocol.py`. Skip the second and the sync test fails; reach for a `hasattr` guard
  instead and mypy stops being able to see the gap even in principle.
- **Read `tests/README.md` before writing a test here.** The `tests/gui/` fixtures build a whole
  window, so a pure test placed there costs seconds per test for nothing.
"""
