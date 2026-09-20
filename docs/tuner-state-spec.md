# Tuner core state machine — transition specification

The per-core state machine's ALLOWED transition relation, declared as data
and exhaustively executed by `tests/test_state_transition_spec.py`: every
(phase x outcome x offset-scenario) combination is driven through the real
`_advance_core` / `_apply_crash_penalty`, and any transition outside this
chart fails the suite. Safety invariants are asserted after every single
transition. `docs/test-order-spec.md` covers which core gets tested next;
this chart covers what happens to a core once it has a verdict.

## Verdict transitions (`_advance_core`)

| Phase | PASS -> | FAIL -> |
|---|---|---|
| NOT_STARTED | COARSE_SEARCH (entry step, verdict ignored) | COARSE_SEARCH |
| COARSE_SEARCH | COARSE_SEARCH, SETTLED (hit max) | FINE_SEARCH, SETTLED |
| FINE_SEARCH | FINE_SEARCH, SETTLED | SETTLED |
| SETTLED | CONFIRMING (baseline if no passing candidate) | same |
| CONFIRMING | CONFIRMED (HARDENING_T1 with tiers) | CONFIRMING (retry), FAILED_CONFIRM |
| FAILED_CONFIRM | BACKOFF_PRECONFIRM (baseline still needs testing) | same |
| BACKOFF_PRECONFIRM | BACKOFF_PRECONFIRM (midpoint probe), BACKOFF_CONFIRMING | BACKOFF_PRECONFIRM, BACKOFF_CONFIRMING (retry pass bound); pause if baseline fails |
| BACKOFF_CONFIRMING | CONFIRMED, BACKOFF_PRECONFIRM (midpoint; HARDENING_T1 with tiers) | BACKOFF_PRECONFIRM, BACKOFF_CONFIRMING; pause if baseline fails |
| HARDENING_T1 | HARDENING_T2, HARDENED | HARDENING_T1 (back off and retest; pause if baseline fails) |
| HARDENING_T2 | HARDENING_T1 (next tier), HARDENED | HARDENING_T2 (back off and retest; pause if baseline fails) |
| CONFIRMED | CONFIRMED (absorbing) | CONFIRMED |
| HARDENED | HARDENED (absorbing) | HARDENED |

## Hard-crash transitions (`_apply_crash_penalty`)

Every search/confirm/terminal phase is forced into BACKOFF_PRECONFIRM (a
crash invalidates any confirmation); NOT_STARTED, SETTLED, FAILED_CONFIRM and
BACKOFF_CONFIRMING keep their phase while the offsets back off. After every
crash penalty: `best_offset` is set (never None), never more aggressive than
the penalized current, the crashed value is a hard fail bound, and the
penalty never overshoots past stock (CO=0).

## Guards that make the function total

- **Contradictory evidence**: a PASS at/beyond the recorded fail bound must
  not widen the bounds (failures outrank passes) — otherwise the backoff
  binary search diverges toward more aggressive values. The pass is dropped
  and the search steps back to just inside the fail bound.
- **Normalization**: a persisted backoff-phase row with `best_offset = NULL`
  (older versions, hand edits) is normalized to the baseline instead of
  crashing the arithmetic.
- **Persistence boundary**: reading or writing a core state with offsets
  outside the sane CO range or negative counters raises — corruption is
  rejected at the boundary, in both directions.
- **Contradicted pass bounds**: a failure at a pass bound, or at a less aggressive
  offset, invalidates that bound. Backoff must earn confirmation again.
- **Baseline is not proof**: reaching stock or an inherited BIOS baseline does
  not confirm or harden it. Baseline failures pause instead of certifying it.
- **Time limit is not proof**: process the completed test's verdict first, then
  pause an unfinished search when its per-core time budget is exceeded.

## Invariants (asserted after every transition in the sweep)

1. Offsets never exceed `max_offset` in the aggressive direction.
2. `backoff_pass_bound` is never more aggressive than `backoff_fail_bound`.
3. Counters never go negative.
4. Every produced state passes the persistence-boundary sanity guard.

## Verdict classes that never enter this state machine

- `thermal` - cool down and retry without treating heat as an offset failure.
  Independently observed MCEs still penalize the named cores, including the loaded core.
- `startup` — environment fault: revert the offset, persist `in_test=0`,
  pause. Never logged as a verdict, never marks the journal survived.
- Apparatus-breaker trips (implausible fail streaks, search flow only) —
  roll back to the most aggressive proven pass, re-enter CONFIRMING, pause.

## Crash attribution on resume (`_attribute_crash_after_reboot`)

Reboot detection uses the persisted session boot ID before any resume-time repairs
or narrative writes. Legacy sessions fall back to execution timestamps; metadata
updates are not execution. Reopening within the same boot does not impose another
crash penalty. Unavailable forensic history pauses before any CO restoration.

Evidence outranks policy; a guess is never written. Priority order:

1. Kernel-journal forensics from the exact persisted session boot, since the last
   execution checkpoint: penalize exactly the cores the kernel's MCE lines name,
   anchored at their journaled resident values. Stock or out-of-scope core evidence
   pauses without penalizing another core. Initrd journal-stop records do not prove
   that a boot ended cleanly.
2. A persisted hunt slot (`tuner_sessions.hunting_core`): the box died while
   one core was stressed alone with every other core at stock — proof by
   isolation.
3. A single in-test core in the SEARCH flow (isolation mode): direct blame.
4. The CO journal's un-survived residents.
5. Anything ambiguous (multi-core in-test set, or any crash under
   validation, including a paused session with a persisted validation cursor):
   penalize NOBODY; run the isolated crash hunt - per-core
   slots at the tuned value with all other cores at stock, most suspect
   first (prior MCE rows, crash history, deepest undervolt). A slot failure
   convicts its core. After `max_unattributed_crash_hunts` fruitless hunts
   in a row the session pauses for the owner.

Cross-core MCE evidence during a live test uses `_apply_crash_penalty` with
`steps=1, count_crash=False` for corrected errors (one-step backoff, re-earn
confirmation, journal kept un-survived) and the full penalty for uncorrected
ones - the same declared transition relation, so the chart above holds.

Hardware-evidence backoff invalidates persisted clean-validation credit before
leaving validation. Resuming preserves that debt even when loading an older
cursor snapshot. Explicit Validate Profile first reconfirms each core and then
runs the configured staged validation; it cannot skip stages just because the
UI already reports validating.

Endurance (`TunerConfig.endurance`) is validation stage 9: a session-level,
perpetual confirmation loop entered instead of completion once a clean staged
pass finishes. It changes no per-core transition - cores stay `HARDENED` and
the chart above holds. Each round runs, per configured workload, one solo slot
per core with every offset live followed by one all-core slot, with slot length
doubling each round up to `endurance_slot_max_seconds`. A solo-slot failure is
`_backoff_core` by one fine step and a retry of the same slot; an all-core-slot
failure backs off the reported lane, re-tests it solo, then reruns the slot.
Crash attribution treats stage 9 exactly like any other validation stage: the
isolated hunt runs and nobody is convicted by guess. `unattributed_crashes`
resets to 0 after every round that completed with no back-off. Endurance hunts
replay the interrupted workload and at least its original duration in isolation.
Hunt passes and non-verdict stops retain the resume-crash streak; a passing
non-hunt test or a convicted hunt backoff clears it.
