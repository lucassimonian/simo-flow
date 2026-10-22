#!/usr/bin/env bash
# Every gate simo-flow's CI runs, in the same order, with the real output kept.
# Exit 0 only if all three pass. Never "looks fine" — the output is the evidence.
#
# Deliberately a flat script rather than a runner class: three gates in one
# file, where a framework would be more code than the thing it runs.
set -uo pipefail
cd "$(dirname "$0")/../../.." || exit 2

# --fast skips the mutation sweep. The sweep rewrites source files and restores
# them at the end, so a run that is killed — by a hook timeout, or Ctrl+C —
# leaves the working tree silently sabotaged. It happened twice on 2026-10-15.
# Automatic callers (the Stop hook) must always pass --fast.
FAST=0
[ "${1:-}" = "--fast" ] && FAST=1

# A mutation sweep rewrites source files one at a time. Testing against a tree
# it is holding mid-rewrite reports failures that are not real — it happened on
# 2026-10-15 and briefly looked like a genuine regression. Refuse rather than
# report a result that means nothing.
if [ -f .mutation-sweep-journal.json ]; then
  echo "A mutation sweep is running and has source rewritten right now."
  echo "Nothing was verified. Re-run once it finishes."
  exit 2
fi

PY=.venv/bin/python
[ -x "$PY" ] || PY=python3

pass=0; fail=0
run_gate () {
  local name="$1"; shift
  local out status
  out=$("$@" 2>&1); status=$?
  if [ $status -eq 0 ]; then
    printf '\n✅ %s\n   %s\n' "$name" "$(echo "$out" | grep -v '^[[:space:]]*$' | tail -1)"
    pass=$((pass+1))
  else
    printf '\n❌ %s (exit %d)\n%s\n' "$name" "$status" "$(echo "$out" | grep -v '^[[:space:]]*$' | tail -25)"
    fail=$((fail+1))
  fi
}

echo "verify-changes — simo-flow"
echo "python: $($PY --version 2>&1)"

run_gate "lint (ruff)"        $PY -m ruff check .
run_gate "types (mypy)"       $PY -m mypy engine/ --ignore-missing-imports
run_gate "soak harness tests"  $PY tools/test_mic_soak.py
run_gate "tests (pytest)"     $PY -m pytest tests/ --ignore=tests/test_pipeline.py -q -p no:warnings
if [ "$FAST" -eq 1 ]; then
  printf '\n·  mutation sweep skipped (--fast). Run without --fast before a release.\n'
else
  run_gate "mutation sweep"   $PY tools/mutation_sweep.py
fi

printf '\n── %d passed, %d failed ──\n' "$pass" "$fail"
[ "$fail" -eq 0 ] || exit 1
