#!/usr/bin/env python3
"""Measure the dictation pipeline, without typing anything into your window.

The numbers in the README come from here, so they can be re-checked rather than
trusted. Reports each stage separately, because "it takes about a second" hides
which second: whisper, the cleanup model, or the paste.

The stopwatch stops when the keystroke would land — not when `paste_text` returns.
The clipboard restore afterwards is ~300ms of housekeeping the user never waits
for, and counting it made every previously reported figure pessimistic.

Real keyboard output is disabled (see tools/_safe_input.py). An earlier version of
this benchmark did not do that and pasted its test sentence twelve times into the
user's terminal.

Usage:
    ./.venv/bin/python tools/benchmark.py
    ./.venv/bin/python tools/benchmark.py --runs 5
"""
import argparse
import statistics as st
import time
import wave
from pathlib import Path

import numpy as np

from _safe_input import stub_input  # noqa: E402  (also fixes sys.path)

from engine import inject, polish, stt  # noqa: E402
from engine.audio import RATE  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "tests" / "utterance.wav"


def load(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return pcm.astype(np.float32) / 32768.0


def one_run(samples: np.ndarray) -> tuple[float, float, float, float, bool]:
    t0 = time.perf_counter()
    raw = stt.transcribe(samples, initial_prompt="")
    t_stt = time.perf_counter()
    cleaned_needed = polish.needs_cleanup(raw)
    text = polish.polish(raw)
    t_polish = time.perf_counter()
    seen: list[float] = []
    inject.paste_text(text, on_pasted=lambda: seen.append(time.perf_counter()))
    t_seen = seen[0] if seen else time.perf_counter()
    return (
        (t_stt - t0) * 1000,
        (t_polish - t_stt) * 1000,
        (t_seen - t_polish) * 1000,
        (t_seen - t0) * 1000,
        cleaned_needed,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--wav", default=str(SAMPLE))
    args = ap.parse_args()

    stub_input()
    samples = load(Path(args.wav))
    audio_sec = len(samples) / RATE

    original = stt.current_tier()
    print(f"sample: {Path(args.wav).name}  ({audio_sec:.1f}s of speech)  runs: {args.runs}\n")
    print(f"{'tier':>9}  {'whisper':>9}  {'cleanup':>9}  {'paste':>7}  {'TOTAL':>8}  cleanup ran")
    for tier in ("accurate", "fast"):
        stt.set_tier(tier)
        stt.transcribe(samples)  # warm the model so we measure the steady state
        runs = [one_run(samples) for _ in range(args.runs)]
        med = [st.median(r[i] for r in runs) for i in range(4)]
        ran = sum(1 for r in runs if r[4])
        print(f"{tier:>9}  {med[0]:>8.0f}ms  {med[1]:>8.0f}ms  {med[2]:>6.0f}ms  "
              f"{med[3]:>7.0f}ms  {ran}/{len(runs)}")
    stt.set_tier(original)
    print("\nStopwatch stops when the keystroke lands. The ~300ms clipboard restore")
    print("afterwards is not counted — the text is already on screen by then.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
