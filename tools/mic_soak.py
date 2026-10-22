#!/usr/bin/env python3
"""Open and close the real microphone N times and report how often it refuses.

Why this exists: every one of the project's 7 test files replaces the microphone
with a fake, and a fake microphone always opens. The failure Lucas actually hits
— PortAudio returning an internal error, roughly 4% of presses — cannot be
reproduced by anything in the suite, because a mock cannot refuse (lessons.md
019). The only way to see it is to drive the real device, many times, and count.

That turns "it sometimes glitches" into a number, which is the difference
between a bug you can fix and a bug you can only apologise for.

**This takes the microphone.** It never runs in `check.sh --fast`: the Stop hook
kills anything over 60s, and more importantly it would steal dictation from
whoever is using the machine. Run it when you are away from the Mac.

Usage:
    ./.venv/bin/python tools/mic_soak.py            # 200 opens
    ./.venv/bin/python tools/mic_soak.py -n 50      # fewer
    ./.venv/bin/python tools/mic_soak.py --hold 0.5 # hold each stream open longer

Exit codes, matching the project's gate convention:
    0  ran, failure rate at or below --max-failure-rate
    1  ran, failure rate above it
    2  could not run at all (no device, import failed)
"""
import argparse
import json
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RESULTS = ROOT / "mic-soak-results.json"

_stop = False


def _on_signal(signum, _frame) -> None:
    """Stop at the next loop boundary rather than mid-open.

    Killing between stream.start() and stream.close() leaks a CoreAudio stream
    and can leave the device unavailable to every other app, which is a worse
    outcome than an unfinished run.
    """
    global _stop
    print(f"\n  interrupted (signal {signum}) — finishing the current open", flush=True)
    _stop = True


def soak(n: int, hold: float) -> dict:
    """Open and close the default input device `n` times. Never raises."""
    from engine import audio

    device = audio.default_input_name()
    print(f"  device: {device}")
    print(f"  opening it {n} times, holding each for {hold}s\n")

    rec = audio.Recorder()
    failures, times, episodes = [], [], []
    run = 0  # consecutive failures, so clustering is visible

    for i in range(1, n + 1):
        if _stop:
            n = i - 1
            break
        t0 = time.time()
        ok = rec._open_stream()
        dt = (time.time() - t0) * 1000
        if ok:
            time.sleep(hold)
            rec._close_stream()
            times.append(dt)
            if run:
                episodes.append(run)
                run = 0
        else:
            failures.append({"attempt": i, "after_ms": round(dt, 1)})
            run += 1
            # Same recovery the app itself uses, so the number this reports is
            # the rate the user would actually experience, not a worse one.
            rec._reinit_portaudio()
        if i % 25 == 0:
            print(f"    {i}/{n}  failures so far: {len(failures)}", flush=True)
    if run:
        episodes.append(run)

    times.sort()
    return {
        "device": device,
        "attempts": n,
        "failures": len(failures),
        "failure_rate_pct": round(100 * len(failures) / n, 2) if n else 0.0,
        "episodes": episodes,
        "worst_episode": max(episodes) if episodes else 0,
        "open_ms_median": round(times[len(times) // 2], 1) if times else None,
        "open_ms_worst": round(times[-1], 1) if times else None,
        "detail": failures[:20],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=200, help="how many opens")
    ap.add_argument("--hold", type=float, default=0.2, help="seconds to hold each open")
    ap.add_argument("--max-failure-rate", type=float, default=0.0,
                    help="exit 1 above this percentage; 0 means report only")
    args = ap.parse_args()

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError):
            pass  # not the main thread, or the platform lacks it

    try:
        result = soak(args.n, args.hold)
    except Exception as e:
        # Exit 2, never 1: "could not measure" must never read as "measured and
        # fine". The gate convention in check.sh depends on that distinction.
        print(f"  could not run the soak ({e.__class__.__name__}: {e})")
        return 2

    print(f"\n  {result['failures']} failures in {result['attempts']} opens "
          f"= {result['failure_rate_pct']}%")
    if result["episodes"]:
        print(f"  clustered into {len(result['episodes'])} episode(s), "
              f"worst {result['worst_episode']} consecutive")
    if result["open_ms_median"] is not None:
        print(f"  open took {result['open_ms_median']}ms median, "
              f"{result['open_ms_worst']}ms worst")

    RESULTS.write_text(json.dumps(result, indent=2) + "\n")
    print(f"  written to {RESULTS.name}")

    if args.max_failure_rate and result["failure_rate_pct"] > args.max_failure_rate:
        print(f"  ABOVE the {args.max_failure_rate}% bar")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
