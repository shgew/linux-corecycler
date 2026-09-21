"""Headless commands — tune, resume and inspect sessions without a display."""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from corecycler import __version__
from corecycler.config import tools
from corecycler.config.settings import load_settings

if TYPE_CHECKING:
    from corecycler.config.tools import Resolution

EXIT_COMPLETED = 0
EXIT_PAUSED = 3
EXIT_QUARANTINED = 4
EXIT_REFUSED = 5
EXIT_ENGINE_ABORTED = 6
EXIT_LOCKED = 7
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

USAGE = """\
corecycler headless commands:

  corecycler --version            print the installed build and exit
  corecycler doctor               report every external tool and where it resolved
  corecycler status               list tuner sessions; newest one with per-core offsets and live-evidence hours
  corecycler report [SESSION_ID] [--json]
                                  per-core offsets with the banked evidence behind each
  corecycler tune [--config F] [--seed-from SESSION_ID]
                                  start a NEW tuning session and run to the end; --seed-from
                                  begins each core at what that session learned, retested first
  corecycler resume [SESSION_ID [--config F]]
                                  resume a session (newest if omitted); --config replaces its
                                  saved settings (search fields must match)

Exit codes: 0 completed, 3 paused (needs attention), 4 quarantined,
5 refused (bad config/environment), 6 engine aborted, 7 already running,
130 aborted by SIGINT (offsets reverted to baseline).
SIGTERM pauses after the current test and exits 3.

Running the binary with no command opens the GUI.
"""


def cli_main(argv: list[str]) -> int:
    if argv in (["--version"], ["-V"]):
        print(f"corecycler {__version__}")
        return EXIT_COMPLETED
    if argv in (["--help"], ["-h"]) or (
        len(argv) == 2
        and argv[0] in ("doctor", "status", "report", "tune", "resume")
        and argv[1] in ("--help", "-h")
    ):
        print(USAGE)
        return EXIT_COMPLETED
    if not argv:
        print(USAGE, file=sys.stderr)
        return EXIT_REFUSED
    command = argv[0]
    args = argv[1:]
    if command in ("doctor", "status") and args:
        print(f"corecycler {command}: unexpected arguments", file=sys.stderr)
        return EXIT_REFUSED
    if command == "report":
        return _dispatch_report(args)
    if command == "doctor":
        return cmd_doctor()
    if command == "status":
        return cmd_status()
    if command == "tune":
        return _dispatch_tune(args)
    if command == "resume":
        if not args:
            return cmd_run(config_path=None, resume_id=None, auto_resume=True)
        bad_shape = len(args) not in (1, 3) or args[0].startswith("-")
        if not bad_shape and len(args) == 3:
            bad_shape = args[1] != "--config" or args[2].startswith("-")
        if bad_shape:
            print("corecycler resume: expected SESSION_ID [--config FILE] or no arguments", file=sys.stderr)
            return EXIT_REFUSED
        try:
            session_id = int(args[0])
        except ValueError:
            print(f"corecycler resume: invalid session id {args[0]!r}", file=sys.stderr)
            return EXIT_REFUSED
        return cmd_run(
            config_path=args[2] if len(args) == 3 else None,
            resume_id=session_id,
            auto_resume=False,
        )
    print(USAGE, file=sys.stderr)
    return EXIT_REFUSED


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
    """Parse ``report [SESSION_ID] [--json]``; an unreadable shape refuses."""
    as_json = "--json" in args
    rest = [a for a in args if a != "--json"]
    if len(rest) > 1 or (rest and rest[0].startswith("-")):
        print("corecycler report: expected [SESSION_ID] [--json]", file=sys.stderr)
        return EXIT_REFUSED
    session_id = None
    if rest:
        try:
            session_id = int(rest[0])
        except ValueError:
            print(f"corecycler report: invalid session id {rest[0]!r}", file=sys.stderr)
            return EXIT_REFUSED
    return cmd_report(session_id=session_id, as_json=as_json)


_TUNE_SHAPE = "corecycler tune: expected [--config FILE] [--seed-from SESSION_ID]"


def _dispatch_tune(args: list[str]) -> int:
    """Parse ``tune [--config FILE] [--seed-from SESSION_ID]``.

    A repeated flag is a refusal rather than a last-one-wins: an operator who
    named two configs does not know which search is about to run.
    """
    values: dict[str, str] = {}
    rest = list(args)
    while rest:
        flag = rest.pop(0)
        if flag in values or flag not in ("--config", "--seed-from") or not rest or rest[0].startswith("-"):
            print(_TUNE_SHAPE, file=sys.stderr)
            return EXIT_REFUSED
        values[flag] = rest.pop(0)
    seed_from = None
    if "--seed-from" in values:
        try:
            seed_from = int(values["--seed-from"])
        except ValueError:
            print(f"corecycler tune: invalid session id {values['--seed-from']!r}", file=sys.stderr)
            return EXIT_REFUSED
    return cmd_run(
        config_path=values.get("--config"),
        resume_id=None,
        auto_resume=False,
        seed_from=seed_from,
    )


