# CoreCycler

Per-core CPU stress cycling on Linux, and an autonomous search for the most aggressive stable AMD Curve Optimizer offset of every core. This glossary fixes the words; `docs/` and `AGENTS.md` hold the rules.

## Language

### Silicon

**Core**:
One physical core. Identified by the kernel's `core id`; the unit the search tunes and the cycler stresses.
_Avoid_: CPU (ambiguous with logical CPU), thread

**Logical CPU**:
One hardware thread as the kernel numbers it. A core with SMT has two, called its siblings.
_Avoid_: thread, processor

**CCD**:
A core complex die; the group of cores sharing one L3. A part has one or more.

**V-Cache CCD**:
A CCD carrying stacked 3D V-Cache, recognised by its enlarged L3. A part may have none, one, or every CCD so equipped.
_Avoid_: cache CCD, frequency CCD, the X3D CCD

**Generation**:
The AMD die family a CPU belongs to, decided from CPUID family and model. It selects the SMU command set.
_Avoid_: architecture, codename (a generation may span several)

**Core slot**:
The SMU's physical address for a core inside its CCD, with gaps where cores are fused off. The core map binds cores to slots.

### Curve Optimizer

**Offset**:
A core's Curve Optimizer value. Negative is more aggressive; zero is stock.
_Avoid_: undervolt, CO value, curve

**Stock**:
Offset zero on every core.
_Avoid_: default, baseline (a baseline is a per-core best-known offset, not zero)

**Baseline**:
The offset a core returns to after a slot: its best-known stable offset, or the seed it started from.

**Command set**:
The per-generation mailbox commands, encoding scheme, and offset range used to read and write offsets.

**Journal**:
The durable record of an intended offset write, made before the write and resolved after it.

### Stress

**Backend**:
An external stress program (mprime, y-cruncher, stress-ng, stressapptest) driven as a subprocess.

**Stress mode**:
The instruction set a backend is asked to use (SSE, AVX2, ...).
_Avoid_: mode (alone)

**FFT preset**:
The FFT size class a backend runs (SMALLEST, SMALL, LARGE, ...).

**Test preset**:
A cycler duration profile (QUICK, STANDARD, THOROUGH, FULL_SPECTRUM). Not a stress mode.
_Avoid_: test mode

**Workload**:
One concrete implementation of a regime: a backend, stress mode, FFT preset, thread count, and load profile.

**Load profile**:
How a workload is applied over time: sustained, spectrum, or transient (sub-millisecond duty cycling).

**Containment**:
The kernel cgroup cpuset a payload runs inside, which it cannot widen. The only affinity mechanism.
_Avoid_: pinning, affinity, taskset

**Lane**:
One contained payload on one core's logical CPUs. Parallel runs have several lanes at once.

**Verdict**:
A lane's earned outcome: pass, or a classified failure. A lane stopped before earning one has none; a verdict is never invented.

**Apparatus fault**:
A failure of the harness or platform rather than the core: startup, stall, killed, thermal, unattributed machine check. It never moves the search.
_Avoid_: error, environment failure

### Search

**Session**:
One search over a set of cores, persisted so it survives restarts and reboots.

**Context**:
The operating point a session's evidence belongs to: CPU identity, the full offset vector, PBO limits, scalar, boost override, BIOS. Identified by its context hash.
_Avoid_: profile, environment

**Regime**:
One of four failure conditions a core must survive: boost, current, transient, coupled. Workloads implement regimes; only regimes bank.

**Battery**:
The ordered workloads a slot runs, one or more per regime. Any failure ends the slot; every regime must pass.

**Coarse regimes**:
The regime subset the coarse search runs, chosen because they fail fast.

**Mask**:
Where every other core sits while one is under test: live (peers at their baselines) or isolated (peers at stock).

**Slot**:
One unit of search work: one core (or lane set) at one offset under one mask in one regime.
_Avoid_: test, run, iteration

**Phase**:
A core's position in the search state machine (`docs/tuner-state-spec.md`). `CONFIRMED` is the single resting phase.
_Avoid_: hardened, hardening tier, state

**Bank**:
Clean running time credited to a core, regime, and offset. Only live-mask evidence banks.
_Avoid_: confidence (the derived judgement), score

**Annealing**:
Probing a confirmed core one step deeper once its weakest regime has banked enough time. A failure doubles the bar and counts a strike.

**Guard band**:
The number of steps a reported offset sits behind the passing edge.

**Validation stage**:
One of the seven whole-CPU checks a converged vector must pass before it is reportable.

**Endurance round**:
A repeated whole-CPU slot whose duration doubles each round, up to a cap.

**Soak**:
A watch for kernel errors with no load applied.

### Attribution

**Crash**:
An unplanned reboot or freeze while offsets were resident, attributed after restart from boot identity and kernel forensics.

**Hunt**:
The search that attributes a failure naming no core: group bisection over the live mask down to a core that reproduces alone.
_Avoid_: bisect (the mechanism, not the whole search), isolated hunt

**Crash context**:
The vector, loaded cores and workload an ordinary slot persists as an unstarted hunt before launch. A crash under it starts a hunt that replays exactly that load.

**Probe**:
A hunt slot.

**Culprit**:
The core a hunt convicts.
_Avoid_: guilty core, offender

**Exoneration**:
Lowering a core's suspicion after a probe that included it passed.

**Suspicion**:
The persisted per-core weight the hunt falls back on when deterministic isolation cannot name one culprit.

**Platform fault**:
A failure reproduced at stock, exonerating every offset.

**Quarantine**:
The terminal session state entered when stock cannot be verified restored, an instrument fails repeatedly, or resumes keep crashing. The machine is left at stock and idle.

### Telemetry

**Clock stretch**:
The APERF/MPERF shortfall from nominal while loaded. A warning, never a verdict.

**Microfreeze**:
A scheduling hitch recorded as forensic context. Never a verdict.

**Calibrated**:
PM-table telemetry decoded with a layout verified for that exact table version. Anything else is uncalibrated and unavailable.

### Testing tiers

**Hermetic**:
The default test tier: no host CPU, sysfs, kernel log, network, real home, desktop, or stress binary.

**Ring A**:
Pinned constants that record an external assumption, run in the hermetic tier.

**Ring B**:
Live tests of the same assumptions against real binaries and hardware, opt-in only.
