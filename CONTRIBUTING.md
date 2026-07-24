# Contributing to Simo Flow

Thanks for your interest. Simo Flow is a local-first macOS dictation tool, and
contributions that make it faster, more accurate, more private, or easier to run
are very welcome.

## Ground rules

- **Privacy is the point.** No contribution may send audio, transcripts, or any
  user data off the machine. The only network calls that exist are to
  `127.0.0.1` (whisper-server and Ollama). Keep it that way.
- **macOS + Apple Silicon** is the target platform.
- Keep changes focused and the diff readable.

## Dev setup

```bash
git clone https://github.com/lucassimonian/simo-flow && cd simo-flow
python3.11 -m venv .venv
./.venv/bin/pip install -r requirements-dev.txt   # runtime + test deps

# models (see README for the download commands)
brew install whisper-cpp ollama portaudio
brew services start ollama && ollama pull qwen2.5:3b-instruct
```

Run it in the foreground while developing:

```bash
./.venv/bin/python -m engine
```

## Tests

Two layers:

```bash
# fast, isolated unit tests (no mic / server / GUI needed — these run in CI)
./.venv/bin/python -m pytest tests/test_units.py -q

# each module also self-checks in isolation
./.venv/bin/python -m engine.stt
./.venv/bin/python -m engine.polish
./.venv/bin/python -m engine.store

# full end-to-end (needs mic, whisper, Ollama, and TextEdit)
./.venv/bin/python tests/test_pipeline.py
```

Please add or update unit tests for any logic change. CI runs the compile check
and `tests/test_units.py` on every push.

## Code map

| File | Responsibility |
|------|----------------|
| `engine/__main__.py` | menu-bar app, `fn` state machine, serialized pipeline worker |
| `engine/hotkey.py` | HID-level consuming `CGEventTap` |
| `engine/audio.py` | on-demand mic capture, silence trim + guard |
| `engine/stt.py` | whisper-server client, model tiers, restart/reaping |
| `engine/polish.py` | LLM cleanup with word-preservation constraints |
| `engine/inject.py` | clipboard set → `Cmd+V` → restore |
| `engine/store.py` | SQLite: history, dictionary, settings, insights |
| `engine/api.py` | FastAPI dashboard (`127.0.0.1`), Origin + Host guarded |
| `engine/static/` | the dashboard (single self-contained HTML file) |
| `engine/overlay.py` | the recording pill (`NSVisualEffectView` + waveform) |

## Submitting

1. Open an issue first for anything non-trivial, so we can agree on the approach.
2. Fork, branch, keep commits clean (conventional-commit style is appreciated).
3. Make sure `pytest tests/test_units.py` passes and the app still launches.
4. Open a PR describing what changed and why.

Security issues: please follow [SECURITY.md](SECURITY.md) — do not open a public
issue for a vulnerability.
