#!/usr/bin/env python3
"""Break each safety guard on purpose and check that a test screams.

Why this exists: two bugs reached the user in one day, and both had passing tests.
One test (`all(flags)` on the paste sequence) actively *asserted* the broken
behaviour — a green suite was protecting the bug. A test written by whoever
misunderstood the problem cannot catch that misunderstanding. The only thing that
can is deliberately breaking the code and watching whether anything fails.

Doing that by hand only covers the guards you happen to doubt, which is exactly
the wrong sample. This runs the whole set.

Each MUTATION below disables one load-bearing guard. A guard whose removal leaves
the suite green is **untested** — no matter how many tests appear to cover the file.
Those are the bugs the user finds before I do.

Usage:
    ./.venv/bin/python tools/mutation_sweep.py
    ./.venv/bin/python tools/mutation_sweep.py --only inject
"""
import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (label, file, exact source to replace, replacement) — each disables one guard.
MUTATIONS: list[tuple[str, str, str, str]] = [
    # ---- inject: the paste path -------------------------------------------
    (
        "inject/cmd-flag-cleared-on-release",
        "engine/inject.py",
        "    _post_key(KEY_CMD, False, 0)",
        "    _post_key(KEY_CMD, False, cmd)",
    ),
    (
        "inject/four-event-sequence",
        "engine/inject.py",
        "    _post_key(KEY_CMD, True, cmd)\n",
        "",
    ),
    (
        "inject/accessibility-gate",
        "engine/inject.py",
        "    if not _is_trusted():",
        "    if False:",
    ),
    (
        "inject/clipboard-changecount-guard",
        "engine/inject.py",
        "        if _change_count() != expected:",
        "        if False:",
    ),
    (
        "inject/focus-restore",
        "engine/inject.py",
        "    if focus and focus.get(\"pid\") and not _same_app(_capture_focus(), focus):",
        "    if False:",
    ),
    (
        "inject/same-app-checks-bundle-id",
        "engine/inject.py",
        "    return a.get(\"bundle_id\") == b.get(\"bundle_id\")",
        "    return True",
    ),
    (
        "inject/abort-if-focus-never-arrives",
        "engine/inject.py",
        "        if not _wait_until(lambda: _same_app(_capture_focus(), focus), ACTIVATE_DELAY):",
        "        if False:",
    ),
    # ---- audio: capture and device handling -------------------------------
    (
        "audio/silence-guard",
        "engine/audio.py",
        "        if float(np.sqrt(np.mean(trimmed**2))) < SILENCE_RMS:",
        "        if False:",
    ),
    (
        "audio/too-short-guard",
        "engine/audio.py",
        "        if len(samples) < MIN_UTTERANCE_SEC * RATE:",
        "        if False:",
    ),
    (
        "audio/reactive-retry-after-failed-open",
        "engine/audio.py",
        '        print("[simo] retrying with a fresh PortAudio device list", flush=True)\n'
        "        self._reinit_portaudio()\n"
        "        if self._open_stream():\n"
        "            self._device_id = default_input_device()\n"
        '            print("[simo] mic recovered after re-initialising PortAudio", flush=True)\n'
        "            return\n",
        "",
    ),
    # `audio/no-coreaudio-teardown-on-the-hotkey-path` lived here and has been
    # deliberately removed, not quietly dropped for surviving. It asserted "do not
    # re-initialise PortAudio when the device id changes", which mattered only
    # because that call blocked the fn-key event tap. Nothing reachable from the
    # hotkey blocks anything now, so reintroducing that check is no longer a
    # freeze — it is at most a wasted teardown on the audio thread, and no test
    # should fail for it. The two mutations below replace it with the stronger
    # claim: the device work is not on the caller's thread at all.
    #
    # begin()/end() are called from the fn-key CGEventTap callback, and a tap that
    # blocks stalls the system input pipeline — every key in every app stops.
    # These put the device work back on the caller's thread, which is what froze a
    # MacBook mid-meeting in v2.2.1.
    (
        "audio/mic-open-never-blocks-the-hotkey-thread",
        "engine/audio.py",
        "        self._audio_q.put(lambda: self._prepare_device(generation))",
        "        self._prepare_device(generation)",
    ),
    (
        "audio/stale-open-failure-cannot-cancel-a-newer-press",
        "engine/audio.py",
        "            if generation != self._generation:\n                return",
        "            if False:\n                return",
    ),
    (
        "audio/mic-close-never-blocks-the-hotkey-thread",
        "engine/audio.py",
        "        self._audio_q.put(self._close_stream)\n        self.level = 0.0",
        "        self._close_stream()\n        self.level = 0.0",
    ),
    (
        "audio/distinguish-unopenable-mic-from-silence",
        "engine/audio.py",
        '                "microphone unavailable — see log" if open_failed else "no audio captured"',
        '                "no audio captured"',
    ),
    # ---- polish: the dictation-integrity guard ----------------------------
    (
        "polish/subsequence-check",
        "engine/polish.py",
        "    if not _is_subsequence(out_w, raw_w):",
        "    if False:",
    ),
    (
        "polish/negator-retention",
        "engine/polish.py",
        "    if _negator_count(out_w) < _negator_count(raw_w):",
        "    if False:",
    ),
    (
        "polish/content-retention",
        "engine/polish.py",
        "        if kept / len(content) < _MIN_CONTENT_RETENTION:",
        "        if False:",
    ),
    (
        "polish/skip-when-nothing-to-clean",
        "engine/polish.py",
        "    if not needs_cleanup(raw_text):",
        "    if False:",
    ),
    (
        "polish/legitimate-doubles-not-stutters",
        "engine/polish.py",
        "    return any(a == b and a not in _LEGITIMATE_DOUBLES for a, b in zip(words, words[1:]))",
        "    return any(a == b for a, b in zip(words, words[1:]))",
    ),
    (
        "polish/failure-is-logged",
        "engine/polish.py",
        '        print(f"[simo] polish unavailable ({type(e).__name__}: {e}) — pasting raw transcript", flush=True)',
        "        pass",
    ),
    # ---- stt: transcript normalisation -----------------------------------
    (
        "stt/flatten-whisper-line-wrapping",
        "engine/stt.py",
        "    return dedupe_sentences(flatten_whitespace(r.json().get(\"text\", \"\")))",
        "    return dedupe_sentences(r.json().get(\"text\", \"\").strip())",
    ),
    # ---- api: the security guards ----------------------------------------
    (
        "api/origin-guard-fails-closed",
        "engine/api.py",
        '    raise HTTPException(\n'
        '        status_code=403, detail="refused: request carried neither Origin nor Referer"\n'
        "    )",
        "    return",
    ),
    (
        "api/host-allowlist",
        "engine/api.py",
        "    if host not in _ALLOWED_HOSTS:",
        "    if False:",
    ),
    (
        "api/export-is-origin-guarded",
        "engine/api.py",
        "    _reject_cross_origin(request)\n    return store.history_all()",
        "    return store.history_all()",
    ),
    # ---- the utterance claim ---------------------------------------------
    (
        "main/single-consumer-claim",
        "engine/__main__.py",
        "            if self._consumed:\n                return False",
        "            if False:\n                return False",
    ),
    (
        "main/discard-declines-while-key-held",
        "engine/__main__.py",
        "            if skip_if_key_down and self._fn_down:\n                return False",
        "            if False:\n                return False",
    ),
    (
        "main/transcript-saved-even-if-paste-fails",
        "engine/__main__.py",
        "            store.log_dictation(raw, cleaned, int(dt), audio_sec=len(samples) / 16000)\n"
        "            if not pasted:",
        "            if not pasted:",
    ),
    # ---- log hygiene ------------------------------------------------------
    (
        "main/log-trim-above-cap",
        "engine/__main__.py",
        "        if os.path.getsize(path) <= max_bytes:\n            return False",
        "        if True:\n            return False",
    ),
    # Indentation matters here: _keep_stream moved from a method to a module-level
    # function, and the old 12-space mutation silently stopped matching. It was
    # reported as "out of date" for one release while this guard — the one keeping
    # dictated transcripts out of a world-readable file — went unchecked.
    (
        "main/transcripts-never-reach-the-boot-log",
        "engine/__main__.py",
        "        return stream is not None and stream.isatty()",
        "        return stream is not None",
    ),
    (
        "main/boot-log-permissions-tightened",
        "engine/__main__.py",
        "        if os.path.exists(boot):\n            os.chmod(boot, 0o600)",
        "        if False:\n            os.chmod(boot, 0o600)",
    ),
    (
        "main/duplicate-writer-detection",
        "engine/__main__.py",
        "    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)",
        "    return False",
    ),
]

