# Changelog

All notable changes to Simo Flow are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[Semantic Versioning](https://semver.org/).

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
