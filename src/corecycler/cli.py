"""Headless commands - tune, resume and inspect sessions without a display."""

from __future__ import annotations

import argparse
import os
import signal
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from corecycler import __version__
from corecycler.config import tools
from corecycler.config.settings import load_settings

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

    from corecycler.config.tools import Resolution

EXIT_COMPLETED = 0
EXIT_PAUSED = 3
EXIT_QUARANTINED = 4
EXIT_REFUSED = 5
EXIT_ENGINE_ABORTED = 6
EXIT_LOCKED = 7
EXIT_HISTORY = 8
EXIT_SIGNAL = 130
# How often the paused engine is checked for a still-running test, and how
# often the Qt loop yields to Python so a pending signal handler can run.
SETTLE_POLL_MS = 500

# Fields that define what a session searched: a resumed session's saved config
# may be replaced, but never in a way that rewrites the search it already ran.
SEARCH_DEFINING_FIELDS = (
    "start_offset",
    "coarse_step",
    "fine_step",
    "direction",
    "max_offset",
    "cores_to_test",
    "inherit_current",
)


class _ParseRefusal(ValueError):
    pass


class _ParserExit(Exception):
    def __init__(self, status: int) -> None:
        self.status = status


class _CliParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _ParseRefusal(message)

    def exit(self, status: int = 0, message: str | None = None) -> None:
        raise _ParserExit(status)


