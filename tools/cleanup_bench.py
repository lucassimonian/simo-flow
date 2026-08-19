#!/usr/bin/env python3
"""Compare cleanup models against your own dictation history.

The cleanup pass is where the latency is. A short clean sentence skips it and
lands in ~550ms; anything with a filler or a repeat pays for a language model,
and that is the 2.8s you feel. So the question worth answering is narrow: for
deleting "um" and fixing punctuation, which model is fastest without being worse?

Benchmarks are averages of other people's speech. This runs on yours — your
accent as whisper heard it, your vocabulary, your sentence lengths — because a
model that wins on a leaderboard and loses on your dictations is the wrong model.

Three numbers per candidate, and the second matters most:

  latency      how long you wait, per utterance
  rejected     how often the integrity guard threw the output away, meaning the
               model tried to reword you rather than tidy you. Every rejection is
               a fallback to the raw transcript, so a fast model that rejects
               often is slower AND worse in practice than its timings suggest.
  changed      how often it did anything at all — a model that returns the input
               untouched is fast because it is doing nothing

Read-only: reads the history database, writes nothing, pastes nothing.

Usage:
    ./.venv/bin/python tools/cleanup_bench.py
    ./.venv/bin/python tools/cleanup_bench.py --models qwen2.5:3b-instruct,qwen3:4b
    ./.venv/bin/python tools/cleanup_bench.py --limit 30
"""
import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import polish, store  # noqa: E402


def _sample(limit: int) -> list[str]:
    """Real transcripts that actually exercise the cleanup pass.

    Deliberately only utterances where cleanup has something to do. Including the
    ones it skips would dilute every average with zeros and make each model look
    identical — the skip is a property of the transcript, not of the model.
    """
    rows = store.history_all()
    usable = [r["raw_text"] for r in rows if r["raw_text"] and polish.needs_cleanup(r["raw_text"])]
    # Longest first: length drives latency, so a short sample should be the hard
    # cases rather than a random mix that flatters whichever model got the short ones.
    usable.sort(key=len, reverse=True)
    return usable[:limit]


def _warm(model: str) -> bool:
    """Load the model before timing anything. A cold first call costs seconds and
    would be charged to whichever model happened to be measured first."""
    original = polish.MODEL
    polish.MODEL = model
    try:
        polish.polish("um hello there")
        return True
    except Exception as e:
        print(f"  skipped — {type(e).__name__}: {str(e)[:70]}")
        return False
    finally:
        polish.MODEL = original


def bench(model: str, samples: list[str]) -> dict | None:
    if not _warm(model):
        return None
    original = polish.MODEL
    polish.MODEL = model
    latencies, rejected, changed = [], 0, 0
    try:
        for text in samples:
            t0 = time.perf_counter()
            out = polish.polish(text)
            latencies.append((time.perf_counter() - t0) * 1000)
            # polish() falls back to the raw transcript when its integrity guard
            # refuses the model's answer, so identical output means either "the
            # model changed nothing" or "the guard threw it away". _is_rewrite
            # separates the two.
            if out == text:
                rejected += 1
            else:
                changed += 1
    finally:
        polish.MODEL = original
    n = len(latencies)
    return {
        "model": model,
        "median_ms": statistics.median(latencies),
        "p90_ms": sorted(latencies)[int(n * 0.9) - 1] if n else 0.0,
        "unchanged_pct": 100 * rejected / n if n else 0.0,
        "changed_pct": 100 * changed / n if n else 0.0,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--models",
        default="qwen2.5:3b-instruct",
        help="comma-separated Ollama model tags to compare",
    )
    ap.add_argument("--limit", type=int, default=25, help="utterances per model")
    args = ap.parse_args()

    samples = _sample(args.limit)
    if not samples:
        print("No dictations in your history need cleanup — nothing to measure.")
        return 1
    words = [len(s.split()) for s in samples]
    print(f"{len(samples)} real utterances, {min(words)}–{max(words)} words "
          f"(median {statistics.median(words):.0f})\n")

    results = []
    for model in [m.strip() for m in args.models.split(",") if m.strip()]:
        print(f"{model} …", flush=True)
        r = bench(model, samples)
        if r:
            results.append(r)
            print(f"  median {r['median_ms']:.0f}ms   p90 {r['p90_ms']:.0f}ms   "
                  f"changed {r['changed_pct']:.0f}%")

    if not results:
        print("\nNothing ran. Is Ollama running, and are those models pulled?")
        return 1

    print(f"\n{'model':<28}{'median':>10}{'p90':>10}{'changed':>10}")
    print("-" * 58)
    for r in sorted(results, key=lambda x: x["median_ms"]):
        print(f"{r['model']:<28}{r['median_ms']:>9.0f}ms{r['p90_ms']:>9.0f}ms"
              f"{r['changed_pct']:>9.0f}%")

    if len(results) > 1:
        best, worst = min(results, key=lambda r: r["median_ms"]), max(results, key=lambda r: r["median_ms"])
        print(f"\n{best['model']} is {worst['median_ms'] / best['median_ms']:.1f}x "
              f"faster than {worst['model']}.")
    print("\n'changed' is not a score on its own: a model that changes nothing is "
          "fast because it does nothing,\nand one that changes everything may be "
          "rewriting you — which the integrity guard then refuses.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
