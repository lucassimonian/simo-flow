# AGENTS.md

Context for AI agents working on Simo Flow. `README.md` explains what the project
*is* and how a human installs it — read that first. This file covers what will
break if you change it carelessly.

Simo Flow is a local, offline dictation app for macOS: hold `fn`, speak, release,
and the text is pasted at your cursor. Menu-bar app (rumps) + whisper.cpp +
Ollama, all on-device. ~2,000 lines of Python. macOS only, by design.

## Commands

```bash
./.venv/bin/python -m engine                       # run in the foreground
./simo restart                                     # restart the installed LaunchAgent
./simo log                                         # tail ~/.simo-flow.log
./simo rehearse [seconds] [text]                   # rehearse the paste path (see below)

./.venv/bin/python -m pytest tests/ -q --ignore=tests/test_pipeline.py   # CI suite
./.venv/bin/python -m pytest tests/test_pipeline.py -q                   # needs mic + models + Ollama
./.venv/bin/python -m engine.stt                   # every module has its own self-check
```

`./simo rehearse` pauses so you can switch windows on purpose, then reports where
the paste landed and whether the clipboard survived. It is the only way to
reproduce the wrong-window class of bug on demand — use it after any change to
`engine/inject.py`.

## Hard invariants — do not "improve" these

1. **Never move or rename the repo directory.** macOS grants Input Monitoring and
   Accessibility to `<repo>/.venv/bin/python3.11` by path, the LaunchAgent plist
   hard-codes the repo path, and the venv contains absolute paths. Moving it
   silently breaks dictation. If a move is unavoidable: `./simo uninstall` → move →
   recreate the venv → `./simo install` → re-grant **both** permissions → `./simo restart`.

2. **`print()` is deliberate. Do not convert it to `logging`.** `_tee_logs()` in
   `engine/__main__.py` mirrors stdout/stderr to `~/.simo-flow.log` (mode `0600`)
   so failures survive a closed terminal and a launchd start with no terminal at all.

3. **`polish._is_rewrite` is load-bearing safety, not a heuristic.** It enforces
   that LLM cleanup is a *deletion-only* transformation of the transcript:
   ordered subsequence + every negator retained + most content words retained.
   It exists because a 3B chat model will answer a dictated question instead of
   cleaning it. Loosening any of the three checks reintroduces shipped bugs.
   Locked down by `tests/test_polish_properties.py` (Hypothesis) — if you change
   the guard, those properties must still pass.

4. **The mic opens per-utterance and closes immediately.** Do not make the stream
   persistent for latency. A held-open stream keeps macOS's orange
   microphone-in-use indicator lit for the app's whole lifetime, which for a
   privacy-first tool wrongly signals "always listening".

5. **Paste stays serialized through the single pipeline worker** in
   `engine/__main__.py`. The system clipboard is global mutable state; two
   concurrent pastes interleave and paste each other's text.

6. **`engine/inject.py` must never paste blind.** Three guards, all with
   regression tests in `tests/test_inject.py`: Accessibility trust checked before
   posting events; the focus snapshot taken at `fn`-down re-activated before
   pasting; the pasteboard `changeCount` compared before restoring, so a user's
   mid-flight `Cmd+C` is never overwritten. Every failure path must return `False`
   having left the clipboard untouched.

7. **Waits are condition polls, not fixed sleeps.** `_wait_until()` exists so the
   common case costs ~0 and the slow case still succeeds. Do not replace it with
   `time.sleep()`.

8. **The dashboard is one self-contained HTML file** (`engine/static/dashboard.html`).
   No build step, no framework, no bundler. It is served from a local FastAPI app
   and must keep working with the file opened straight from disk.

9. **`engine/api.py` binds `127.0.0.1` only.** The Host allow-list blocks DNS
   rebinding and `_reject_cross_origin` blocks cross-origin writes. These are the
   reason a browsing session cannot read your dictation history. Do not add
   permissive CORS, do not bind `0.0.0.0`, and keep every write plus
   `/api/history/export` behind `_reject_cross_origin`. Three specifics a security
   review flagged as easy to get wrong later:
   - **Never add `CORSMiddleware`.** Every read endpoint is unreadable to a
     malicious page *only* because no `Access-Control-Allow-Origin` header is ever
     sent. That absence is the entire defence, and
     `test_no_cors_headers_are_ever_sent` exists to keep it that way.
   - **The check on `DELETE` looks redundant and is not.** Browsers happen to
     block a cross-origin `DELETE` at preflight today, so the guard never fires
     under attack — but it is the last line if CORS is ever loosened. Where it
     genuinely earns its keep is `POST /api/dictionary`: a `text/plain` HTML form
     can dodge the JSON preflight, and `Request.json()` does not check
     `Content-Type`.
   - **`_reject_cross_origin` must fail closed.** It rejects a missing Origin
     *and* a missing Referer. `fetch()` omits Origin on same-origin GETs, so the
     Referer fallback is what keeps the dashboard's own export working — don't
     "simplify" it away.

10. **`~/.simo-flow.db` and `~/.simo-flow.log` hold the plaintext of everything
    ever dictated, at mode `0600`.** Never loosen those permissions, never write
    transcripts anywhere else, and never add a network call that carries them.
    The history must stay deletable and exportable from the dashboard's Privacy
    page — a store of everything you have said needs both an exit and an off switch.

## Conventions

- Comments explain **why**, never what. The interesting comments in this repo record
  a failure that was actually hit — keep that standard, and name the failure mode.
- Every module runs standalone as its own `__main__` self-check.
- Tests split by what they need: `tests/test_units.py`, `tests/test_inject.py`, and
  `tests/test_polish_properties.py` are hermetic and run in CI;
  `tests/test_pipeline.py` needs real hardware and does not.
- Bug fixes ship with a regression test written first, that failed before the fix.
- Type annotations on function signatures. `ruff format` for formatting.

## Known rough edges

- The venv's script shebangs break if the repo is ever moved (see invariant 1).
  Symptom: `./.venv/bin/pip: bad interpreter`. Repair the first line of the files
  in `.venv/bin/` rather than recreating the venv — recreating replaces the Python
  binary and macOS revokes the granted permissions.
- `engine/hotkey.py` installs a **consuming** CGEventTap on `fn`. It swallows the
  key so macOS's own globe-key action never fires. macOS disables slow taps; the
  re-enable path also resets the held state, or `fn` sticks down.

## Developer tooling must never type into the user's window

`engine/inject.py` posts real keyboard events and stages the real clipboard. Any
tool that measures or exercises it must call `tools/_safe_input.stub_input()`
first, which turns key posting into a counter and the clipboard into a dict.

This is not hypothetical: a latency benchmark once called `paste_text()` directly
to time the paste path and posted twelve real ⌘V keystrokes into the user's
terminal. It looked exactly like the app malfunctioning. Timing the paste does not
require delivering it.

The single exception is `tools/rehearse_paste.py`, whose entire purpose is to check
that a real paste lands in the right window, and which the user invokes knowingly
via `./simo rehearse`.

`tools/mutation_sweep.py` rewrites source files in place and restores them. It
refuses to run when a target file has uncommitted changes, so an interrupted run is
always recoverable with `git checkout -- <file>`, and it verifies the tree is clean
before reporting success. Keep both guards if you touch it.
