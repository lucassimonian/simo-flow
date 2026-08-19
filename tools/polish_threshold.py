#!/usr/bin/env python3
"""At what length does the cleanup model stop being worth waiting for?

The integrity guard refuses any cleanup that is not a deletion-only edit of what
was actually said. When it refuses, the raw transcript is pasted instead — so the
user waited for a language model and received nothing from it.

A first measurement showed that happening on 92% of the longest utterances, at a
cost of ~3.6 seconds each. This finds where that starts: it groups real dictation
history by length and reports, per bucket, how often the model's answer survived.

The output is the evidence for a threshold. Guessing one would be a number nobody
could defend; this makes it a reading.

Read-only — reads history, writes nothing, pastes nothing.

Usage:
    ./.venv/bin/python tools/polish_threshold.py
    ./.venv/bin/python tools/polish_threshold.py --per-bucket 8
"""
import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import polish, store  # noqa: E402

# Word-count bands. Chosen to match how people actually dictate: a sentence, a
# couple of sentences, a paragraph, a monologue.
BUCKETS = [(1, 15), (16, 40), (41, 80), (81, 10_000)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-bucket", type=int, default=6, help="utterances sampled per band")
    args = ap.parse_args()

    rows = [r["raw_text"] for r in store.history_all() if r["raw_text"]]
    candidates = [t for t in rows if polish.needs_cleanup(t)]
    print(f"{len(rows)} dictations in history, {len(candidates)} that invoke the cleanup model\n")

    polish.polish("warm up")  # never charge the first bucket for a cold model

    print(f"{'words':>12}  {'n':>3}  {'median':>9}  {'kept':>6}  {'rejected':>9}")
    print("-" * 48)
    findings = []
    for lo, hi in BUCKETS:
        sample = [t for t in candidates if lo <= len(t.split()) <= hi][: args.per_bucket]
        if not sample:
            continue
        latencies, kept = [], 0
        for text in sample:
            t0 = time.perf_counter()
            out = polish.polish(text)
            latencies.append((time.perf_counter() - t0) * 1000)
            # polish() returns the input unchanged when the guard refuses the
            # model's answer — so "different" means the cleanup was actually used.
            if out != text:
                kept += 1
        n = len(sample)
        rejected_pct = 100 * (n - kept) / n
        band = f"{lo}–{hi if hi < 10_000 else '∞'}"
        print(f"{band:>12}  {n:>3}  {statistics.median(latencies):>7.0f}ms  "
              f"{100 * kept / n:>5.0f}%  {rejected_pct:>8.0f}%")
        findings.append((band, statistics.median(latencies), rejected_pct))

    print()
    wasted = [f for f in findings if f[2] >= 60]
    if wasted:
        print("Bands where most of the wait buys nothing:")
        for band, ms, pct in wasted:
            print(f"  {band} words — {pct:.0f}% rejected, {ms:.0f}ms each")
        print("\nSkipping the model in those bands costs almost nothing, because its")
        print("answer was being thrown away, and returns the raw transcript instantly.")
    else:
        print("No band wastes most of its wait — the model is earning its latency.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
