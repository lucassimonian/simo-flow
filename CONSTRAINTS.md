# Constraints

What "good enough to ship" means in SimoFlow. Read this before writing code.
**Do not weaken it to make a change pass.**

Written 2026-10-18. Tightening this file should be quiet; loosening it should
be loud.

## The floor — always true, no setup

- No new suppression comments (`# noqa`, `# type: ignore`)
- No unimplemented stubs, no empty `except:`
- No skipped or deleted tests without a reason in the commit message
- No secrets in source
- This file does not get weakened to make a change pass

## Enforced, with numbers

| What | Rule | Proven by | Runs |
|---|---|---|---|
| Lint | Zero errors | `ruff check .` | every change |
| Tests | , zero failures | `pytest tests/ --ignore=tests/test_pipeline.py` | every change |
| Guards are real | **64 of 64** safety guards fail a test when broken | `tools/mutation_sweep.py` | before a release, and CI |
| Nothing destroys a user's clipboard | The pasteboard guard in `engine/inject.py` stays live | covered by the sweep above | every change |

Every row names a command. A row without one is an aspiration, not a constraint.

## Measured, not yet enforced

| What | Today | Direction |
|---|---|---|
| Line coverage | **not measured** (`pytest-cov` is not installed) | install it, record the number, then it must not fall |
| Time to the cursor | 570 ms accurate tier, 110 ms fast tier | must not grow |

## How this is enforced

`.claude/skills/verify-changes/check.sh` runs these. The global Stop hook runs
it automatically after any change and reports failures.

- `check.sh --fast` — lint and tests, about 10 seconds. What the hook runs.
- `check.sh` — adds the mutation sweep, several minutes. Run before a release.

The sweep is excluded from `--fast` deliberately: it rewrites source files
while it runs, and a kill mid-rewrite leaves the tree silently sabotaged. That
happened twice on 2026-10-15.

## One honest weakness

Every constraint above is checked by SimoFlow's own tests, which SimoFlow's own
authors wrote. The mutation sweep makes that much stronger than usual, because
it proves the tests actually catch a break rather than merely passing. But there
is still **no outside opinion** in this bar: no vulnerability scan, no benchmark
against a real device, nothing a stranger would recognise.

Nearest useful addition: a dependency vulnerability scan. Nothing at high or
above. It is the cheapest outside check available for a Python project.

## Exceptions

None.
