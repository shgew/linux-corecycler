<!-- markdownlint-disable MD013 -->

# Usage guide

## Quick start

1. Launch CoreCycler (`corecycler` or `python src/corecycler/main.py`). Full monitoring
   (active-clock ratio, per-core power, Curve Optimizer) needs access to the MSR and SMU
   devices: grant it to your user once ([Device access](installation.md#device-access))
   or launch with `sudo`. Without either it runs with reduced telemetry and a status-bar
   warning.
2. The CPU topology (cores, CCDs, SMT, X3D) is detected automatically.
3. In the Configuration tab, select a backend -- use stress-ng if mprime is not
   installed. A backend that is not on PATH (mprime and y-cruncher are extracted
   tarballs, and `sudo` drops your shell's PATH) is not a dead end: CoreCycler offers
   any copy it finds and remembers the one you pick. `corecycler doctor` shows every
   tool and where it resolved from.
4. The default preset is **Standard** (10 min/core, 1 cycle); adjust or pick another.
5. Click **Start Test** and watch the core grid: each core lights up as it is tested,
   turning green (passed) or red (failed). The Results tab shows per-core detail.

### Test mode presets

| Preset | Time/Core | Cycles | Variable Load | Idle Test | Use Case |
|---|---|---|---|---|---|
| Quick | 2 min | 1 | No | No | Fast screening, rough check |
| Standard | 10 min | 1 | No | No | Initial CO tuning |
| Thorough | 30 min | 2 | No | 5s between | Validation after tuning |
| Full Spectrum | 20 min | 3 | Yes | 60s + 10s between | Comprehensive stability proof |
| Custom | User-defined | User-defined | Optional | Optional | Fine-tuned testing |

## Finding optimal CO values (manual)

The manual workflow for tuning AMD PBO Curve Optimizer:

1. **Set conservative starting CO values in BIOS** (e.g. -15 all-core) -- your baseline.
2. **Run a screening pass**: mprime, SSE, Small FFTs, Standard preset
   (~`10 min x core_count`).
3. **Identify failing cores** -- those that cannot sustain -15 at full single-threaded
   boost.
4. **Reduce CO for failing cores** in BIOS (e.g. -15 to -10, or less); leave passing
   cores at -15.
5. **Repeat** until all cores pass.
6. **Extended validation**: switch to Thorough or Full Spectrum -- the variable-load and
   idle tests catch instability sustained load misses.
7. **Test other instruction sets**: SSE produces the highest clocks and is most
   sensitive; AVX2 exercises different execution units and can expose other failures.

The **Auto-Tuner** (below) automates this whole loop via runtime SMU writes, with no
per-step reboots.

## Curve Optimizer tab

Direct SMU access for reading and writing per-core CO offsets at runtime (requires
`ryzen_smu`). Reads all current offsets, per-core spinboxes, per-core and bulk Apply
(each with a confirmation dialog), Reset to 0, Backup/Restore within a session, Dry Run
(logs writes without touching hardware), and Save/Load CO Profile (JSON, shared with the
Auto-Tuner's Export Profile and the History tab's "Load to CO Tab").

All values written here are **volatile** and reset on reboot; set them in BIOS to make
them permanent. For Zen 2 (no Curve Optimizer) the tab shows "Connected (no CO support)"
-- PBO limits and scalar are still accessible.

## Auto-Tuner

The Auto-Tuner automates the entire PBO Curve Optimizer search via runtime SMU writes
(requires `ryzen_smu`). For each core it runs a coarse-to-fine search:

1. **Coarse search** -- from `start_offset` (default 0), step by `coarse_step`
   (default -5) toward `max_offset`, running a short test (`search_duration_seconds`, default
   60s) per step. A pass goes more aggressive; a fail bounds the limit. If the first
   coarse probe fails, fine-step backoff explores the gap toward baseline.
2. **Fine search** -- from the last passing coarse value, step by `fine_step`
   (default -1) toward the failure point to narrow the exact limit.
3. **Confirmation** -- a longer test (`confirm_duration_seconds`, default 300s) validates the
   best offset. On failure, a **smart backoff** finds the nearest stable offset: a quick
   pre-confirm filter (`search_duration_seconds x backoff_preconfirm_multiplier`, default 2x), a
   **midpoint jump** after `midpoint_jump_threshold` (default 3) consecutive pre-confirm
   fails, then **binary search** to the boundary. Backoff stops at the BIOS baseline
   (from `inherit_current`), not zero -- unless the baseline itself crashes, in which
   case it descends toward CO=0 (see [Crash safety](#crash-safety)).
4. **Multi-core validation** (`auto_validate`, on by default) -- after every core is
   individually confirmed, a staged sequence catches failures invisible to per-core
   testing:
   - **Stage 1 -- per-core, all offsets live**: all confirmed offsets applied, each core
     stressed in turn. Catches shared-VRM power-delivery interactions.
   - **Stage 2 -- all-core simultaneous**: one pinned stress process per core at once
     for `validate_duration_seconds` -- full package-power worst case, per-core verdicts.
   - **Stage 3 -- alternating half-core load**: half loaded, half idle, then swap
     (split by CCD when available). Catches boost-ramp voltage transients.
   - **Stage 4 -- rapid transitions** (`validate_transitions`): fast load/idle cycling
     across all cores -- catches idle-to-boost instability.
   - **Stage 5 -- per-core spectrum** (`validate_spectrum`): bursts, load transitions
     and idle watch per core with all offsets live, independently of stage 4.
   - **Stage 6 -- all-core memory load** (`validate_memory`): one memory stressor
     (stressapptest) per core at once, all offsets live -- catches CO marginality
     that only appears under memory-controller load. Skipped with a log if no memory
     stress tool is installed.
   - **Stage 7 -- real-world soak** (`validate_soak`): no synthetic load; the kernel
     error stream is watched for `soak_duration_seconds` while the machine is used
     normally. Any hardware event fails it.

   A failing core is backed off by one `fine_step`, re-proven solo, and only the failed
   stage reruns. A stall, external kill, or unattributable machine check is an
   APPARATUS fault: the step retries without a verdict (up to `max_apparatus_retries`),
   never a back-off. Any back-off marks the pass dirty -- completion requires one final
   clean pass. If a failing core reaches its baseline with real failures persisting,
   the tuner reverts everything to baseline and PAUSES with an honest message instead
   of declaring completion. A reboot mid-validation that no evidence attributes does
   not pause and does not guess: it opens the [attribution hunt](#flows).
5. **Endurance** (`endurance`, on by default) -- instead of declaring a result,
   validation restarts in rounds whose slots double in length. The vector keeps being
   re-proven, and a core that banks enough clean time earns an annealing probe a step
   deeper. Turn endurance off to get a terminal `completed` session instead.
6. **Result** -- `corecycler report` prints each core's offset with the evidence behind
   it. Export it as JSON or load it into the Curve Optimizer tab.

A confirmed offset has passed the configured workloads and durations, not every
possible workload indefinitely. The search targets the most aggressive passing
candidate within its bounds; intermittent failures limit how precisely that
boundary can be established.

The active-clock warning compares APERF/MPERF against nominal frequency. It does
not prove clock stretching or CO instability and never changes a stress verdict.
The saved `stretch_threshold_pct` field controls this warning for existing sessions.
A contradictory-failure circuit breaker pauses with the new failure bound intact;
an earlier passing run does not make a later failure invalid.

### Offset masks

Which cores hold a live offset during a slot is a deliberate variable, not an
implementation detail, because it decides what a crash can prove.

- **Live mask** -- the default for per-core search, validation, and endurance. Every
  core other than the one under test sits at its own best-known offset, which is the
  only condition the machine actually operates in. An idle core at a deep offset can
  take the box down, and a search that parks everyone at stock is structurally unable
  to see that.
- **Isolated mask** -- one core at its offset, every other core at stock. Only the
  attribution hunt uses it, as the last step of bisection. Its result is a
  hypothesis about a core's limit, never proof of one, so nothing banks confidence
  from an isolated run.

Because the live mask is the norm, being the only core under load proves nothing
about who crashed; attribution is a separate experiment. On a stage failure or a
partial SMU write, all cores revert to baseline before pausing.

### Crash safety

Hard crashes can occur during CO tuning. Recovery backs off attributable failures,
hunts ambiguous ones by varying the offset mask, and stops when evidence or hardware
control is unavailable. It cannot guarantee a reboot-free machine. Three mechanisms
bound recovery:

1. **CO write-ahead journal** -- every CO value is recorded in SQLite *before* it is
   written to the SMU, and marked "survived" only after a test completes with it
   resident. So on the next launch the exact `(core, value)` live at crash time is known
   (whether or not a test was running, and including a crash during idle, baseline
   restore, post-test revert, or multi-core validation). Every un-survived offset is
   treated as a hard crash: a fail bound is set there and the core backs off past it.
2. **CO=0 is the recovery floor** -- crash backoff never goes past stock. If an
   inherited baseline crashes, it moves toward 0 rather than being reapplied
   unchanged. Stock is not proof of stability: hardware errors at stock pause.
3. **Repeated crash-resumes ask a different question** -- consecutive crash-resumes
   without a passing non-hunt test are counted. Hunt passes, thermal stops, apparatus
   faults, and plain app restarts do not clear the counter. Finding and backing off a
   hunt culprit does. After `resume_crash_quarantine_threshold` (default 3) the tuner
   stops trusting those per-crash verdicts and starts the attribution hunt over the
   live offsets. `profile_quarantined` is reserved for the one case it always meant
   literally: stock restoration itself failed, so offsets may still be resident.

An interrupted session is detected on next launch and offered for resume. Resume
rebuilds journal evidence from that session only and verifies baseline restoration
through the SMU; it does not assume a reboot left stock values resident. A crash or
pause during validation retains the persisted cursor and pending validation work.

Reboot detection compares the session's persisted Linux boot ID with the current
boot, not configuration-save times or wall-clock changes. Older histories gain
that ID from their last narrative event; sessions without one retain the timestamp
fallback. Kernel evidence and orderly-shutdown checks refer to that exact boot,
not simply the immediately previous boot. Unreadable or unidentified forensic
history pauses before offsets are reapplied. Hardware errors at stock or on an
unmapped/unselected core also pause without blaming another core.

An unattributed crash never pauses and never guesses. Search runs the **live offset
mask** -- every core other than the one under test sits at its own best-known offset,
the only condition the machine actually operates in -- so being the sole core under
load proves nothing about who crashed on its own. What does prove it is the journal:
when the loaded core was on a search, confirmation, backoff, or annealing step and
every other live offset had already survived, that step is the only change from a
vector that survived, and the crash fails it directly. A crash that no kernel machine
check, no un-survived CO journal write, and no such lone trial names starts the
attribution hunt, replaying the workload and offset vector the slot persisted before
it launched. A crash with no core holding a live offset is a platform fault outright
and needs no hunt.

1. **Group bisection over the live mask** -- half the cores keep their offsets, half
   drop to stock. A crash means the culprit is in the live half; log2(n) probes.
   Both halves failing means two culprits, and both subtrees are pursued. A set that
   fails while both of its halves run clean fails only as a whole, so every member
   backs off one step. There is no all-stock control probe first: it cost the longest
   probe of every hunt to rule out the least likely cause, and hardware errors
   reported by a core at stock already pause the hunt.
2. **Lone reproduction** -- a single core that reproduces the failure with every
   other core at stock is the culprit. That reproduction is the answer the hunt was
   asking for, so nothing re-runs the other cores without it.
3. **Suspicion fallback** -- when nothing reproduces inside budget, every core that
   held a live offset accrues suspicion weighted by offset depth and by whether it
   was loaded or idle. It only acts on a two-to-one separation after at least three
   unattributed failures; a near-tie refuses to act, which is exactly where a guess
   would be worst.

Probe budgets are `max(probe_base_seconds, probe_mttf_multiplier x observed
time-to-failure)`, grown per bisection level.
A replayed slot longer than `probe_base_seconds` (a soak) raises that base to its own
length; a shorter one never lowers it.
The observed time comes from the micro-freeze breadcrumb, which records when its slot
started. A failure within `onset_failure_seconds` of load starting is an onset
failure: load starts reproduce it and wall time does not, so the probe budget is
spent as launches of `max(onset_launch_seconds, probe_mttf_multiplier x observed
time)` each. The probe is answered after its last clean launch or at its first
failure. A series cut short by a pause, a shutdown, a thermal stop, or an apparatus
fault resumes the same probe from its last clean launch.

Every solo slot idles `co_settle_seconds` between its CO write and its load step,
watched for machine checks. The breadcrumb names the settle, so a freeze reads as
following either the write or the load.

A hunt that convicts nobody is recorded as such; it is never read as proof that the
live profile is stable. It counts as one unattributed failure for the suspicion
fallback, and the search step that was running when the machine died records a
fail, so a step never advances on retries alone.

### State machine

```text
NOT_STARTED -> COARSE_SEARCH -> FINE_SEARCH -> SETTLED -> CONFIRMING -> CONFIRMED
                                    |                       |            |
                                    v                       v            v (all cores)
                                 SETTLED              FAILED_CONFIRM   VALIDATION
                                                           |     S1 -> S2 -> S3 -> S4 -> S5 -> S6 -> S7
                                                           v      (fail: back off failing core,
                                                    BACKOFF (smart)    solo re-prove, rerun stage;
                                                    - pre-confirm      dirty pass => one final
                                                    - midpoint jump    clean pass before DONE)
                                                    - binary search
                                                    - baseline floor

CONFIRMED -> ANNEALING -> CONFIRMED        (deeper on a pass; back, with a doubled
                                            bar and a strike, on a fail)
```

`CONFIRMED` is the only phase a core rests in. Each offset must survive the whole
**regime battery** before it counts: one slot per regime (`boost`, `current`,
`transient`, `coupled`), a pass with regimes remaining re-testing the *same* offset
under the next one, and any failure ending the slot immediately -- a short fail is
conclusive, a short pass is not. Slot time is split across regimes by how often each
has actually caught something on this silicon, with a floor (`regime_floor_pct`) so a
quiet regime is never scheduled away: its silence is the thing being proven.

Clean time banks per `(operating point, core, regime, offset)`, so confidence survives
reboots and is invalidated the moment the operating point changes. Once a core has
banked `anneal_bank_hours` in its **weakest** regime, it earns one probe a step deeper.
A pass makes that the new answer; a fail returns it to the proven offset, doubles the
bar, and counts a strike, stopping after `anneal_max_strikes`. With endurance enabled
there is no terminus: the vector keeps being re-proven and occasionally improved for
as long as the machine is left running, which is why an overnight run and a week-long
run differ in confidence rather than in kind.

`corecycler report [SESSION_ID] [--json]` prints the per-core answer with the banked
hours, failure classes, and regime yield behind it.

#### Phase reference

Every core carries exactly one phase. `docs/tuner-state-spec.md` is the normative
transition contract; this table is what each phase means in practice.

| Phase | What the core is doing | Entered from | Leaves to |
|---|---|---|---|
| `not_started` | No slot has run yet. The entry step applies `start_offset` and the verdict is ignored. | session start | `coarse_search` |
| `coarse_search` | Stepping by `coarse_step` toward `max_offset`, `search_duration_seconds` per regime, restricted to the regimes named in `coarse_regimes`. | `not_started` | deeper `coarse_search`, `settled` at the limit, `fine_search` or `backoff_preconfirm` on a fail |
| `fine_search` | Narrowing by `fine_step` between the last pass and the first fail, including the gap below a failed first coarse probe. | `coarse_search` | `fine_search`, `settled` |
| `settled` | A candidate is chosen and waiting for the long confirmation run. | `coarse_search`, `fine_search` | `confirming` |
| `confirming` | `confirm_duration_seconds` at the candidate, full battery. | `settled` | `confirmed` on a pass; retry up to `max_confirm_retries`, then `failed_confirm` |
| `failed_confirm` | The candidate did not hold. No offset is trusted until backoff finds one. | `confirming` | `backoff_preconfirm` |
| `backoff_preconfirm` | Smart backoff: a short filter run (`search_duration_seconds x backoff_preconfirm_multiplier`), a midpoint jump after `midpoint_jump_threshold` consecutive filter fails, then binary search between the known bounds. | `failed_confirm`, `backoff_confirming`, any crash penalty | `backoff_confirming` once a value survives the filter; pauses if the baseline itself fails |
| `backoff_confirming` | Full-length confirmation of a backoff candidate. | `backoff_preconfirm` | `confirmed` on a pass, back to `backoff_preconfirm` on a fail |
| `confirmed` | The core's answer, and the only phase a core rests in. | `confirming`, `backoff_confirming`, `annealing` | `annealing` when it has earned a probe; `backoff_preconfirm` on a crash penalty |
| `annealing` | One probe a fine step deeper than `best_offset`, paid for with banked clean time. | `confirmed` | `confirmed` either way: promoted on a pass, restored plus a strike and a doubled bar on a fail |

### Flows

#### Entry points

`corecycler` with no command opens the GUI; the Auto-Tuner tab drives the same engine
the CLI does. `corecycler tune [--config F] [--seed-from SESSION_ID]` starts a new
session headless and runs to the end. `corecycler resume [SESSION_ID]` continues one
(newest eligible if omitted). `corecycler status` lists sessions, `corecycler report`
prints the answer and its evidence, and `corecycler doctor` is the preflight for
external tools. A `QLockFile` allows one instance, so GUI and CLI cannot fight over
the SMU.

#### Seeding a new session from an old one

`--seed-from SESSION_ID` starts each core at the `best_offset` that session reached
instead of at `start_offset`. Use it when the search rules changed underneath a
result: the numbers are worth keeping as a starting point, the evidence behind them
is not.

A seed is a hypothesis, not a result, so it is treated as one:

- The seeded core enters `coarse_search` **at the seeded value**, so the first slot
  retests it under the live mask and the full regime battery. Nothing is inherited
  as proven.
- Its `baseline_offset` stays at `start_offset`. The seed is not a floor, so a core
  whose seed fails can back off the whole way to stock instead of pausing on a
  baseline failure at a number it was merely handed.
- A seed that is not more aggressive than `start_offset` is dropped, a seed past
  `max_offset` is clamped to it, and a seed for a core outside `cores_to_test` is
  ignored.
- No banked confidence comes across. Banks are keyed by `(context, core, regime,
  offset)` and the new session re-earns every hour it claims.

#### Session statuses

The status is the session's flow, and the CLI exit code follows it.

| Status | Flow | Exit code |
|---|---|---|
| `running` | Per-core search, backoff, or an annealing probe | -- |
| `validating` | Multi-core validation stages, or an endurance round | -- |
| `hunting` | Attribution hunt: who crashed the machine | -- |
| `paused` | Stopped on an instrument failure and waiting for you | 3 |
| `profile_quarantined` | Stock restoration itself failed; offsets may still be resident | 4 |
| `platform_fault` | The machine died with no core holding a live offset | 9 |
| `aborted` | Deliberate stop; baselines restored, progress resumable | 6, or 130 from SIGINT |
| `completed` | Every core confirmed and validation clean, endurance off | 0 |
| `idle` | No session in flight | -- |

#### The five things a session can be doing

1. **Per-core search** (`running`) -- one core per slot walks the phases above under
   the live offset mask. Core selection follows `test_order`
   ([Test orderings](#test-orderings)), and `docs/test-order-spec.md` is normative.
2. **Multi-core validation** (`validating`) -- stages 1-7, run once every core is
   `confirmed`. A stage failure backs off the failing core, re-proves it solo, and
   reruns only that stage; any back-off marks the pass dirty and owes one final clean
   pass.
3. **Endurance** (`validating`, `endurance=true`) -- instead of completing, validation
   restarts in rounds whose slots double from `endurance_slot_seconds` up to
   `endurance_slot_max_seconds`. A failing slot backs its core off one fine step and
   the round restarts. This is the state an unattended machine lives in.
4. **Annealing** (`running`) -- entered only when no ordinary core needs a slot, so it
   never delays the search. One core, one step deeper, one battery.
5. **Attribution hunt** (`hunting`) -- opened by an unattributed crash or by reaching
   `resume_crash_quarantine_threshold` crash-resumes. It replays the load that was
   running and varies only the offset mask.

#### Attribution hunt stages

| Stage | Question | Outcome |
|---|---|---|
| `probe` | Which half of the live mask carries the culprit? | Recurses into the failing half, or into both halves when both fail. A lone core that reproduces is a culprit. A set that fails while both halves ran clean backs off every member |
| `culprit` | -- | The core is backed off one step and its banked confidence is discarded |
| `exhausted` | Nothing reproduced inside budget | Counts one unattributed failure for the suspicion model, which acts only on a 2:1 separation after at least three; the search step that was running records a fail |

A probe that is interrupted by a thermal stop, an apparatus fault, a pause, or a
deliberate abort stays in flight rather than counting as an answer, with its clean
launches kept, so a clean stop costs no attribution progress.

#### Interrupting a run

| Action | Effect |
|---|---|
| **Pause** (GUI) or `SIGTERM` | Finishes the current test, saves the cursor, exits 3. Resumable |
| **Abort** (GUI) or `SIGINT` / Ctrl+C | Stops the workload, reverts every core to its session baseline, marks the session `aborted`, exits 130. Resumable |
| Crash, freeze, or reboot | The write-ahead journal and the persisted boot ID reconstruct what was resident; recovery runs on next launch |
| `kill -9`, closing the terminal | Outside the safe model: no restoration runs, though a reboot clears CO anyway |

On the next launch an interrupted session is detected and offered for resume. Resume
attributes any crash from evidence first, restores baselines, verifies them through
the SMU, and only then continues from the persisted cursor.

### Configuration options

| Parameter | Default | Range | Description |
|---|---|---|---|
| Start Offset | 0 | -60 to +30 | Starting CO value for all cores |
| Coarse Step | 5 | 1-15 | Step size during coarse search |
| Fine Step | 1 | 1-5 | Step size during fine search |
| Max Offset | -50 | -60 to +60 | Most aggressive offset (auto-clamped to CPU generation) |
| Search Duration | 60s | 10-600s | Test duration per search step |
| Confirm Duration | 300s | 30-1800s | Test duration for the confirmation run |
| Validate Duration | 300s | 30-3600s | Test duration per multi-core validation stage |
| Max Confirm Retries | 2 | 0-5 | Retries before backing off from a value |
| Auto Validate | true | true/false | Run staged multi-core validation (stages 1-7) after all cores confirm |
| Backend | mprime | mprime/stress-ng/y-cruncher | Per-core stress backend (stressapptest is Memory tab only) |
| Mode | SSE | SSE/AVX/AVX2/AVX512 | Stress instruction set |
| FFT Preset | SMALL | SMALLEST/SMALL/LARGE/HUGE/ALL/MODERATE/HEAVY/HEAVY_SHORT | FFT size preset (mprime) |
| Test Order | sequential | see below | Core testing order |
| Stretch Threshold | 3.0% | 0-20% | Clock-stretch failure threshold (0 = off, requires root) |
| Abort on Consecutive Failures | 0 | >= 0 | Abort if N cores fail at start_offset (0 = off) |
| Resume Crash Quarantine Threshold | 3 | 1-20 | Crash-resumes (no surviving test between) before the attribution hunt replaces the per-crash verdicts |
| Allow Missing Thermal Sensor | false | true/false | Permit running with no readable temperature sensor (false = fail closed) |
| Inherit Current CO | false | true/false | Read current SMU offsets as starting points |
| Regime Floor Pct | 15.0 | 0-100 | Smallest share of slot time any regime may be scheduled down to |
| Anneal Bank Hours | 6.0 | > 0 | Clean hours in the weakest regime before a core probes a step deeper |
| Anneal Max Strikes | 3 | 1-10 | Failed deeper probes before a core stops probing |
| Control Run Confirmations | 2 | >= 1 | Stock reproductions required to call a platform fault |
| Probe Base Seconds | 1800 | 60-86400 | Floor on an attribution probe's budget |
| Probe MTTF Multiplier | 4.0 | 0.01-100 | Multiple of the observed time-to-failure a probe (or one onset launch) must outlast |
| Onset Failure Seconds | 60 | 0-3600 | A failure this soon after load start is probed with repeated launches (0 = off) |
| Onset Launch Seconds | 30 | 10-3600 | Minimum length of one onset-probe launch |
| CO Settle Seconds | 5 | 0-60 | Watched idle between a solo slot's CO write and its load step |
| Suspicion Separation | 2.0 | >= 1 | Score ratio the top suspect needs before the fallback acts |
| Suspicion Min Failures | 3 | >= 1 | Unattributed failures required before the fallback may act |

Abort on Consecutive Failures, Resume Crash Quarantine Threshold, Allow Missing
Thermal Sensor, the battery, and every annealing/hunt knob are not in the panel: set
them in the JSON that `corecycler tune --config` loads. The Backoff Pre-Confirm
Multiplier (2.0) and Midpoint Jump Threshold (3) use sensible defaults and are not
exposed at all.

### How each backend uses Mode and FFT Preset

**Mode** (SSE/AVX/AVX2/AVX512) and **FFT Preset** mean different things per backend:

- **mprime** -- Mode selects the torture-test instruction set and FFT Preset sets the FFT
  size range. `SSE` with Small FFTs is the most sensitive, highest-boost combination.
- **stress-ng** -- Mode selects the CPU stressor method; FFT Preset is ignored.
- **y-cruncher** -- y-cruncher has no instruction-set switch: the instruction set is fixed
  by the per-microarchitecture binary it auto-selects for your CPU. Mode instead chooses
  which of y-cruncher's component tests run (FFT Preset is ignored):
  - `SSE` -- `BKT` only (scalar-integer Basecase/Karatsuba): the lowest-power,
    highest-boost, most CO-sensitive test.
  - `AVX` -- `BKT, BBP, SFTv4, SNT, SVT` (adds the AVX compute and small in-cache tests).
  - `AVX2` / `AVX512` -- all algorithms, including the memory-bandwidth transforms
    (`FFTv4, N63, VT3`). Because y-cruncher auto-selects a supported binary, `AVX512` mode
    runs the strongest instruction set your CPU actually has and never crashes on a
    non-AVX512 chip.

### Test orderings

| Order | Strategy | Best for |
|---|---|---|
| sequential | Finish each core completely before the next | Simple, easy to follow |
| round_robin | One test per core per round | Partial results for all cores sooner |
| weakest_first | Prioritize cores closest to confirmation | Finish nearly-done cores first |
| ccd_alternating | Alternate CCDs, prioritize the CCD with fewest confirmed | Balanced thermal coverage |
| ccd_round_robin | Round-robin within each CCD, alternating CCDs | Best thermal profile -- each core cools while the other CCD is tested |

### Tips

- **mprime, Small FFTs, SSE** is the gold standard for CO testing -- highest single-core
  clocks and the most sensitive error detection (rounding checks, SUMOUT verification).
- The default `max_offset` of -50 suits Zen 4 and is Zen 5's firmware limit (a
  deeper request is clamped); Zen 3/3D are clamped to -30 automatically.
- A typical 16-core run takes ~2-4 hours plus several hours of staged validation (stages 1-7 include an all-core memory-load stage and a 30-minute real-world soak).
- If many cores fail at the starting offset, enable **abort on consecutive failures**
  (e.g. 3) -- it usually means BIOS PBO needs adjusting first.
- **Multi-core validation** is the key differentiator from manual testing: a core stable
  in isolation may fail when all cores draw power at once. If validation keeps backing
  off and restarting, the VRM may not support the aggregate profile -- reduce
  `max_offset` or test fewer cores.

During an active tuner session the config snapshot is taken at start, so mid-run UI
changes have no effect; the Curve Optimizer tab and manual Start Test are locked to
prevent SMU conflicts. Every session is saved in SQLite (config, per-core progress, and
each test result); starting a new session never deletes old ones.

## Recommended settings

| Scenario | Backend | Mode | FFT Preset | Preset |
|---|---|---|---|---|
| Quick screening | stress-ng | SSE | -- | Quick |
| Initial CO tuning | mprime | SSE | Small | Standard |
| Thorough validation | mprime | SSE | Huge | Thorough |
| Comprehensive stability | mprime | SSE | All | Full Spectrum |
| AVX2 validation | mprime | AVX2 | Heavy | Standard |

## Understanding results

Core grid colors -- **blue**: testing now; **green**: passed/confirmed; **red**: failed;
**amber**: tuner smart-backoff; **gray**: pending; **purple**: memory stress (all cores
at once).

| Error type | Meaning | Typical cause |
|---|---|---|
| MCE (Machine Check Exception) | Hardware-level CPU error (sysfs or dmesg) | CO too aggressive -- voltage too low for the requested frequency |
| Computation | Stress test got a wrong result (rounding, sumout, mismatch) | CO too aggressive -- subtle numerical instability |
| Idle instability | MCE during an idle/C-state transition | CO unstable during voltage ramp-up from deep sleep |
| Load transition | Error during a variable-load stop/start | Voltage regulation insufficient during rapid load changes |
| Timeout | Stress process stopped responding | Possible instability hang (sometimes benign) |
| Crash | Stress process terminated unexpectedly | Core instability causing instruction faults |

**MCE errors are the most serious** -- the CPU detected an actual hardware-level error;
reduce that core's CO (make it less negative). **Computation errors** are the most common
CO-tuning failure. **Idle instability** is caught by the idle test phase and is a failure
mode sustained-load tests miss entirely.
