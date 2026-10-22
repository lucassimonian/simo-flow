#!/usr/bin/env python3
"""The soak harness reports a rate; these prove it reports the right one.

A measuring tool that quietly miscounts is worse than no tool, because the
number it produces gets written into CONSTRAINTS.md and trusted afterwards.
Every test here uses a fake recorder — the point is the arithmetic and the
reporting contract, not the device. The device is what mic_soak.py itself
exists to exercise.

Run: ./.venv/bin/python tools/test_mic_soak.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import mic_soak  # noqa: E402

failures = []


class FakeRecorder:
    """Opens successfully except on the attempts listed in `fail_on`."""

    def __init__(self, fail_on):
        self.fail_on = set(fail_on)
        self.attempt = 0
        self.closes = 0
        self.reinits = 0

    def _open_stream(self):
        self.attempt += 1
        return self.attempt not in self.fail_on

    def _close_stream(self):
        self.closes += 1

    def _reinit_portaudio(self):
        self.reinits += 1


def run(n, fail_on, hold=0.0):
    """Drive soak() against a fake recorder and return its report."""
    fake = FakeRecorder(fail_on)
    import engine.audio as audio
    real_rec, real_name = audio.Recorder, audio.default_input_name
    audio.Recorder = lambda: fake
    audio.default_input_name = lambda: "Fake Microphone"
    try:
        return mic_soak.soak(n, hold), fake
    finally:
        audio.Recorder, audio.default_input_name = real_rec, real_name


# 1. A clean run reports zero, and closes every stream it opened.
r, fake = run(10, [])
if r["failures"] != 0 or r["failure_rate_pct"] != 0.0:
    failures.append(f"clean run reported {r['failures']} failures")
if fake.closes != 10:
    failures.append(f"opened 10 but closed {fake.closes} — a leaked stream")

# 2. The rate is a percentage of attempts, not a count.
r, _ = run(20, [3, 7, 11, 15])
if r["failures"] != 4 or r["failure_rate_pct"] != 20.0:
    failures.append(f"4 of 20 reported as {r['failures']} / {r['failure_rate_pct']}%")

# 3. Consecutive failures are one episode, not several. The user experiences a
#    run of five refusals as one broken moment, and the worst episode is what
#    makes the app feel unusable.
r, _ = run(20, [5, 6, 7, 8, 9])
if r["worst_episode"] != 5:
    failures.append(f"5 consecutive failures reported as worst episode {r['worst_episode']}")
if len(r["episodes"]) != 1:
    failures.append(f"one run of failures split into {len(r['episodes'])} episodes")

# 4. Separate failures are separate episodes.
r, _ = run(20, [2, 10, 18])
if len(r["episodes"]) != 3:
    failures.append(f"3 separate failures reported as {len(r['episodes'])} episodes")

# 5. A failure triggers the same recovery the app uses, so the reported rate is
#    the one the user would actually experience.
r, fake = run(10, [4])
if fake.reinits != 1:
    failures.append(f"a failed open did not re-init PortAudio ({fake.reinits} times)")

# 6. "Could not measure" must never read as "measured and fine". The gate reads
#    exit 2 as unproven and exit 1 as failed, and conflating them would let a
#    broken harness report success.
import engine.audio as _audio  # noqa: E402
_real = _audio.Recorder
_audio.Recorder = lambda: (_ for _ in ()).throw(RuntimeError("no device"))
try:
    sys.argv = ["mic_soak.py", "-n", "1"]
    code = mic_soak.main()
    if code != 2:
        failures.append(f"an unrunnable soak returned {code}, not 2")
finally:
    _audio.Recorder = _real

for f in failures:
    print("FAIL:", f)
print("ok" if not failures else f"{len(failures)} failed")
sys.exit(1 if failures else 0)
