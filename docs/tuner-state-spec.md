# Tuner core state machine - transition specification

This document is the normative transition contract for one tuner core.
`tests/test_state_transition_spec.py` executes the real `_advance_core`,
`_on_test_finished`, `_apply_crash_penalty`, and annealing picker paths against
this contract. `docs/test-order-spec.md` specifies which core is selected.

## Verdict transitions

The destinations below are the complete phase relation after a slot has a
verdict. A comma means the destination depends on bounds, retry counters, or
the configured offset limit.

| Phase | PASS -> | FAIL -> |
|---|---|---|
| `NOT_STARTED` | `COARSE_SEARCH` (entry step; verdict ignored) | `COARSE_SEARCH` (entry step; verdict ignored) |
| `COARSE_SEARCH` | `COARSE_SEARCH`, `SETTLED` | `FINE_SEARCH`, `SETTLED`, `BACKOFF_PRECONFIRM` |
| `FINE_SEARCH` | `FINE_SEARCH`, `SETTLED` | `SETTLED` |
| `SETTLED` | `CONFIRMING` | `CONFIRMING` |
| `CONFIRMING` | `CONFIRMED` | `CONFIRMING`, `FAILED_CONFIRM` |
| `CONFIRMED` | `CONFIRMED` | `CONFIRMED` |
| `FAILED_CONFIRM` | `BACKOFF_PRECONFIRM` | `BACKOFF_PRECONFIRM` |
| `BACKOFF_PRECONFIRM` | `BACKOFF_PRECONFIRM`, `BACKOFF_CONFIRMING` | `BACKOFF_PRECONFIRM`, `BACKOFF_CONFIRMING`; pause if the baseline fails |
| `BACKOFF_CONFIRMING` | `CONFIRMED`, `BACKOFF_PRECONFIRM` | `BACKOFF_PRECONFIRM`, `BACKOFF_CONFIRMING`; pause if the baseline fails |
| `ANNEALING` | `CONFIRMED` and promote the probed offset | `CONFIRMED` and return to the prior best offset |

`CONFIRMED` is the single resting phase. There are no hardening phases or
hardening tiers.

A core created with a seed (`tune --seed-from`) starts in `COARSE_SEARCH` at the
seeded offset rather than in `NOT_STARTED`, so its first slot tests the seed
instead of stepping past it, and its `baseline_offset` stays at the configured
`start_offset`. Once running it obeys the table above unchanged.

## Per-slot regime battery gate

A state-machine PASS means that the current offset passed **every regime**
required for that slot, not merely one workload:

1. `_slot_regimes` uses the validated `coarse_regimes` subset during
   `COARSE_SEARCH`. From `FINE_SEARCH` onward the configured battery must cover
   all four regimes: boost, current, transient, and coupled.
2. Within that set, regimes are ordered by observed failure yield, subject to
   the configured floor that prevents a regime with no failures from being
   starved.
3. `_battery_entry` selects a workload for the regime at `battery_index`.
4. A PASS with regimes remaining increments `battery_index`, schedules the
   next regime, and retests the **same offset in the same phase**. It does not
   call `_advance_core`.
5. A PASS of the final regime resets `battery_index` to zero and delivers one
   PASS to `_advance_core`.
6. A FAIL in any regime resets `battery_index` to zero and immediately
   delivers one FAIL to `_advance_core`; later regimes in that slot are not
   run.

Thus a short failure is conclusive for the slot, while a short pass proves
only one part of the battery. The battery gate also applies to `ANNEALING`.

## Annealing loop

Annealing lets a converged answer improve with accumulated real running time:

1. Clean time is banked by `(context, core, regime, offset)`. The eligibility
   value is the banked time in the **weakest regime**, i.e. the minimum across
   every regime at the core's current `best_offset`.
2. Only when no ordinary core is available may `_pick_next_core` fall through
   to a `CONFIRMED` core whose weakest-regime bank meets its current bar. The
   picker changes it to `ANNEALING`, sets `current_offset` one fine step deeper
   than `best_offset`, and resets `battery_index`.
3. The deeper offset must pass the complete regime battery. A complete PASS
   promotes it to `best_offset`, returns to `CONFIRMED`, clears
   `anneal_strikes`, and resets `anneal_bar_hours` to the configured
   `anneal_bank_hours`.
4. Any regime FAIL returns `current_offset` to the existing `best_offset`,
   returns to `CONFIRMED`, increments `anneal_strikes`, and doubles the current
   bar (using `anneal_bank_hours` as the initial bar).
5. A core at the configured offset limit, or with
   `anneal_strikes >= anneal_max_strikes`, is no longer an annealing candidate.

## Hard-crash transitions

`_apply_crash_penalty` records the crashed value as a hard fail bound, backs
off without crossing stock, invalidates an overly aggressive best value, and
applies crash cooldown bookkeeping. Its phase relation is:

| Phase before crash | Phase after crash penalty |
|---|---|
| `COARSE_SEARCH` | `BACKOFF_PRECONFIRM` |
| `FINE_SEARCH` | `BACKOFF_PRECONFIRM` |
| `CONFIRMING` | `BACKOFF_PRECONFIRM` |
| `CONFIRMED` | `BACKOFF_PRECONFIRM` |
| `BACKOFF_PRECONFIRM` | `BACKOFF_PRECONFIRM` |
| `NOT_STARTED` | `NOT_STARTED` |
| `SETTLED` | `SETTLED` |
| `FAILED_CONFIRM` | `FAILED_CONFIRM` |
| `BACKOFF_CONFIRMING` | `BACKOFF_CONFIRMING` |
| `ANNEALING` | `ANNEALING` |

