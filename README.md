# Simo Flow

**Fully local, offline voice dictation for macOS.** Hold `fn`, speak, release — clean text appears at your cursor in any app. No cloud, no subscription, no word caps. Your audio never leaves your machine.

Built as a local-first answer to [Wispr Flow](https://wisprflow.ai) after tearing down its app bundle and discovering it runs **zero** on-device inference — every word you dictate is sent to their servers. Simo Flow does the same job entirely on Apple Silicon.

```
hold fn ──► mic (16kHz, always warm) ──► whisper.cpp base.en + Metal   (~100ms)
                                              │ raw transcript
                                              ▼
                                      Ollama qwen2.5:3b, temp 0        (~500ms)
                                              │ cleaned text
                                              ▼
                                      clipboard paste + restore        (~20ms)
```

**~600ms from silence to pasted text** — competitive with Wispr's own published 700ms cloud budget, with no network hop and no account.

## Features

- 🎙 **Push-to-talk**: hold `fn`, speak, release
- 🔒 **Hands-free lock**: double-tap `fn` to keep recording, tap once to finish
- ⚫ **Floating pill**: minimal recording indicator with live voice meter, click ✕ to cancel or ✓ to commit — without stealing focus from the app you're dictating into
- ✨ **Clean mode**: removes *um*s and false starts, fixes punctuation — keeps your hedges and wording (the LLM is explicitly forbidden from substituting words)
- 🎯 **Exact mode**: verbatim whisper output, no LLM pass, ~500ms faster
- 📊 **Dashboard** (`localhost:7331`): history feed with search, WPM / streak / activity-heatmap insights, and a **Dictionary** that biases the speech model toward your names and jargon
- 🗄 Everything stored in a local SQLite you own (`~/.simo-flow.db`)

## Install

```bash
brew install whisper-cpp ollama portaudio ffmpeg
brew services start ollama
ollama pull qwen2.5:3b-instruct

git clone https://github.com/lucassimonian/simo-flow && cd simo-flow
python3.11 -m venv .venv
./.venv/bin/pip install sounddevice numpy pyobjc requests rumps fastapi uvicorn pydantic

# whisper model (~142MB)
curl -L -o models/ggml-base.en.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin

./.venv/bin/python -m engine
```

First run: grant **Microphone**, **Accessibility**, and **Input Monitoring** to your terminal when prompted, and set System Settings → Keyboard → **"Press 🌐 key to" → "Do Nothing"** so macOS doesn't fight over the key (Wispr Flow requires the same).

## Engineering notes

Things that turned out to matter, in the order they bit me:

1. **Don't stream — batch.** Continuous transcription with live-correcting text is where naive Whisper dictation clones die (flicker, re-typing, repetition artifacts). Push-to-talk bounds the utterance; one paste on release deletes the whole problem class. Every serious open-source clone converges on this.
2. **Keep everything warm.** whisper-server holds the model resident (96ms transcription vs ~11s cold Metal-shader compile on first load). Ollama `keep_alive` does the same for the LLM. The mic stream never closes — macOS mic wake-up otherwise clips your first word.
3. **3B models over-edit.** `qwen2.5:3b` treats hedges ("basically", "I think") as fillers, and if you list example hedge words in the prompt it starts *substituting* them. The fix: "never substitute — every kept word must appear verbatim in the input", plus an Exact mode that skips the LLM entirely.
4. **The `fn` key is contested territory.** macOS binds it to the emoji picker, Apple Dictation wants it, and a stale Character Palette window will silently swallow your pasted text via key focus. A HID-level consuming event tap + one system setting resolves it.
5. **Clipboard paste beats synthetic typing.** One `Cmd+V` CGEvent is atomic and works in every app; per-character typing is slow and drops characters. Snapshot and restore the user's clipboard around it.
6. **`pkill -f "python ..."` doesn't match a venv GUI process on macOS** — the venv execs the framework `Python.app` binary (capital P). Three engine instances stacked up during development, each pasting, before a singleton flock fixed it for good.

## Layout

```
engine/__main__.py   menu-bar app, fn state machine (hold / double-tap / stray-tap), pipeline
engine/hotkey.py     HID-level consuming CGEventTap
engine/audio.py      warm mic stream, pre-roll ring buffer, silence trim
engine/stt.py        whisper-server client, dictionary prompt, repetition dedupe
engine/polish.py     LLM cleanup with word-preservation constraints
engine/inject.py     clipboard set → Cmd+V → restore
engine/store.py      SQLite: history, dictionary, insights
engine/api.py        FastAPI on 127.0.0.1:7331 (dashboard; zero external calls)
engine/static/       the dashboard (single self-contained HTML file)
engine/overlay.py    the recording pill (NSPanel, non-activating)
tests/               assert-based self-checks + full E2E (synthesizes speech with `say`,
                     plays it through the speakers, posts synthetic fn events, and
                     reads the pasted result back out of TextEdit)
```

Every module runs standalone as its own self-check: `./.venv/bin/python -m engine.stt` etc.

## Requirements

Apple Silicon Mac, macOS 14+, ~2.5GB disk (whisper model + 3B LLM). Built and benchmarked on a MacBook Air M5.

## License

MIT
