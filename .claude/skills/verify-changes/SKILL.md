---
name: verify-changes
description: Use after changing any code in simo-flow and before claiming the work is done, committing, or opening a PR. Runs lint, tests and the mutation sweep, reads the diff for out-of-scope edits and weakened tests, and reports pass or fail with the real command output attached.
---

# Verify changes

**Done is the gates being run and observed, not the diff looking right.**

## Run the gates

```bash
.claude/skills/verify-changes/check.sh
```

Three gates, in CI's order: `ruff` → `pytest` → `tools/mutation_sweep.py`.

The third is the one that matters most. Green tests prove nothing if a test was
loosened to get there; the sweep disables each safety guard in turn and fails if
the suite stays green. See `tools/mutation_sweep.py` for why it exists.

## Then read the diff

The script cannot judge these. Do them by hand, every time:

1. **`git diff` — every changed line traces to the request.** Anything else is
   scope creep: say so and offer to revert it separately.
2. **No test was weakened.** An assertion deleted, a case skipped, a threshold
   loosened, an `xfail` added — each one makes the suite greener and weaker.
   Flag it even when the sweep passes.
3. **A bug fix has a regression test that failed before the fix.** If it has
   never been observed failing, it is not yet a regression test.

## Report

State the gate results with the command output attached, not a summary of it.
If anything failed, say what failed and stop — do not describe the work as done.

Never: "should pass", "looks correct", "tests should be fine".
