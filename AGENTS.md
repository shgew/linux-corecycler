# Repository Guidelines

## Project Overview

CoreCycler is a Linux PySide6/Qt6 desktop app (plus a headless CLI) that stress-tests **one physical core at a time at full single-threaded boost** and tunes AMD PBO **Curve Optimizer** offsets per core via the `ryzen_smu` sysfs mailbox. Its centrepiece is an automatic, crash-safe **tuner** that searches each core's most aggressive stable undervolt.

Safety claims are load-bearing, not marketing. Treat these as invariants when editing:

- CO writes are volatile SMU SRAM overlays. There is no path to BIOS/UEFI NVRAM; a reboot restores stock.
- Stress testing alone writes no CO, voltage, or frequency.
- CO is written in exactly two places: `gui/smu_tab.py` (confirmation dialog, dry-run, backup/restore) and `tuner/engine.py`. They are mutually exclusive; the tab locks while the tuner runs.
- Fail closed. Missing thermal sensor, unreadable forensics, ambiguous core map, or absent cgroup containment must refuse or pause, never proceed optimistically.

## Architecture & Data Flow

`main.py` picks GUI vs headless; both converge on the same execution engine.

```mermaid
graph LR
  A[main.py] -->|command| B[cli.py cmd_run]
  A -->|no command| C[gui/main_window.py]
  B --> D[tuner/engine.py TunerEngine]
  C --> E[TestWorker QThread]
  D --> F[engine/scheduler.py CoreScheduler]
  D --> G[engine/parallel.py ParallelStress]
  E --> F
  F --> H[engine/execution.py Supervisor + Lane]
  G --> H
  H --> I[engine/containment.py systemd-run scope]
  I --> J[backends/* subprocess]
  H --> K[engine/detector.py dmesg MCE]
  D --> L[tuner/persistence.py -> history/db.py SQLite]
```

Headless call chain: `main:main` -> `cli.cli_main` -> `cmd_run` (QCoreApplication, `QLockFile`, `TunerConfig.validate`, `backends.load_all`, `detect_topology`, `_build_smu`) -> `TunerEngine.start|resume` -> `_run_next` -> `_start_worker` -> `CoreScheduler.run` -> `Supervisor.run` -> `containment.contain` -> `subprocess.Popen`.

Supervisor polling checks, per tick: thermal state, dmesg MCEs, backend live errors, process exit, cgroup placement (escape watchdog), CPU-usage stall, deadline. `StressResult` is the single boundary from external process back into the engine.

**Layering rules:**

- `engine/` never imports `gui/` or `tuner/`. `tuner/` drives `engine/`. `gui/` drives both.
- All external binary lookup goes through `config/tools.py`. PATH alone is unreliable under sudo and tarball installs.
- All CPU affinity enforcement goes through `engine/containment.py`. `taskset` or plain affinity is not an acceptable substitute: a payload can widen its own mask, a cgroup cpuset it cannot. `observed_tree_cpus` is an observation watchdog, not enforcement.
- All persistent path resolution goes through `config/paths.py` (`user_home`, `resolve_work_dir`, `atomic_write`, `fix_sudo_ownership`). Ruff bans `Path.home()` and `os.path.expanduser` repo-wide for exactly this reason: under sudo they resolve to `/root` and fork application state into a second database.

## Key Directories

| Path | Purpose |
|---|---|
| `src/corecycler/engine/` | Stress execution: `execution.py` (Lane/Supervisor/ThermalWatch), `scheduler.py`, `parallel.py`, `containment.py`, `detector.py`, `topology.py`, `backends/` |
| `src/corecycler/tuner/` | CO search state machine: `engine.py` (179 KB, `TunerEngine` + Qt workers), `state.py`, `config.py`, `persistence.py` |
| `src/corecycler/smu/` | `driver.py` (`RyzenSMU` mailbox), `commands.py` (generation command sets, CO encoding), `pmtable.py` |
| `src/corecycler/history/` | `db.py` (WAL SQLite, versioned migrations), `context.py`, `logger.py`, `export.py` |
| `src/corecycler/monitor/` | Read-only sensors: `hwmon.py`, `msr.py`, `power.py`, `frequency.py`, `memory.py`, `cpu_usage.py` |
| `src/corecycler/config/` | `paths.py`, `settings.py`, `tools.py` |
| `src/corecycler/gui/` | One module per tab plus `style.py`, `widgets/` |
| `tests/` | 94 `test_*.py` modules, one `conftest.py` |
| `nix/` | NixOS module and out-of-tree kernel module derivations |
| `overlays/` | Temporary hand-authored nixpkgs fixes, one per file |
| `scripts/` | Updater, overlay healing, live scenarios, mutation testing |

