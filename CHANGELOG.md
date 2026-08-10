# Changelog

All notable changes to Simo Flow are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[Semantic Versioning](https://semver.org/).

## [2.2.1] — 2026-08-10

### Fixed

- **Dictation stopped working entirely after AirPods connected, and stayed broken
  until the app was restarted by hand.** Twelve consecutive
  `Internal PortAudio error [PaErrorCode -9986]` in the log, with CoreAudio
  reporting `err='-10851'`. PortAudio caches its device list at initialisation, so
  a microphone appearing or disappearing mid-session left the daemon asking for a
  device that had moved — in a fresh process every sample rate opened fine, which
  is what made it look like a hardware fault.

  The failure was terminal by construction: `begin()` caught the exception and
  gave up. `_needs_reinit` already existed and would have fixed it, but was only
  ever set after a *silent capture*, never after a *failed open*. A failed open now
  re-initialises PortAudio and retries once, and sets `_needs_reinit` if that fails
  too, so the next press starts from a clean list rather than repeating a
  known-broken one.

  Not a regression: the only change v2.1.0–v2.2.0 made to `audio.py` was adding a
  read-only `is_recording` property.

- **The Command key could be left latched after every dictation.** The four-event
  paste added in v2.1.0 sent the Cmd key-*up* with the Command flag still set,
  which is self-contradictory — "Command released, Command still active" — and
  macOS latched it. `CGEventSourceFlagsState` reported Command as held after every
  paste, so the next keystroke was liable to be read as a shortcut and the keyboard
  would appear broken. Real hardware clears the flag on release; now so does this.
  Asserted in `tests/test_inject.py` and verified against the live modifier state.

- **Switching microphone mid-session is now a non-event, not a recovered failure.**
  Recovering after an error still means one press behaves oddly, which is not good
  enough for a device you connect several times a day. The current default input
  device is now read straight from CoreAudio — the one source PortAudio's cache
  cannot make stale — and the list is refreshed *before* asking for a device that
  moved. One property read, so it costs nothing on the common path where nothing
  has changed. Connect or disconnect AirPods and the next press simply works.

- **A mic that never opened reported "no audio captured"** — which reads as *you
  didn't speak*, and sends you to check your microphone instead of the log. That
  case now says "microphone unavailable" and is tracked separately from genuinely
  hearing nothing.

## [2.2.0] — 2026-08-06

### Changed

- **Cleanup is skipped when there is nothing to clean.** Every dictation used to
  be sent to the local model, even when it had no fillers and no stutters.
  Measured over 40 real dictations, the model returned the transcript **completely
  unchanged 72% of the time** — a full second or more spent being handed back
  exactly what whisper already said, and several seconds on a long utterance,
  because generation cost scales with length.

  `polish.needs_cleanup()` now decides up front. The test is deliberately narrow:
  fillers, or a stuttered repeat. Those are the only things cleanup is permitted
  to remove — everything else the model might do is forbidden by the prompt and
  rejected by `_is_rewrite` anyway, so skipping cannot lose it. Doubled words that
  are ordinary English ("that that", "had had") are not treated as stutters, and
  hedges never trigger cleanup on their own, since sending an utterance to the
  model purely because it contains "kind of" is what let the model strip a hedge
  it was explicitly told to keep.

  **Measured effect on real data: 50% of dictations skip the model, median
  1,627ms → ~430ms.** The rest are unchanged. The pill no longer announces a
  "Polishing" stage that isn't going to run.

  Knob: widen `_FILLERS` to send more utterances for cleanup, narrow it to keep
  more of them instant.

## [2.1.1] — 2026-08-06

### Fixed

- **Dictation pasted stray line breaks into the middle of your sentences.**
  whisper.cpp does not return a plain string of speech — it returns the
  transcript wrapped into fixed-width segments, so a long utterance arrived
  broken by a newline roughly every 57 characters, mid-sentence, with the
  continuation starting with a space. That was pasted verbatim. Measured against
  real logged transcripts: **86% of them contained newlines.** Whitespace is now
  flattened in `stt.transcribe()`, so the flat form is what history stores, what
  exact mode pastes, and what the polish pass is asked to clean. The polish output
  is flattened too, since a 3B model asked to fix punctuation will occasionally
  re-wrap the text itself.

- **Every log line was written twice, into a file nothing could rotate.** The
  LaunchAgent redirected stdout and stderr to `~/.simo-flow.log`, which is the
  same file the app tees into — so each line landed once from launchd and once
  from the app. Because launchd held the file open, it also could not be rotated.
  The result was 20MB of duplicated plaintext transcripts and growing. launchd now
  writes to a separate `~/.simo-flow.boot.log`, which exists only to catch
  failures that happen before the app can log for itself (a broken venv, a missing
  interpreter). The app owns its own log and trims it to the most recent entries
  once it passes 8MB, truncating in place rather than renaming so any other holder
  of the file keeps writing to the right inode. An install predating this change
  is detected at startup and reported rather than silently doubling output.

  **This one needs `./simo install` to take effect** — it changes the LaunchAgent.
  Permissions are unaffected: the interpreter path doesn't change.

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

### Fixed (from adversarial review of this release)

Two differently-primed review agents attacked the branch before merge — one for
security, one for correctness and concurrency. The security pass found no CRITICAL
or HIGH; the correctness pass returned BLOCK on a real race. Every finding was
verified independently before acting on it, and one was rejected as incorrect.

- **A deliberate commit could be silently discarded.** Three paths end an
  utterance — the discard timer, ✕, and ✓/release — and they coordinated via
  `Timer.cancel()`, which does nothing once the timer body has begun running and
  returns as though it had worked. Pressing ✓ at the moment the timer fired had
  both threads call `recorder.end()`; the timer took the audio and the user's
  "finish and paste" came back as "no audio captured". Consumers now agree through
  a flag held under the existing lock.
- **A failed paste destroyed the transcription.** Found while tracing the above:
  the pipeline returned before writing to history, so a refused paste meant the
  words were gone entirely. The dictation is now saved regardless, and the pill
  says "saved to dashboard".
- **A pid is not an identity.** `bundle_id` was captured on `fn`-down and never
  read. Since macOS reuses pids and up to two seconds pass before the paste, a
  quit-and-reissued pid could have activated an unrelated process and pasted into
  it. Both fields are now compared — which also fixes the everyday case of a
  transient system helper being frontmost at the instant of capture.
- **The origin guard failed open.** It only rejected an `Origin` that was present
  and unlisted, so a client omitting the header inherited full write access — and
  for these endpoints that check is the only protection there is. It now falls back
  to `Referer` and refuses a request carrying neither. That fallback is
  load-bearing rather than defensive: `fetch()` omits `Origin` on same-origin GETs,
  so the dashboard's own export request arrives with only a `Referer`.
- **`/api/history/export` is now origin-guarded too.** The response was already
  unreadable cross-origin, but an unbounded full-table read of every transcript is
  worth denying as a *trigger* as well.
- Added `test_no_cors_headers_are_ever_sent`: the unreadability of every read
  endpoint rests entirely on the absence of `Access-Control-Allow-Origin`, and
  nothing enforced that.
- Closed a test gap: every activation test flipped focus synchronously, so the
  polling loop's body had never actually executed.
- Removed a condition-poll before `Cmd+V` that was provably true on its first
  check (`NSPasteboard` writes are synchronous and in-process). It replaced a 50ms
  sleep that was itself cargo; a no-op with a confident comment is worse than
  neither.

- **The claim fix was itself re-reviewed, and had made one interleaving worse.**
  If the discard timer fires in the gap between the second tap of a double-tap
  going down and coming up, it used to win the claim and delete the audio; the
  release then engaged the hands-free lock over nothing, so the user saw
  "Recording — tap fn to stop", spoke, and got complete silence — the claim was
  already spent, so even the error flash was skipped. The timer now declines to
  claim while `fn` is physically held, checked inside the same lock as the claim.
  The reviewer's proposed fix (synchronising the `_locked` read) would not have
  worked: at that instant `_locked` is legitimately still False. A lost claim also
  restores the menu-bar title and status text, which is what made this invisible.

One review finding was **rejected**: the suggestion to wrap the export query in
`run_in_threadpool` to avoid blocking the event loop. Every route handler here is
a sync `def`, which FastAPI already dispatches to a threadpool — verified before
changing anything.

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
