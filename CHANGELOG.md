# Changelog

All notable changes to Simo Flow are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[Semantic Versioning](https://semver.org/).

## [2.1.0] — 2026-08-04

Everything in this release comes from one wrong assumption: that the world holds
still between you finishing a sentence and the text arriving. It doesn't. Up to
two seconds pass while whisper and the LLM run, and in that window the focused
window can change, the clipboard can change, and the permission that makes
pasting work at all can be revoked.

### Added

- **Your dictation history can now be deleted and exported.** A new **Privacy**
  page in the dashboard exports every dictation as JSON (including the raw
  pre-cleanup transcript) and deletes all of it. `clear_history()` had existed and
  been tested for weeks, but was wired to nothing — so an app whose whole promise
  is privacy had no off switch. `DELETE /api/history` and
  `GET /api/history/export`, both behind the same-origin guard.
- **Text now lands in the window you started in.** The focused application is
  snapshotted on `fn`-down and re-activated before the paste, so clicking away
  mid-transcription no longer types your words into whatever happened to be in
  front. Uses macOS 14+ cooperative activation where available, and aborts before
  touching the clipboard if the target app went away.
- **`./simo rehearse`** — rehearses the real paste path with a deliberate pause so
  you can switch windows on purpose, then reports where the text landed and
  whether the clipboard survived. The wrong-window class of bug was previously
  impossible to reproduce on demand.
- **Liquid Glass recording pill.** Real `NSGlassEffectView` on macOS 26, detected
  at runtime with the previous vibrancy material as fallback. Contrast is
  guaranteed by an explicit scrim rather than by the material: measured over pure
  white, pure black and a mid-tone desktop, `tintColor` turned out to be a subtle
  hue wash, not an opacity overlay, and left white content nearly invisible on a
  light background — a flaw the old material shared. `tools/pill_preview.py`
  renders all three cases so it stays measured rather than assumed.
- **Honest pipeline states.** The pill shows `Transcribing` then `Polishing`
  instead of one anonymous spinner, so a slow run is distinguishable from a hung
  one.
- **Property-based tests for the dictation-integrity guard**
  (`tests/test_polish_properties.py`). Hypothesis attacks the deletion-only
  contract from four directions rather than relying on hand-picked examples —
  which is what let a meaning-inverting bug ship in 2.0.1. Each property was
  verified to bite by disabling the corresponding check and confirming exactly
  one failure.
- **`AGENTS.md`** — the invariants that aren't inferable from reading the code, so
  a future session doesn't "improve" the integrity guard, convert the deliberate
  `print()` calls to `logging`, or move the repo and silently break the macOS
  permission grants.

### Fixed

- **Pasting could silently fail in Electron apps** (Slack, VS Code, Discord,
  Notion). Only the `V` key events were posted, with the Command flag set;
  Chromium tracks modifier state from the Command key's own `flagsChanged` event
  and dropped the paste without it. All four events are now sent.
- **A `Cmd+C` during transcription could be overwritten.** The previous clipboard
  was restored unconditionally after the paste, so anything copied inside that
  window was destroyed. The pasteboard `changeCount` is now compared before
  restoring, and a third-party write wins.
- **Revoked Accessibility permission failed invisibly, forever.** macOS discards
  synthetic events from an untrusted process without error, so after an OS update
  the app kept recording, transcribing and pasting nothing, with no signal
  anywhere. Trust is now checked before any clipboard write, with an actionable
  message and a visible pill state.
- **A failing polish pass was completely silent.** `except Exception: return
  raw_text` had no logging, so a stopped Ollama or an evicted model degraded every
  dictation to raw output indefinitely with nothing to find in the log.
- **A very fast double-tap could confuse the discard timer.** `_pending_discard`
  was written from both the main runloop and the timer's own thread; cancel and
  fire are now serialized.
- Three dead imports and a combined-import line, found by wiring lint into CI.

### Changed

- **Fixed sleeps in the paste path replaced with condition polling.** Waiting a
  guessed 50ms for the pasteboard, and a guessed interval for window activation,
  was simultaneously too slow in the common case and too short in the bad one.
  `_wait_until()` continues the instant the real condition holds, so an
  already-correct focus costs nothing and a cold app still gets time to come
  forward. Measured end-to-end paste: 403ms → 355ms, and the pre-keystroke
  portion is now effectively zero.
- **CI actually lints and runs every hermetic test.** It previously ran
  `test_units.py` alone, so the other suites could have rotted unnoticed, and the
  job was named "Lint & test" without linting. Adds `ruff` on bug-class rules
  (config in a new `pyproject.toml`, line length 100 to match the existing code)
  and a `gitleaks` secret scan over full history.
- `Recorder.is_recording` replaces external reads of the private `_recording`
  attribute.

### Notes

- Repairing the venv's script shebangs may be needed if the repository was moved
  at any point (symptom: `./.venv/bin/pip: bad interpreter`). Rewrite the first
  line of the files in `.venv/bin/` rather than recreating the venv — recreating
  replaces the Python binary and macOS revokes the granted permissions.

## [2.0.2] — 2026-07-27

### Fixed

- **A dictated question could still be pasted as its answer.** The 2.0.1 guard
  rejected cleanup output that was much longer than the transcript or full of
  unspoken words, which caught obvious answers ("what is the capital of France"
  → "Paris") but leaked two shapes: an answer that reorders your own words ("…is
  the capital of France" → "The capital of France is Paris"), and an answer that
  collapses the utterance to a single word you did say ("how do you spell
  restaurant" → "Restaurant"). Both are now impossible to paste.

  The guard was rebuilt on the exact contract instead of a heuristic: cleanup is
  deletion-only, so a valid output must be an ordered **subsequence** of the
  spoken words (no reordering, substituting, or appending) **and** must retain
  most of the non-filler content words (no collapsing to a keyword). Anything
  else falls back to your raw transcript — the model can never reach the cursor
  with words you didn't speak. Six regression tests pin every answer shape
  observed from the model; verified end-to-end against the live cleanup model.

## [2.0.1] — 2026-07-24

### Fixed

- **Cleanup could answer a dictated question instead of transcribing it.** A
  general chat model, fed a question, would sometimes "helpfully" answer it, and
  the answer got pasted as if you'd said it. Fixed structurally: cleanup output
  is now rejected and replaced with your raw transcript whenever it isn't a
  plausible filler-removed version of what you said (much longer, or full of
  words you didn't speak). A question few-shot also teaches the model the
  boundary. Covered by a regression test.

## [2.0.0] — 2026-07-24

The reliability, accuracy, security, and design rebuild. Simo Flow went from a
one-day proof of concept to something built to a production bar.

### Added

- **Two model tiers**, switchable live from the menu bar: **Accurate**
  (`large-v3-turbo`, default) and **Fast** (`base.en`), persisted in settings.
- **On-demand microphone**: the mic stream now opens only while you dictate, so
  macOS's microphone indicator is off when the app is idle.
- **`./simo` control script + LaunchAgent**: `install`, `uninstall`, `open`,
  `start`, `stop`, `restart`, `status`, `log`. Starts at login; relaunches only
  on a crash (a menu Quit actually quits).
- **Unit test suite** (`tests/test_units.py`) and **GitHub Actions CI**.
- **Pinned `requirements.txt` / `requirements-dev.txt`** for reproducible setup.
- Governance docs: `SECURITY.md`, `CONTRIBUTING.md`, this changelog.

### Changed

- **Dashboard redesigned** to an Apple / iCloud aesthetic (SF Pro, `#f5f5f7`
  canvas, Apple-blue accents, light + dark themes).
- **Recording pill rebuilt** with real macOS vibrancy (`NSVisualEffectView`) and
  a live voice waveform; the menu now uses native submenus with checkmarks.
- **Polish prompt** keeps hedges ("basically", "I think") via a named list plus
  a few-shot example, instead of the 3B model editing them away.

### Fixed

- **The `[end of transcript]` failure**: a changed mic device fed silence that
  was transcribed and pasted. Root cause was missing health monitoring — now the
  mic opens fresh per utterance, silence is refused before transcription, junk
  transcripts are never pasted, and failures surface in the pill.
- **Clipboard race**: two quick dictations could paste the wrong text. The
  pipeline is now serialized through a single worker queue.
- **Cross-thread AppKit access**: menu/title writes are marshalled to the main
  thread.
- **Orphaned `whisper-server`** after a `SIGTERM` (logout / `./simo stop`) is now
  reaped on startup, and cleaned up on quit.
- **Model tier reset**: an auto-restart of whisper-server no longer silently
  reverts your chosen tier.
- **Feed ordering** is now deterministic when two dictations share a timestamp.
- **Stale dashboard cache**: served with `Cache-Control: no-store`.

### Security

- **DNS-rebinding**: `Host` header validated on all dashboard routes, closing a
  path that could read `/api/history` cross-origin.
- **CSRF**: `Origin` header checked on state-changing routes.
- **Local data**: `~/.simo-flow.db` and `~/.simo-flow.log` locked to `0600`.
- **Clipboard**: non-restorable previous contents are cleared rather than left
  holding dictated text.

## [1.0.0] — 2026-07-06

Initial public release. Fully local, offline voice dictation for macOS:
`whisper.cpp` (Metal) + a local 3B model via Ollama, push-to-talk and
double-tap-lock, a recording pill, and a local dashboard with history, insights,
and a personal dictionary. Built in a single day. MIT licensed.