PYTEST = [
    sys.executable, "-m", "pytest", "tests/",
    "--ignore=tests/test_pipeline.py", "-x", "-q", "--no-header", "-p", "no:cacheprovider",
]


# The suite runs in ~3s. This is not a tuning knob, it is a deadlock backstop.
SUITE_TIMEOUT_SEC = 300


def run_suite(explain: bool = False) -> bool:
    """True if the suite passes.

    Bounded, because a mutation that makes a test hang would otherwise wedge this
    tool for ever with nothing printed — and the first symptom would be a CI job
    that never finishes and a developer guessing why. A suite that hangs is not a
    suite that passes, so a timeout counts as a failure (the mutation was caught).

    `explain` prints pytest's own output. Off during the sweep, where a failing
    suite is the *expected* result and thousands of lines would bury the report —
    on for the pre-flight run, where a failure means something is genuinely broken
    and swallowing the reason leaves you with "the suite is already failing" and
    nowhere to go. That happened, on CI, and cost a debugging round trip.
    """
    try:
        r = subprocess.run(
            PYTEST, cwd=ROOT, capture_output=True, text=True, timeout=SUITE_TIMEOUT_SEC
        )
    except subprocess.TimeoutExpired:
        print(f"      (suite hung for {SUITE_TIMEOUT_SEC}s — counted as caught)", flush=True)
        return False
    if explain and r.returncode != 0:
        print((r.stdout or "") + (r.stderr or ""), flush=True)
    return r.returncode == 0