def _build_parser() -> tuple[_CliParser, argparse._SubParsersAction]:
    parser = _CliParser(prog="corecycler", description="Per-core CPU stability tester and Curve Optimizer tuner")
    parser.add_argument("-V", "--version", action="version", version=f"corecycler {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    commands.add_parser("doctor", help="check required and optional external tools")
    commands.add_parser("status", help="show tuner sessions and evidence")
    report = commands.add_parser("report", help="render a durable tuner report")
    report.add_argument("session_id", nargs="?", type=int)
    report.add_argument("--json", action="store_true", dest="as_json")
    tune = commands.add_parser("tune", help="start a new headless tuning session")
    tune.add_argument("--config", action="append", dest="config_paths")
    tune.add_argument("--seed-from", action="append", dest="seed_sources", type=int)
    resume = commands.add_parser("resume", help="resume a saved tuning session")
    resume.add_argument("session_id", nargs="?", type=int)
    resume.add_argument("--config", action="append", dest="config_paths")
    return parser, commands


_PARSER, _SUBPARSERS = _build_parser()
CLI_COMMANDS = frozenset(_SUBPARSERS.choices)
USAGE = _PARSER.format_help()


def invocation_is_cli(argv: list[str]) -> bool:
    if not argv:
        return False
    return not argv[0].startswith("-") or argv[0] in {"-h", "--help", "-V", "--version"}


def command_requires_confinement(argv: list[str]) -> bool:
    return bool(argv and argv[0] in {"tune", "resume"})


def _refuse(message: str, *, command: str | None = None) -> int:
    prefix = f"corecycler {command}" if command else "corecycler"
    print(f"{prefix}: {message}", file=sys.stderr)
    return EXIT_REFUSED


def cli_main(argv: list[str]) -> int:
    try:
        args = _PARSER.parse_args(argv)
    except _ParserExit as exc:
        return EXIT_COMPLETED if exc.status == 0 else EXIT_REFUSED
    except _ParseRefusal as exc:
        return _refuse(str(exc))
    if args.command is None:
        return _refuse("a command is required")
    if args.command == "doctor":
        return cmd_doctor()
    if args.command == "status":
        return cmd_status()
    if args.command == "report":
        return cmd_report(session_id=args.session_id, as_json=args.as_json)
    config_paths = getattr(args, "config_paths", None) or []
    if len(config_paths) > 1:
        return _refuse("--config may be specified only once", command=args.command)
    config_path = config_paths[0] if config_paths else None
    if args.command == "resume":
        if config_path is not None and args.session_id is None:
            return _refuse("--config requires an explicit SESSION_ID", command="resume")
        return cmd_run(config_path=config_path, resume_id=args.session_id, auto_resume=args.session_id is None)
    seed_sources = args.seed_sources or []
    if len(seed_sources) > 1:
        return _refuse("--seed-from may be specified only once", command="tune")
    seed_from = seed_sources[0] if seed_sources else None
    return cmd_run(config_path=config_path, resume_id=None, auto_resume=False, seed_from=seed_from)


def doctor_lines(resolutions: list[Resolution], unmet: list[str]) -> list[str]:
    """The dependency report, one tool per line, grouped by how much it matters."""
    width = max(len(r.key) for r in resolutions)
    lines = [f"corecycler doctor ({__version__})", ""]
    for kind in (tools.BACKEND, tools.CORE, tools.OPTIONAL):
        lines.append(kind)
        for resolution in [r for r in resolutions if tools.TOOLS[r.key].kind == kind]:
            detail = str(resolution.path) if resolution.path else resolution.problem
            lines.append(f"  {resolution.key:<{width}}  {resolution.origin:<7} {detail}")
            if resolution.path is None:
                lines += [f"    candidate: {c}" for c in tools.discover(resolution.key)]
        lines.append("")
    if os.geteuid() == 0:
        lines += [tools.SUDO_PATH_NOTE, ""]
    lines.append("Pin a path with CORECYCLER_<TOOL>_BIN, or in the GUI when a backend is missing.")
    lines.append("")
    lines += [f"doctor: FAILED -- {problem}" for problem in unmet] or ["doctor: ok"]
    return lines


def cmd_doctor() -> int:
    tools.load_configured_paths()
    resolutions = tools.report()
    unmet = tools.unmet_requirements(resolutions)
    for line in doctor_lines(resolutions, unmet):
        print(line)
    return EXIT_REFUSED if unmet else EXIT_COMPLETED


def _dispatch_report(args: list[str]) -> int:
    return cli_main(["report", *args])


def _history_read(command: str, operation: Callable[[], Any]) -> tuple[bool, Any]:
    try:
        return True, operation()
    except Exception as error:
        detail = str(error) or type(error).__name__
        print(f"corecycler {command}: cannot read tuner history: {detail}", file=sys.stderr)
        return False, None


def cmd_report(session_id: int | None = None, as_json: bool = False, db=None) -> int:
    from corecycler.history.db import HistoryDB
    from corecycler.tuner import report as tuner_report

    own_db = db is None
    error: Exception | None = None
    missing_session = False
    output = ""
    try:
        if db is None:
            db = HistoryDB()
        if session_id is None:
            sessions = db.list_tuner_sessions(limit=1)
            if not sessions:
                output = "null" if as_json else "no tuner sessions"
            else:
                session_id = sessions[0].id
        if session_id is not None:
            if db.get_tuner_session(session_id) is None:
                missing_session = True
            else:
                data = tuner_report.build(db, session_id)
                output = tuner_report.to_json(data) if as_json else "\n".join(tuner_report.render(data))
    except Exception as e:
        error = e

    if own_db and db is not None:
        try:
            db.close()
        except Exception as e:
            if error is None:
                error = e

    if missing_session:
        print(f"corecycler report: no tuner session {session_id}", file=sys.stderr)
        return EXIT_REFUSED
    if error is not None:
        detail = str(error) or type(error).__name__
        print(f"corecycler report: cannot read tuner history: {detail}", file=sys.stderr)
        return EXIT_HISTORY
    print(output)
    return EXIT_COMPLETED


def cmd_status(db=None) -> int:
    from corecycler.history.db import HistoryDB

    own_db = db is None
    error: Exception | None = None
    result = EXIT_COMPLETED
    try:
        if db is None:
            db = HistoryDB()
        result = _render_status(db)
    except Exception as exc:
        error = exc
    if own_db and db is not None:
        try:
            db.close()
        except Exception as exc:
            if error is None:
                error = exc
    if error is not None:
        detail = str(error) or type(error).__name__
        print(f"corecycler status: cannot read tuner history: {detail}", file=sys.stderr)
        return EXIT_HISTORY
    return result


def _render_status(db) -> int:
    from corecycler.tuner import persistence as tp
    from corecycler.tuner.config import TunerConfig

    sessions = db.list_tuner_sessions(limit=50)
    if not sessions:
        print("no tuner sessions")
        return EXIT_COMPLETED
    latest_states = {}
    for index, sess in enumerate(sessions):
        states = db.get_tuner_core_states(sess.id)
        if index == 0:
            latest_states = states
        done = sum(1 for cs in states.values() if cs.phase == "confirmed")
        print(
            f"#{sess.id}  {sess.status:<12} {done}/{len(states)} cores done  "
            f"created {sess.created_at[:19]} by {sess.app_version or 'unknown'}  {sess.cpu_model or ''}"
        )
    latest = sessions[0]
    try:
        config = TunerConfig.from_json(latest.config_json)
    except ValueError:
        print("  (config unreadable; no evidence summary)")
        return EXIT_COMPLETED
    if latest.validation_stage == 9:
        print(
            f"  endurance round {latest.endurance_round}, workload "
            f"{latest.endurance_workload + 1}/{len(config.endurance_workloads)}, "
            f"slot {latest.endurance_index}"
        )
    summary = tp.evidence_summary(db, latest.id, latest_states, config.direction)
    for core_id in sorted(latest_states):
        cs = latest_states[core_id]
        print("  " + tp.format_evidence_line(core_id, cs.best_offset, summary.get(core_id, {})))
    return EXIT_COMPLETED


def _build_smu(topology):
    from corecycler.smu.commands import detect_generation, get_commands
    from corecycler.smu.driver import RyzenSMU, core_map_blocked

    commands = get_commands(detect_generation(topology.family, topology.model, topology.model_name))
    if commands is None or not commands.has_co or not RyzenSMU.is_available():
        return None
    smu = RyzenSMU(commands)
    smu.set_topology(topology)
    map_err = core_map_blocked(smu)
    if map_err is not None:
        print(f"corecycler: per-core CO disabled: {map_err}", file=sys.stderr)
        return None
    return smu


def cmd_run(
    config_path: str | None,
    resume_id: int | None,
    auto_resume: bool,
    *,
    seed_from: int | None = None,
    engine_factory=None,
    db=None,
) -> int:
    from corecycler.history.db import HistoryDB

    own_db = db is None
    if own_db:
        command = "resume" if resume_id is not None or auto_resume else "tune"
        ok, db = _history_read(command, HistoryDB)
        if not ok:
            return EXIT_HISTORY
    try:
        result = _cmd_run(
            config_path, resume_id, auto_resume, seed_from=seed_from, engine_factory=engine_factory, db=db
        )
    except BaseException:
        if own_db:
            try:
                db.close()
            except Exception as error:
                print(f"corecycler: cannot close tuner history: {error}", file=sys.stderr)
        raise
    if own_db:
        try:
            db.close()
        except Exception as error:
            print(f"corecycler: cannot close tuner history: {error}", file=sys.stderr)
            return EXIT_HISTORY
    return result


def _cmd_run(
    config_path: str | None,
    resume_id: int | None,
    auto_resume: bool,
    *,
    seed_from: int | None = None,
    engine_factory=None,
    db=None,
) -> int:
    from PySide6.QtCore import QCoreApplication, QLockFile, QTimer

    from corecycler.config.paths import ensure_state_directory

    app = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])

    instance_lock = QLockFile(str(ensure_state_directory()))
    if not instance_lock.tryLock(0):
        print("corecycler: another instance is already running.", file=sys.stderr)
        return EXIT_LOCKED

    from corecycler.engine.backends import get_backend, load_all
    from corecycler.engine.topology import detect_topology
    from corecycler.tuner.config import TunerConfig
    from corecycler.tuner.engine import TunerEngine

    load_all()
    tools.load_configured_paths()
    command = "resume" if resume_id is not None or auto_resume else "tune"

    session = None
    if resume_id is not None:
        ok, session = _history_read(command, lambda: db.get_tuner_session(resume_id))
        if not ok:
            return EXIT_HISTORY
    elif auto_resume:
        ok, sessions = _history_read(command, db.list_resumable_tuner_sessions)
        if not ok:
            return EXIT_HISTORY
        session = sessions[0] if sessions else None
    if (resume_id is not None or auto_resume) and session is None:
        print("corecycler: no resumable session", file=sys.stderr)
        return EXIT_REFUSED
    if session is not None and session.status == "completed":
        print(f"corecycler: session {session.id} is completed and cannot be resumed", file=sys.stderr)
        return EXIT_REFUSED
    seeds: dict[int, int] | None = None
    if seed_from is not None:
        ok, source = _history_read(command, lambda: db.get_tuner_session(seed_from))
        if not ok:
            return EXIT_HISTORY
        if source is None:
            print(f"corecycler: session {seed_from} not found", file=sys.stderr)
            return EXIT_REFUSED
        if source.status == "quarantined":
            print(
                f"corecycler: session {seed_from} is quarantined and cannot seed a new search",
                file=sys.stderr,
            )
            return EXIT_REFUSED
        unresolved = None
        if source.resume_crash_streak:
            unresolved = "crash recovery"
        elif source.unattributed_crashes:
            unresolved = "unattributed crash evidence"
        elif source.hunt_state:
            unresolved = "crash hunt"
        else:
            ok, suspects = _history_read(command, lambda: db.journal_suspects(seed_from))
            if not ok:
                return EXIT_HISTORY
            if suspects:
                unresolved = "unapplied journal"
        if unresolved is not None:
            print(
                f"corecycler: session {seed_from} has unresolved {unresolved} and cannot seed a new search",
                file=sys.stderr,
            )
            return EXIT_REFUSED
        ok, seeds = _history_read(command, lambda: db.get_tuner_session_offsets(seed_from))
        if not ok:
            return EXIT_HISTORY
        if not seeds:
            print(f"corecycler: session {seed_from} learned no offsets to seed from", file=sys.stderr)
            return EXIT_REFUSED
    override = None
    try:
        if session is not None:
            config = TunerConfig.from_json(session.config_json)
            if config_path is not None:
                override = TunerConfig.from_json(Path(config_path).read_text())
        elif config_path is not None:
            config = TunerConfig.from_json(Path(config_path).read_text())
        else:
            config = TunerConfig()
    except (OSError, ValueError) as e:
        print(f"corecycler: cannot read config: {e}", file=sys.stderr)
        return EXIT_REFUSED

    if override is not None:
        changed = [f for f in SEARCH_DEFINING_FIELDS if getattr(override, f) != getattr(config, f)]
        if changed:
            print(
                f"corecycler: --config would change search-defining field(s) "
                f"{', '.join(changed)} of session {session.id}; start a new "
                f"session with 'tune' instead",
                file=sys.stderr,
            )
            return EXIT_REFUSED
        if session.status == "quarantined":
            print("corecycler: --config cannot be applied to a quarantined session", file=sys.stderr)
            return EXIT_REFUSED

        config = override
    if seeds is not None:
        selected = set(config.cores_to_test) if config.cores_to_test is not None else set(seeds)
        seeds = {
            core_id: offset
            for core_id, offset in seeds.items()
            if core_id in selected
            and (
                (config.direction < 0 and offset < config.start_offset)
                or (config.direction > 0 and offset > config.start_offset)
            )
        }
        if not seeds:
            print(f"corecycler: session {seed_from} has no offsets applicable to this search", file=sys.stderr)
            return EXIT_REFUSED

    if engine_factory is not None:
        engine = engine_factory(db, config)
    else:
        topology = detect_topology()
        if topology is None or not topology.cores:
            print("corecycler: CPU topology detection failed", file=sys.stderr)
            return EXIT_REFUSED
        smu = _build_smu(topology)
        if smu is None:
            print(
                "corecycler: per-core SMU access is unavailable - the tuner "
                "needs it (any refusal reason is printed above; otherwise "
                "check modprobe ryzen_smu and device permissions).",
                file=sys.stderr,
            )
            return EXIT_REFUSED
        validation = config.validate(smu.commands.co_range)
        if validation:
            print("corecycler: invalid tuner config: " + "; ".join(validation), file=sys.stderr)
            return EXIT_REFUSED
        try:
            backend = get_backend(config.backend)
        except KeyError:
            print(f"corecycler: unknown backend {config.backend!r}", file=sys.stderr)
            return EXIT_REFUSED
        if not backend.is_available():
            resolution = backend.resolution()
            print(
                f"corecycler: backend {config.backend!r} {resolution.problem} -- "
                f"install {tools.TOOLS[config.backend].package}, or set "
                f"{tools.env_var(config.backend)}; run 'corecycler doctor' for the full report",
                file=sys.stderr,
            )
            return EXIT_REFUSED
        engine = TunerEngine(db=db, topology=topology, smu=smu, backend=backend, config=config)

    if override is not None:
        db.update_tuner_session_config(session.id, override.to_json())
        print(f"corecycler: session {session.id} config replaced from {config_path}")

    outcome: dict[str, int] = {}

    def finish(code: int, *, force: bool = False) -> None:
        if force:
            outcome["exit"] = code
        else:
            outcome.setdefault("exit", code)
        app.quit()

    engine.log_message.connect(lambda m: print(m, flush=True))
    engine.session_completed.connect(lambda _p: finish(EXIT_COMPLETED, force=True))

    def settle_paused() -> None:
        # pause() takes effect after the in-flight test; quitting sooner would
        # kill the worker mid-test and leave its offset resident and in_test.
        if engine.test_in_flight:
            QTimer.singleShot(SETTLE_POLL_MS, settle_paused)
            return
        finish(EXIT_PAUSED)

    def on_status(status: str) -> None:
        if status == "paused":
            settle_paused()
        elif status == "quarantined":
            finish(EXIT_QUARANTINED)
        elif status == "idle":
            finish(EXIT_ENGINE_ABORTED)

    engine.status_changed.connect(on_status)

    def on_interrupt(signum, _frame) -> None:
        print(f"corecycler: signal {signum} - aborting (offsets revert)", flush=True)
        outcome["exit"] = EXIT_SIGNAL
        engine.abort()
        app.quit()

    def on_terminate(_signum, _frame) -> None:
        print("corecycler: SIGTERM - pausing after the current test", flush=True)
        engine.pause()

    signal.signal(signal.SIGINT, on_interrupt)
    signal.signal(signal.SIGTERM, on_terminate)

    # Python runs signal handlers only between bytecodes; a loop idle in C
    # would otherwise defer them until the worker's next queued emission.
    wake = QTimer()
    wake.timeout.connect(lambda: None)
    wake.start(SETTLE_POLL_MS)

    if session is not None:
        engine.resume(session.id)
    else:
        engine.start(seeds)

    if "exit" in outcome:
        return outcome["exit"]
    if engine.status not in ("running", "validating", "hunting"):
        print(
            f"corecycler: engine did not start (status {engine.status}) - see log",
            file=sys.stderr,
        )
        return EXIT_REFUSED

    app.exec()
    code = outcome.get("exit", EXIT_ENGINE_ABORTED)
    _notify_outcome(code)
    return code


_OUTCOME_NOTES = {
    EXIT_COMPLETED: ("Tuning complete", "The session finished and confirmed a profile.", "normal"),
    EXIT_PAUSED: ("Tuning paused", "The tuner stopped for attention - check the log.", "critical"),
    EXIT_QUARANTINED: ("Tuning quarantined", "The profile is unsafe; cores forced to stock.", "critical"),
    EXIT_ENGINE_ABORTED: ("Tuning aborted", "The engine stopped itself - check the log.", "critical"),
}


def _notify_outcome(code: int) -> None:
    note = _OUTCOME_NOTES.get(code)
    if note is None:
        return
    try:
        if not load_settings().notify_on_completion:
            return
        from corecycler.notify import desktop_notify

        title, body, urgency = note
        desktop_notify(title, body, urgency=urgency)
    except Exception as e:
        print(f"notification failed: {e}", file=sys.stderr)
