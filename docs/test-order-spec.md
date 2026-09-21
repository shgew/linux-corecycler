# Test-order specification

The auto-tuner selects ordinary search work with one of five
`TunerConfig.test_order` policies. `tests/test_test_order_spec.py` executes each
row and invariant below against the real selectors.

## Selector state

The selectors read persisted `CoreState.phase`, `crash_cooldown`,
`crash_count`, `best_offset`, `anneal_strikes`, and `anneal_bar_hours` plus the
persisted per-regime clean-time banks. `_last_tested_core` and
`_ccd_last_tested` are in-memory cursors rebuilt from real test-log rows after
a restart.
For the five ordinary selectors, a core is **available** exactly when
`phase is not CONFIRMED` and `crash_cooldown == 0`. This includes an
`ANNEALING` core whose battery has not finished.

## Ordinary orders

| Order | Next ordinary pick | Cursor state | After interruption |
|---|---|---|---|
| `sequential` | lowest-id available core; stays on it until it becomes `CONFIRMED` or enters cooldown | none | derived from persisted phases and cooldowns |
| `round_robin` | next available core after the last tested core, cyclic ascending; first available when no cursor exists | `_last_tested_core` | cursor is the core in the last real test-log row |
| `weakest_first` | minimum `phase_score + 2 * crash_count`; ties use lowest core id | none | derived from persisted phases and crash counts |
| `ccd_alternating` | prefer a CCD different from the last tested one; among candidate CCDs choose fewest `CONFIRMED`, then lowest CCD; choose the lowest core id within it | `_last_tested_core` | cursor rebuilt from the test log |
| `ccd_round_robin` | alternate CCD from the last tested one and, within it, rotate after that CCD's last tested core; fewer than two CCDs degrades to `round_robin` | `_last_tested_core` and `_ccd_last_tested` | both cursors rebuilt from the test log |

### `weakest_first` scores

Lower scores run sooner. `CONFIRMED` is unavailable and therefore has no
score.

| Phase | Score |
|---|---:|
| `FINE_SEARCH` | 0 |
| `FAILED_CONFIRM` | 0 |
| `BACKOFF_PRECONFIRM` | 0 |
| `BACKOFF_CONFIRMING` | 1 |
| `CONFIRMING` | 1 |
| `COARSE_SEARCH` | 2 |
| `SETTLED` | 3 |
| `NOT_STARTED` | 4 |
| `ANNEALING` | 5 |

## Annealing fall-through

The ordinary selector always runs first. Only if it returns no core does
`_pick_next_core` ask for an annealing candidate. A candidate must:

- be `CONFIRMED` with a non-null `best_offset`;
- have a one-fine-step-deeper offset inside `max_offset`;
- have `anneal_strikes < anneal_max_strikes`; and
- have banked at least its current annealing bar in **every** regime at its
  best offset (equivalently, the weakest-regime bank meets the bar).

Selecting it immediately changes the core to `ANNEALING`, sets
`current_offset = best_offset + direction * fine_step`, and resets
`battery_index = 0`. If no ordinary or eligible annealing core exists, the
picker returns `None`.

## Invariants for every order

1. An ordinary selector never picks `CONFIRMED` and never picks a core with
   `crash_cooldown > 0`.
2. An ordinary core takes precedence over every annealing fall-through
   candidate.
3. Picking a core decrements every **other** core's cooldown by one.
4. If every unfinished ordinary core is cooling and no annealing candidate is
   eligible, repeated scheduler cooldown drains eventually make ordinary work
   available; cooldown cannot deadlock the tuner.
5. The picked core is marked `in_test` and persisted before its worker starts,
   then cleared on every delivered result.

## Interruption contract

- `_reconstruct_scheduling_position()` rebuilds the in-memory cursors from the
  real test log so cyclic orders continue where they stopped.
- Synthetic crash-recovery rows (`duration_seconds = NULL`) do not move either
  cursor because they are evidence records, not completed tests.
- Resume applies crash penalties only after an actual reboot since the last
  execution checkpoint. A same-boot process exit clears stale `in_test`
  bookkeeping without moving offsets or scheduling cursors.