def _dirty_files() -> list[str]:
    """Tracked files with uncommitted changes."""
    r = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        cwd=ROOT, capture_output=True, text=True,
    )
    return [line[3:].strip() for line in r.stdout.splitlines() if line.strip()]


def _guard_working_tree(targets: set[str]) -> str | None:
    """Refuse to mutate a file that has uncommitted changes.

    This tool rewrites source in place and restores it afterwards. `finally`
    covers an exception or Ctrl-C, but not a hard kill — and if that happened
    while the tree was already dirty, there would be no way to tell our mutation
    from the author's real work. Requiring the target files to be committed means
    the worst case is always recoverable with `git checkout -- <file>`.
    """
    clash = sorted(targets & set(_dirty_files()))
    if clash:
        return (
            "Refusing to run: these files have uncommitted changes, and this tool\n"
            "edits source in place:\n"
            + "".join(f"  - {f}\n" for f in clash)
            + "Commit or stash them first, so an interrupted run is always recoverable\n"
            "with: git checkout -- <file>"
        )
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="substring filter on the mutation label")
    args = ap.parse_args()

    muts = [m for m in MUTATIONS if not args.only or args.only in m[0]]

    problem = _guard_working_tree({relpath for _l, relpath, _o, _n in muts})
    if problem:
        print(problem)
        return 2

    if not run_suite(explain=True):
        print("The suite is already failing — fix that before mutating anything.")
        return 2

    survivors, killed, unapplied = [], [], []
    for label, relpath, old, new in muts:
        path = ROOT / relpath
        original = path.read_text()
        if old not in original:
            unapplied.append(label)  # the code moved; the mutation is stale
            print(f"  ??  {label}  (source not found — mutation is out of date)")
            continue
        try:
            path.write_text(original.replace(old, new, 1))
            if run_suite():
                survivors.append(label)
                print(f"  !!  {label}  SURVIVED — this guard is untested")
            else:
                killed.append(label)
                print(f"  ok  {label}")
        finally:
            path.write_text(original)

    # Belt and braces: prove we put every file back exactly as we found it.
    left_behind = sorted({relpath for _l, relpath, _o, _n in muts} & set(_dirty_files()))
    if left_behind:
        print("\n!! MUTATIONS WERE NOT FULLY REVERTED — recover with:")
        for f in left_behind:
            print(f"   git checkout -- {f}")
        return 3

    print(f"\n{len(killed)}/{len(muts)} guards are genuinely tested")
    if unapplied:
        print(f"\n{len(unapplied)} mutation(s) out of date — update tools/mutation_sweep.py:")
        for label in unapplied:
            print(f"  - {label}")
    if survivors:
        print(f"\n{len(survivors)} UNTESTED guard(s) — a bug here ships silently:")
        for label in survivors:
            print(f"  - {label}")
        return 1
    print("\nEvery guard, when broken, is caught by a test.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
