#!/usr/bin/env python3
"""The sweep rewrites real source files. Prove it puts them back even when killed.

On 2026-10-15 a timeout killed a sweep mid-mutation and left engine/inject.py
with its pasteboard guard replaced by `if False:` — the guard that stops a paste
destroying whatever the user had just copied. The tree looked normal.

Run: ./.venv/bin/python tools/test_sweep_restores.py
"""
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SWEEP = ROOT / "tools" / "mutation_sweep.py"
JOURNAL = ROOT / ".mutation-sweep-journal.json"
PY = str(ROOT / ".venv" / "bin" / "python")
sys.path.insert(0, str(ROOT / "tools"))
from mutation_sweep import MUTATIONS

failures = []


def dirty():
    out = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                         capture_output=True, text=True).stdout
    # Only the files the sweep actually rewrites. Anything else dirty in the
    # tree is the developer's own work in progress, not the sweep's doing.
    targets = {relpath for _l, relpath, _o, _n in MUTATIONS}
    return sorted(l[3:] for l in out.splitlines() if l[3:] in targets)


def kill_mid_run(sig):
    """Start a sweep, let it mutate something, kill it, report what was left."""
    JOURNAL.unlink(missing_ok=True)
    p = subprocess.Popen([PY, str(SWEEP)], cwd=ROOT,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    mutated = False
    for _ in range(600):                      # wait for a live mutation
        time.sleep(0.1)
        if JOURNAL.exists() and dirty():
            mutated = True
            break
        if p.poll() is not None:
            break
    if not mutated:
        p.kill()
        p.wait()
        return None, "sweep never reached a live mutation"
    p.send_signal(sig)
    try:
        p.wait(timeout=20)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()
    time.sleep(0.5)
    return dirty(), None


for sig, name in [(signal.SIGTERM, "SIGTERM (what a timeout sends)"),
                  (signal.SIGINT, "SIGINT (Ctrl+C)")]:
    left, err = kill_mid_run(sig)
    if err:
        failures.append(f"{name}: {err}")
    elif left:
        failures.append(f"{name}: left files rewritten: {left}")
    else:
        print(f"  ok  {name} — source restored")

# SIGKILL cannot be caught. The journal must let the next run repair it.
left, err = kill_mid_run(signal.SIGKILL)
if err:
    failures.append(f"SIGKILL: {err}")
elif not left:
    failures.append("SIGKILL left nothing behind — the test proved nothing")
elif not JOURNAL.exists():
    failures.append("SIGKILL left files rewritten and no journal to repair them")
else:
    print(f"  ok  SIGKILL — left {left}, journal written")
    import mutation_sweep
    mutation_sweep.recover_from_journal()
    still = dirty()
    if still:
        failures.append(f"recovery did not restore: {still}")
    elif JOURNAL.exists():
        failures.append("journal not cleared after recovery")
    else:
        print("  ok  next run repaired it from the journal")

subprocess.run(["git", "checkout", "--", "engine/"], cwd=ROOT)
JOURNAL.unlink(missing_ok=True)
for f in failures:
    print("FAIL:", f)
print("ok" if not failures else f"{len(failures)} failed")
sys.exit(1 if failures else 0)