## Development Commands

```bash
nix develop                                     # the package's Python env + ruff, nixfmt, pre-commit
ruff check src                                  # lint (no formatter gate)
python -m pytest -m 'not slow'                  # the suite, inside nix develop
nix flake check                                 # build + every check (what CI runs)
corecycler doctor                               # tool resolution preflight
```

Non-Nix distros: `python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]" && pip install PySide6`, then `pytest tests/ -v`. PySide6 is required by the whole suite, not just GUI tests: autouse fixtures import the tuner engine.

Runtime:

```bash
corecycler                       # GUI
corecycler --version             # installed build, e.g. 0.1.0+g2c5c133
corecycler doctor                # every external tool and how it resolved
corecycler status                # persisted tuner sessions
corecycler tune --config F.json  # headless tuning
corecycler resume [SESSION_ID]   # resume; omitted picks an eligible active session
```

There is no `src/corecycler/__main__.py`; `python -m corecycler` is not an entrypoint. `python src/corecycler/main.py` works via the `sys.path` bootstrap in `main.py`.

Flake checks beyond the package build: `full-eval` (eval-gates the unfree `full` variant without realizing mprime), `python-site-packages` (flat `cli.py` collision invariant), `module-eval-nixos` (every `mkIf` path in `nix/module.nix`), `kernel-modules` (builds `ryzen-smu`, `zenpower`, `it87` against both `linuxPackages.kernel` and `linuxPackages_latest.kernel`).

## Code Conventions & Common Patterns

- Python 3.12+, Ruff lint only (`E,F,W,I,UP,B,SIM,TCH,TID251`), 120 columns. No `ruff format` gate in CI, though the generated git hook runs it.
- `from __future__ import annotations` everywhere; built-in generics, `X | None`, `match`, `TYPE_CHECKING` imports to break cycles.
- Type hints on every function signature. State records are `@dataclass(slots=True)`; value objects and topology/backend metadata are `@dataclass(frozen=True, slots=True)`.
- `StrEnum` for domain states so DB and JSON values need no adapters (`TunerPhase`, `StressMode`).
- Comments explain why, never what. Default to zero comments.
- Logging: `logging.getLogger(__name__)` per module. INFO+ to stderr, DEBUG to a rotating `~/.local/share/corecycler/logs/corecycler.log`. Tuner user-facing narrative goes through the `log_message` Qt signal and is persisted as `tuner_events` rows.
- Concurrency is Qt, not asyncio. Long work is a `QThread` (`_TunerWorker`, `_ParallelWorker`, `_RapidTransitionWorker`, `_SoakWorker`, `TestWorker`); stress payloads are `subprocess.Popen`; the APERF/MPERF sampler is a daemon `threading.Thread`. No async/await path exists.
- Seams are constructor parameters, not a DI framework: `TunerEngine` takes db/topology/smu/backend/config/work_dir, `Supervisor` takes a containment function, `cli` takes an optional `engine_factory`.
- Named exceptions are rare and deliberate: `ContainmentUnavailable`, `CoreMapError`. Ordinary environment failures become a classified `StressResult` (`startup`, `computation`, `mce`, `mce_unattributed`, `thermal`, `stall`, `killed`, `crash`, `timeout`, `idle_instability`, `load_transition`, `clock_stretch`, `unknown`) that pauses the tuner instead of advancing its search.
- Display, notification, and optional-sensor failures are caught and logged so they can never change a stability verdict. Hardware-state and containment failures are never swallowed. `tests/test_no_silent_swallow.py` enforces the distinction at the source level.
- State is written before the action it describes: CO journal intent before the SMU write, core/session state before advancing a phase, `in_test` before launching a worker (that row is how a crash gets attributed after reboot).
- Never invent a verdict. Lanes stopped before earning one stay absent; a scheduler kill is pass-like only when no backend error, crash signal, MCE, or external-kill evidence overrides it.
- Adding a backend: new module in `engine/backends/`, subclass `StressBackend` with `get_command`/`parse_output`/`get_supported_modes`, decorate `@register_backend`, add it to `load_all` in `backends/__init__.py`, and add a matching `ExternalTool` to `config/tools.py`. GUI combos derive from the registry, so no GUI edits are needed.
- Commits are Conventional with a scope: `feat(hwmon):`, `fix(smu):`, `test(tuner):`, `docs:`. Scopes in use: `tuner`, `smu`, `gui`, `engine`, `backends`, `history`, `topology`, `tests`, `ci`, `nix`.
- Nix: `nixfmt`, `lib.mkOption` rather than `with lib;`.