def cmd_report(session_id: int | None = None, as_json: bool = False, db=None) -> int:
    from corecycler.history.db import HistoryDB
    from corecycler.tuner import report as tuner_report

    own_db = db is None
    if db is None:
        db = HistoryDB()
    try:
        if session_id is None:
            sessions = db.list_tuner_sessions(limit=1)
            if not sessions:
                print("no tuner sessions")
                return EXIT_COMPLETED
            session_id = sessions[0].id
        try:
            data = tuner_report.build(db, session_id)
        except ValueError as e:
            print(f"corecycler report: {e}", file=sys.stderr)
            return EXIT_REFUSED
        print(tuner_report.to_json(data) if as_json else "\n".join(tuner_report.render(data)))
        return EXIT_COMPLETED
    finally:
        if own_db:
            db.close()


def cmd_status(db=None) -> int:
    from corecycler.history.db import HistoryDB
    from corecycler.tuner import persistence as tp
    from corecycler.tuner.config import TunerConfig

    own_db = db is None
    if db is None:
        db = HistoryDB()
    try:
        sessions = db.list_tuner_sessions(limit=50)
        if not sessions:
            print("no tuner sessions")
            return EXIT_COMPLETED
        latest_states = {}
        for index, sess in enumerate(sessions):
            states = tp.load_core_states(db, sess.id)
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
    finally:
        if own_db:
            db.close()


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
    from PySide6.QtCore import QCoreApplication, QLockFile, QTimer

    from corecycler.config.paths import user_home

    app = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])

    lock_dir = user_home() / ".local" / "share" / "corecycler"
    lock_dir.mkdir(parents=True, exist_ok=True)
    instance_lock = QLockFile(str(lock_dir / "corecycler.lock"))
    if not instance_lock.tryLock(0):
        print("corecycler: another instance is already running.", file=sys.stderr)
        return EXIT_LOCKED

    from corecycler.engine.backends import get_backend, load_all
    from corecycler.engine.topology import detect_topology
    from corecycler.history.db import HistoryDB
    from corecycler.tuner import persistence as tp
    from corecycler.tuner.config import TunerConfig
    from corecycler.tuner.engine import TunerEngine

    load_all()
    tools.load_configured_paths()
    if db is None:
        db = HistoryDB()
    session = None
    if resume_id is not None:
        session = tp.get_session(db, resume_id)
    elif auto_resume:
        session = tp.pick_auto_resume_session(db)
        if session is None:
            sessions = db.list_resumable_tuner_sessions()
            session = sessions[0] if sessions else None
    if (resume_id is not None or auto_resume) and session is None:
        print("corecycler: no resumable session", file=sys.stderr)
        return EXIT_REFUSED
    seeds: dict[int, int] | None = None
    if seed_from is not None:
        if tp.get_session(db, seed_from) is None:
            print(f"corecycler: no session {seed_from} to seed from", file=sys.stderr)
            return EXIT_REFUSED
        seeds = tp.get_session_offsets(db, seed_from)
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
                "corecycler: per-core SMU access is unavailable — the tuner "
                "needs it (any refusal reason is printed above; otherwise "
                "check modprobe ryzen_smu and device permissions).",
                file=sys.stderr,
            )
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
        tp.update_session_config(db, session.id, override.to_json())
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
        print(f"corecycler: signal {signum} — aborting (offsets revert)", flush=True)
        outcome["exit"] = EXIT_SIGNAL
        engine.abort()
        app.quit()

    def on_terminate(_signum, _frame) -> None:
        print("corecycler: SIGTERM — pausing after the current test", flush=True)
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
            f"corecycler: engine did not start (status {engine.status}) — see log",
            file=sys.stderr,
        )
        return EXIT_REFUSED

    app.exec()
    code = outcome.get("exit", EXIT_ENGINE_ABORTED)
    _notify_outcome(code)
    return code


_OUTCOME_NOTES = {
    EXIT_COMPLETED: ("Tuning complete", "The session finished and confirmed a profile.", "normal"),
    EXIT_PAUSED: ("Tuning paused", "The tuner stopped for attention — check the log.", "critical"),
    EXIT_QUARANTINED: ("Tuning quarantined", "The profile is unsafe; cores forced to stock.", "critical"),
    EXIT_ENGINE_ABORTED: ("Tuning aborted", "The engine stopped itself — check the log.", "critical"),
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