After every crash penalty, `best_offset` is set, the crashed value remains a
fail bound, and neither `current_offset` nor `best_offset` crosses the safe
stock floor.

## Guards that make the function total

- A PASS at or beyond a recorded fail bound cannot widen the bounds. Failure
  evidence wins, and the search steps back inside the fail bound.
- A persisted backoff state with `best_offset = NULL` is normalized to the
  baseline before backoff arithmetic.
- A failure at or less aggressive than a recorded pass bound invalidates that
  pass bound.
- Reaching stock or an inherited baseline is not proof. A baseline failure in
  confirmation/backoff pauses rather than certifying it.
- Offset limits are clamped, counters cannot become negative, and persistence
  rejects corrupt states at the database boundary.
- A completed verdict is processed before the per-core time budget can pause
  an unfinished search.
- Every non-confirmed outcome is persisted before any signal is emitted or the
  next action begins. Stock restoration must be read back; any core that cannot
  be verified at CO=0 transitions the session to `profile_quarantined`.

## Verdicts that do not enter this relation

- `thermal`: cool down and retry the same slot; heat is not an offset verdict.
- `startup`, `stall`, `killed`, an unknown worker outcome, or another
  apparatus fault: no stability verdict is manufactured. Restore the
  applicable baseline and retry or pause according to the instrument-failure
  breaker. A backend's own wrong-answer report (mprime `FATAL ERROR`,
  y-cruncher `Error(s) encountered`, `Coefficient is too large`, `Checksum
  mismatch`) is a `computation` FAIL, never an apparatus fault.
- A worker result containing an unattributed machine check does not advance a
  loaded core merely because it was loaded; it enters crash attribution.

## Crash attribution priority

Resume binds evidence to the persisted boot and execution checkpoint before
repairing session state. Reopening in the same boot does not apply another
penalty. Attribution uses this strict priority:

1. **An in-flight attribution hunt owns the event.** Its persisted `hunt_state`
   determines which mask was loaded and advances the hunt; ordinary attribution
   must not corrupt that experiment. Every ordinary slot persists its crash
   context (vector, loaded cores, workload) as an unstarted `HuntState` before
   launch; a crash under it starts the hunt from that context, with the
   breadcrumb's failure time.
2. **Kernel MCE evidence naming a mapped, non-stock core** penalizes exactly
   that core at its journaled resident offset. Evidence naming an unknown or
   stock core is an instrument/evidence inconsistency, never permission to
   blame a different core.
3. **The only non-stock resident** is attributable: the only core away from
   stock when exactly one journaled resident offset is non-zero. Being the
   one loaded core is not attribution on its own; that goes to the hunt's
   `LEAD` probe.
4. **Otherwise start or resume the attribution hunt.** An aggregate validation
   failure with no per-core verdict starts a hunt using the exact failed
   workload; it never guesses the most-aggressive core. There is no stock
   control stage. When exactly one loaded core holds a live offset and the
   live set has more than one core, the hunt opens in `LEAD`: that core runs
   alone with every other core at stock. A reproduction makes it the culprit;
   a clean answer moves to `PROBE` over the whole live set, that core
   included. `PROBE` applies the persisted in-flight group from
   `HuntState`; an in-flight group that was never answered is replayed, never
   skipped. A lone core that reproduces with every other core
   at stock is a culprit with no further probe. Culprits do not terminate a
   hunt while known guilty pending sets remain.

`PROBE` treats a set that reproduces while both of its halves ran clean as
a conjunction: every member is found and charged with the crash. For an onset
failure, timed from the breadcrumb to within `onset_failure_seconds` of load
start, each probe is a series of launches. The series answers the probe only
after every launch passes, or at the first reproduction. `launches_done`
persists each clean launch, so a series cut short resumes where it stopped.
An `EXHAUSTED` hunt names nobody, but it counts one unattributed failure for
the suspicion fallback. The core whose stepped-phase
slot (`coarse_search`, `fine_search`, `confirming`, `backoff_preconfirm`,
`backoff_confirming`, `annealing`) was running records a FAIL, so a step
cannot advance through crashes that retries merely outlast. Entering fresh
validation resets the unattributed-failure count.

The persisted hunt workload records the concrete worker kind (`solo`,
`parallel`, `rapid_transition`, or `soak`), duration, backend, stress mode,
FFT preset, threads, profile, and test list. Resume therefore replays the same
experiment instead of reconstructing or substituting a workload. A failure the
breadcrumb timed past `onset_failure_seconds` sets the probe budget itself,
`probe_mttf_multiplier` times that time. An onset or untimed failure keeps a
floor: the larger of the recorded duration and `probe_base_seconds`, so a short
search slot never shortens attribution.

A stability ambiguity never pauses the crash-attribution engine and never
causes a guessed penalty: it becomes another hunt probe. This path pauses only
for an instrument failure, such as unavailable boot forensics, contradictory
stock/unknown evidence, or inability to apply or restore the requested mask.