## Important Files

- `src/corecycler/main.py` - process bootstrap, logging, `QLockFile` single-instance, sudo session recovery, signal handlers, `sys.excepthook`/`threading.excepthook`.
- `src/corecycler/cli.py` - headless dispatch and exit-code mapping.
- `src/corecycler/tuner/engine.py` - the state machine; `_run_next`, `_on_test_finished`, `_advance_core`, `_run_validation_next`, `resume`.
- `docs/tuner-state-spec.md` - **normative** per-core transition table and crash-attribution priority. `tests/test_state_transition_spec.py` executes it.
- `docs/test-order-spec.md` - **normative** scheduler selection contract for all five orders. `tests/test_test_order_spec.py` executes it.
- `docs/architecture.md` - layer map and containment contract.
- `CONTRIBUTING.md` - the authoritative style and PR checklist; this file summarizes it.
- `pyproject.toml` - Ruff rules, the `TID251` path bans, pytest markers, coverage config.
- `flake.nix` - package variants (`default` FOSS, `full` unfree with mprime/y-cruncher), the pytest gate, all repo checks, generated git hooks.
- `.github/workflows/` - `ci.yml`, `distro-matrix.yml`, `maintenance.yml`, `update.yml`.

**Do not hand-edit:**

- `README.md` `options` block between `<!-- BEGIN generated:options -->` / `<!-- END generated:options -->`: rewritten by `scripts/update-readme-options.sh`. The other `generated:*` markers (`badges`, `upstream`, `installation`, `footer`) must exist for the `check-readme-sections` hook; nothing in this repo regenerates their content.
- `.pre-commit-config.yaml` (0 bytes, gitignored, generated by git-hooks.nix through `flake.nix`).
- `scripts/update.sh`, `scripts/heal-overlays.sh`, `scripts/classify-build-failure.sh`, `.github/workflows/{ci,maintenance,update}.yml`, `.envrc`, `.editorconfig` are canonical copies from `nix-packaging-standard`; the `std-conformance` flake check fails on any byte of drift or a missing file. `update.yml` is a deliberate daily no-op here (`upstream.type: none` is the standard's first-party archetype).

## Runtime/Tooling Preferences

- The Nix devshell is the intended environment; `.envrc` is `use flake` under direnv. Do not pin the Nix build to `python312`: it deliberately uses nixpkgs' default `python3` for PySide6 binary-cache hits while `pyproject.toml` stays `>=3.12`-compatible.
- Version is git-derived (setuptools-scm); there is no per-commit literal to bump. `[tool.setuptools_scm].fallback_version` in `pyproject.toml` is the release line, bumped only when cutting a tag. The Nix sandbox has no `.git`, so `flake.nix` builds `<fallback_version>+g<rev>` and feeds it to both the derivation and `SETUPTOOLS_SCM_PRETEND_VERSION`; a git checkout reports `<next>.devN+g<rev>` instead. `corecycler.__version__` falls back to `0+unknown` in an uninstalled checkout and is stamped on every `tuner_sessions` row.
- Required at runtime: `systemd-run` and `setpriv` (containment refuses to launch without them) plus at least one stress backend. `corecycler doctor` is the preflight and fails on exactly that set.
- Tool resolution order is `CORECYCLER_<TOOL>_BIN` -> `~/.config/corecycler/tool-paths.json` -> `PATH`. An explicit non-executable path is rejected rather than falling back. Discovery only suggests candidates; it never executes an unrecorded binary.
- State locations: settings `~/.config/corecycler/settings.json`, tool paths `~/.config/corecycler/tool-paths.json`, history `~/.local/share/corecycler/history/history.db`, logs `~/.local/share/corecycler/logs/`, lock `~/.local/share/corecycler/corecycler.lock`, work dir `$XDG_RUNTIME_DIR/corecycler/work` when owned by the invoking UID, else `~/.cache/corecycler/work`.
- CO tuning additionally needs a loaded `ryzen_smu`, a writable `/sys/kernel/ryzen_smu_drv/smu_args`, a supported generation, and an unambiguous core map. `MSRReader` needs `/dev/cpu/N/msr` and is read-only; it never writes MSRs.
- Do not remove the mprime prepare/cleanup path. Without `local.txt`/`prime.txt` mprime self-pins its workers, and a stale `results.txt` silently fails the next run.
- `overlays/*.nix` are temporary. Each must export `meta.reason`, `meta.added` (`YYYY-MM-DD`), `overlay`, and exactly one of `dropWhen`/`dropWhenBuilds` testing the real condition, not a version-string proxy. `scripts/heal-overlays.sh` `git rm`s them once the probe passes.

## Testing & QA

pytest (`pythonpath = ["src"]`, `testpaths = ["tests"]`) with Hypothesis. Markers: `slow`, `hardware`, `contract`. Register new markers in `pyproject.toml`.

```bash
python -m pytest -m "not slow" --cov=corecycler --cov-report=term --cov-fail-under=100
python -m pytest -m "not slow" --cov=corecycler --cov-report=term-missing   # find gaps
python -m pytest tests/test_smu_commands.py -v                              # one module
QT_QPA_PLATFORM=offscreen python -m pytest -m 'not slow'                    # headless
CORECYCLER_HW_CONTRACTS=1 python -m pytest -m contract                      # Ring B live
sudo -E env CORECYCLER_HW_CONTRACTS=1 CORECYCLER_HW_PRIVILEGED=1 \
  python -m pytest -m contract                                              # privileged
python3 scripts/mutate.py --src src/corecycler/smu/commands.py \
  --tests tests/test_smu_commands.py --max 60                               # mutation
```

**Coverage floor is 100% line coverage** for the non-slow suite, enforced by the Nix package build (`--cov-fail-under=100`, `branch = false`, `*/main.py` omitted). New code without tests fails the build.

Three rings:

- **Hermetic** (the normal gate): `tmp_path`, in-memory `HistoryDB`, dictionary-backed fake sysfs, fake SMU mailboxes, scripted supervisors, patched `subprocess`. No test may depend on the host CPU, `/proc`, `/sys`, `/dev/cpu`, the kernel journal, installed tools, or real SMU writes.
- **Ring A**: constant pins in `tests/test_contracts.py` driven by `tests/contract_inventory.py`. Runs in the normal gate.
- **Ring B**: `@pytest.mark.contract` (usually plus `slow`), real binaries and hardware. `tests/_contract_hw.py` makes an absent resource skip normally but **fail** under `CORECYCLER_HW_CONTRACTS=1`. A meta-test requires every live-verifiable inventory entry to name its Ring B test, so a new external assumption needs an inventory entry, a Ring A pin, and a Ring B test.

`tests/conftest.py` is the only conftest. Key fixtures: `topo_dual_ccd_x3d`/`topo_single_ccd`/`topo_intel`, `build_topology`, `mock_sysfs`, `mock_backend`, `mock_ryzen_smu_sysfs`, `zen3_commands`/`zen5_commands`, `db`, `exec_tmp_path` (falls back off a `noexec` `/tmp`), `on_path`. Autouse: `tool_search_roots` (strips every `CORECYCLER_*_BIN`), `no_blocking_dialogs` (any modal raises with the name of the helper to patch), `assume_rebooted`, `no_real_forensics`, `assume_clean_shutdown`.

Conventions and gotchas:

- Assert observable behavior and safety outcomes, not implementation. Three source-level guards already exist: `test_no_duplicate_functions.py`, `test_no_silent_swallow.py`, `test_packaging.py`.
- Tuner fault tests patch `QTimer.singleShot` to run synchronously; worker tests call `run()` directly. A new hermetic test must not start a real `QThread` or a real stress binary.
- GUI test modules skip themselves when `PySide6` has no `__path__`, which is how they detect the conftest's minimal Qt fallback. They reuse `QApplication.instance() or QApplication([])`.
- Avoid `sleep` in hermetic tests: patch timing, use events, or use a scripted supervisor. Real sleeps belong only to the short timing tests in `test_scheduler.py`/`test_execution.py` and to Ring B.
- Most Hypothesis properties set `deadline=None` because they drive state machines, Qt, or file-backed doubles. There is no registered Hypothesis profile.
- `scripts/live_scenarios.py` and `scripts/run_live_scenario.sh` drive the real GUI against real hardware with an isolated `HOME`. The wrapper `pkill -9`s broad stress-process patterns and aborts if mprime or y-cruncher is already running. Never point it at a normal `HOME` or a machine doing other work.
