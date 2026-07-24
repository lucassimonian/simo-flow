# Changelog

All notable changes to Simo Flow are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[Semantic Versioning](https://semver.org/).

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
