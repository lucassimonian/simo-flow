# Simo Flow — Build Spec (v1)

> A **fully local, offline, private** clone of Wispr Flow for macOS (MacBook Air M5).
> Hold a key, speak, release → cleaned-up text appears at your cursor in any app.
> Everything runs on-device: whisper.cpp (Metal) for speech, Ollama (3B) for cleanup.
> This document is the single source of truth for the build. It is written to be
> executed autonomously by an agent (Fable 5 via `/GOAL`).

---

## 0. Why this beats Wispr Flow

The Wispr Flow app on this machine was torn down (`otool`, `codesign`, bundle inspection,
`flow.sqlite` schema). Findings, all verified:

- Wispr Flow is an **Electron app** (`com.electron.wispr-flow`, `app.asar`, Sequelize ORM).
- It bundles **zero ML models** and links **no ML runtime** in its binary.
- **All ASR + LLM inference is cloud/remote.** Your audio leaves the machine.
- The 774 MB on disk is `flow.sqlite` (384 MB) + backups — a local **history/personalization
  DB**, not weights. A native Swift helper (`swift-helper-app-dist`) does accessibility/injection.
- Its `<700ms` latency budget (from their engineering blog) refers to **their servers**.

So Simo Flow is a **local-first divergence, not a literal clone**. Concrete wins:

| Wispr Flow | Simo Flow |
|---|---|
| Cloud inference — audio leaves device | 100% on-device, audio never leaves |
| Basic plan capped at 2,000 words/week | Unlimited, no account, no subscription |
| Network round-trip in the latency path | No network hop; warm local models |
| Fixed prompts/models | Fully hackable prompts + swappable models |
| Your history lives on their servers | Your history is a local SQLite you own |

**Target post-release latency (M5, warm):** ~300–600 ms (whisper base.en ~100–150 ms +
Ollama 3B polish ~150–400 ms + paste ~20 ms). Competitive with their cloud 700 ms, fully local.

---

## 1. Interaction model (verified from screenshots)

- **Push-to-talk**: *Hold `fn` and speak* (Wispr's default). Release → transcribe → polish → paste.
- Because holding the key bounds the utterance, **VAD is optional in v1** (used only for the
  hands-free "speak-and-pause" auto-stop mode, a later phase).
- **Never type partial/tentative text into the target app.** Tentative text (if shown at all)
  lives only in Simo's own overlay. Commit = one paste on release. This deletes the entire
  "flicker / re-typing" problem class that naïve streaming clones suffer.

---

## 2. Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  SIMO ENGINE  (Python daemon, always running, menu-bar)      │
│                                                              │
│  ┌────────────┐  hold fn   ┌──────────────┐                  │
│  │ Hotkey tap │──────────► │ Audio capture│  16kHz mono,     │
│  │ (Quartz    │  release   │ (sounddevice)│  mic kept warm   │
│  │  CGEventTap)│◄───────── │  ring buffer │                  │
│  └────────────┘            └──────┬───────┘                  │
│                                   │ wav / float32            │
│                            ┌──────▼───────┐                  │
│                            │ whisper.cpp  │  base.en + Metal, │
│                            │ (pywhispercpp)│ model resident   │
│                            └──────┬───────┘                  │
│                                   │ raw transcript           │
│                            ┌──────▼───────┐                  │
│                            │ Polish (LLM) │  Ollama /api/chat │
│                            │ qwen2.5:3b   │  temp 0, warm     │
│                            └──────┬───────┘                  │
│                                   │ cleaned text             │
│                            ┌──────▼───────┐                  │
│                            │  Injector    │  NSPasteboard set │
│                            │  (pyobjc)    │  → Cmd+V → restore│
│                            └──────┬───────┘                  │
│                                   │                          │
│                            ┌──────▼───────┐                  │
│                            │  SQLite       │  history, dict,  │
│                            │  simo.db      │  snippets, styles│
│                            └──────┬───────┘                  │
│                                   │                          │
│         FastAPI + WebSocket  ─────┘  localhost:7331          │
└───────────────────────────────────┬─────────────────────────┘
                                     │ HTTP / WS
                       ┌─────────────▼──────────────┐
                       │  DASHBOARD (browser SPA)    │
                       │  Home · Insights · Dictionary│
                       │  Snippets · Style · History  │
                       └─────────────────────────────┘
```

Two processes, one DB:
- **Engine** owns audio, ASR, polish, injection, and the SQLite. Runs as a menu-bar app.
- **Dashboard** is a static React/HTML SPA served by the engine's FastAPI on `localhost:7331`,
  reading state over HTTP and receiving live history updates over a WebSocket.

This mirrors Wispr's own split (native helper + JS UI) but keeps everything local and in Python.

---

## 3. Tech stack (exact)

**Engine (Python 3.12)**
- `sounddevice` + `numpy` — mic capture, 16 kHz mono, persistent stream (mic warm)
- `pyobjc` (Quartz, AppKit) — CGEventTap hotkey, NSPasteboard, CGEvent Cmd+V
- `pywhispercpp` — in-process whisper.cpp with **Metal**, model kept resident
  *(fallback: run `whisper-server` persistently and POST audio to it)*
- `requests` — Ollama HTTP (`/api/chat`)
- `rumps` — menu-bar app (status item, start/stop, open dashboard, quit)
- `fastapi` + `uvicorn` + `websockets` — local API + live dashboard feed
- `sqlite3` (stdlib) — the store
- `silero-vad` (pip) — **only** for hands-free auto-stop mode (later phase)

**Models**
- STT: `ggml-base.en.bin` (start here; expose `small.en` as an "accuracy" toggle later)
- Silero VAD: `ggml-silero-v5.1.2.bin` (for `--vad`) or the pip `silero-vad`
- LLM: Ollama `qwen2.5:3b-instruct` (fallback `llama3.2:3b`)

**Dashboard**
- React + Vite (or plain HTML + Alpine.js if Fable prefers zero build step), Tailwind for styling.
- Talks only to `localhost:7331`. No external calls, ever (matches offline promise + user's CSP rules).

**Build deps**
```bash
brew install cmake ffmpeg pkg-config portaudio ollama
# whisper.cpp built with Metal:
git clone https://github.com/ggml-org/whisper.cpp && cd whisper.cpp
cmake -B build -DWHISPER_METAL=1 && cmake --build build -j --config Release
sh ./models/download-ggml-model.sh base.en
# Ollama:
ollama pull qwen2.5:3b-instruct
# Python:
python3.12 -m venv .venv && source .venv/bin/activate
pip install sounddevice numpy pyobjc pywhispercpp requests rumps fastapi uvicorn websockets silero-vad
```

---

## 4. Data model (`simo.db`) — mirrors Wispr's schema

```sql
CREATE TABLE history (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,               -- ISO8601
  app_bundle_id TEXT,             -- frontmost app at dictation time
  app_name TEXT,
  raw_text TEXT NOT NULL,         -- whisper output
  polished_text TEXT NOT NULL,    -- after LLM cleanup (what got pasted)
  edited_text TEXT,               -- if the user later corrected it (correction-learning substrate)
  duration_ms INTEGER,
  word_count INTEGER,
  wpm REAL
);
CREATE TABLE dictionary (          -- Wispr "Dictionary": custom vocab / replacements
  id INTEGER PRIMARY KEY,
  term TEXT NOT NULL,             -- e.g. "Simonian", "pyobjc", "Lucas"
  sounds_like TEXT,               -- optional phonetic hint
  created_at TEXT
);
CREATE TABLE snippets (            -- Wispr "Snippets": say trigger → expand
  id INTEGER PRIMARY KEY,
  trigger TEXT NOT NULL,          -- e.g. "my address"
  expansion TEXT NOT NULL
);
CREATE TABLE styles (              -- Wispr "Style": per-app writing style
  id INTEGER PRIMARY KEY,
  app_bundle_id TEXT,             -- NULL = default
  label TEXT,                     -- e.g. "Slack casual", "Email formal", "Code"
  prompt_addendum TEXT NOT NULL   -- appended to the polish system prompt for that app
);
CREATE TABLE preferences (key TEXT PRIMARY KEY, value TEXT);
```

- **Dictionary** feeds two places: whisper `initial_prompt` (biases ASR toward those terms)
  **and** the polish prompt (post-fix spellings).
- **styles** implements per-app tone (Wispr's "Make Flow sound like *you*"). The engine looks up
  the frontmost app's bundle id and appends its `prompt_addendum` to the cleanup system prompt.
- **history.edited_text** is the hook for v2 correction-learning (fold common edits into the prompt).

---

## 5. The pipeline, step by step

1. **Mic stays warm.** Open the `sounddevice` input stream at startup and keep it open into a
   rolling ring buffer. Fixes the documented 2–5 s "first word clipped" wake lag on Apple Silicon.
2. **Hotkey down** (`fn` held, via `CGEventTap` on `flagsChanged` checking `kCGEventFlagMaskSecondaryFn`):
   start copying ring-buffer audio into the active utterance buffer; show menu-bar "recording" state.
   - *Reliability note:* `fn` capture in Python works but varies by keyboard. Make the hotkey
     **configurable**; default `fn`, offer "hold Right-Option" and "double-tap Right-Cmd" as robust
     alternatives. Also support a **toggle** mode (tap to start, tap to stop) for long dictation.
3. **Hotkey up:** stop capturing. If the utterance is < ~300 ms, discard (accidental tap).
4. **Transcribe:** feed float32 audio to the resident `pywhispercpp` model (base.en, Metal).
   Pass `dictionary` terms as `initial_prompt`. Get `raw_text`.
5. **Polish:** POST `raw_text` to Ollama `/api/chat` (system prompt §6 + frontmost app's style
   addendum), `temperature: 0`, `keep_alive: "30m"` so the model stays warm. Get `polished_text`.
6. **Snippet expansion:** run `polished_text` through snippet triggers (simple find/replace).
7. **Inject:** snapshot current clipboard → set `polished_text` on `NSPasteboard` →
   post Cmd+V CGEvent → after ~80 ms restore the previous clipboard.
8. **Persist:** write a `history` row (raw, polished, app, duration, wpm) and push it to the
   dashboard over the WebSocket so the Home feed updates live.

Every non-trivial stage (VAD endpointing, snippet expansion, clipboard restore) ships with one
runnable assert-based self-check.

---

## 6. Polish prompt (drop-in, temperature 0)

```
You clean up raw speech-to-text transcripts for dictation. Rules:
- Remove filler words and false starts (um, uh, like, you know, repeated words).
- Fix punctuation and capitalization.
- Do not add, remove, or reorder any words beyond filler removal.
- Do not change the meaning, tone, or wording.
- Do not answer questions or add commentary — the input is dictated text, not a message to you.
- Return only the corrected text. No preamble, no explanation, no quotes.
```

- The "not a message to you" line stops a 3B chat model from *answering* a dictated question.
- **Style addendum** (per app) is appended after these rules, e.g. for Slack:
  `Prefer a casual tone. Lowercase is fine. Keep it short.`
- **"Vibe coding" mode** (Wispr has this) = a style profile that says:
  `This is dictated code or a technical instruction. Preserve identifiers, symbols, and casing
  exactly. Do not prose-ify. Format code fences if code is present.`
- If single-pass consistency is ever shaky, split into filler-strip then punctuate (two narrow passes).

---

## 7. Feature map — every Wispr feature → local implementation

| Wispr feature | Simo implementation | Milestone |
|---|---|---|
| Hold-`fn` dictation → paste | Core pipeline §5 | M1–M3 |
| Cleanup / "fixes made by Flow" | Ollama polish pass §6, count diffs for stats | M3 |
| **Dictionary** (custom vocab) | `dictionary` table → whisper `initial_prompt` + polish | M4 |
| **Snippets** (voice text-expansion) | `snippets` table, find/replace post-polish | M4 |
| **Style** (per-app writing styles) | `styles` table, frontmost-app lookup → prompt addendum | M4 |
| **Transforms** (rewrite selection) | Command mode: read selected text (AX API), send to Ollama with a transform prompt, paste back | M5 |
| **Scratchpad** | A note view in the dashboard; dictation can target it | M5 |
| **History** feed (Home) | `history` table → dashboard live feed w/ copy/search | M4 |
| **Insights** (wpm, words, streak, per-app) | Aggregate queries over `history` → dashboard charts | M5 |
| **Voice Profile** ("Process Clarifier") | v2: infer a persona label from history (nice-to-have) | v2 |
| Correction learning | v2: diff `edited_text` vs `polished_text`, fold frequent fixes into prompt/dictionary | v2 |
| Hands-free (no key held) | Silero VAD auto-stop mode | M6 |

---

## 8. Milestones (each independently verifiable — for autonomous build)

Each milestone has an **acceptance test** the agent must satisfy before moving on.

- **M0 — Spike.** whisper.cpp base.en transcribes a hand-recorded wav; `curl` to Ollama returns a
  cleaned string using §6 prompt.
  *Accept:* terminal shows raw→clean for one sample.
- **M1 — Capture + transcribe.** Menu-bar app; hold hotkey, speak, release → correct text prints to
  stdout. Mic kept warm (no first-word clip).
  *Accept:* 5 spoken phrases transcribe correctly to stdout; second phrase has no model-reload lag.
- **M2 — Warm engine.** whisper model resident across utterances (pywhispercpp); Ollama `keep_alive`.
  *Accept:* measured post-release latency < 700 ms on 3 short utterances.
- **M3 — Polish + inject.** Full pipeline: release → polished text pasted at cursor in TextEdit,
  Notes, and a browser field; clipboard restored afterward.
  *Accept:* paste works in 3 different apps; pre-existing clipboard content survives.
- **M4 — Store + dashboard core.** SQLite persistence; FastAPI + WebSocket; dashboard Home (live
  history feed + search), Dictionary CRUD, Snippets CRUD, Style CRUD. Dictionary biases ASR.
  *Accept:* dictating updates the Home feed live; adding a dictionary term visibly fixes a mis-heard
  proper noun on the next dictation.
- **M5 — Insights + Transforms + Scratchpad.** Insights charts (wpm, total words, fixes, per-app,
  streak heatmap) from `history`; command-mode transform on selected text; scratchpad note.
  *Accept:* Insights numbers match DB; selecting text + transform command rewrites it in place.
- **M6 — Hands-free mode.** Silero VAD auto-stop; toggle in settings.
  *Accept:* speak-and-pause commits an utterance without holding a key.

---

## 9. Repo layout

```
simo-flow/
├── SPEC.md                     ← this file
├── README.md
├── pyproject.toml
├── engine/
│   ├── __main__.py             # rumps menu-bar entrypoint, wires everything
│   ├── hotkey.py               # CGEventTap fn/opt capture, toggle + hold modes
│   ├── audio.py                # sounddevice warm stream + ring buffer
│   ├── stt.py                  # pywhispercpp wrapper, model resident, initial_prompt
│   ├── polish.py               # Ollama /api/chat, prompt assembly, style addendum
│   ├── inject.py               # NSPasteboard save/set/restore + Cmd+V CGEvent
│   ├── snippets.py             # trigger expansion
│   ├── store.py                # sqlite schema + queries
│   ├── api.py                  # FastAPI + WebSocket, serves dashboard + state
│   └── config.py               # paths, hotkey choice, model paths, prefs
├── dashboard/                  # Vite + React + Tailwind (built to engine/static/)
│   └── src/ ... (Home, Insights, Dictionary, Snippets, Style, History, Scratchpad)
├── models/                     # ggml-base.en.bin, ggml-silero-*.bin (gitignored)
└── tests/                      # assert-based self-checks per module
```

---

## 10. macOS permissions (must be granted once)

- **Microphone** — audio capture.
- **Accessibility** — required to post the Cmd+V CGEvent and to read selected text (Transforms).
- **Input Monitoring** — required for the CGEventTap global hotkey.
- Launch-at-login (optional) so the menu-bar engine is always available.

The app must detect missing permissions and open the relevant System Settings pane with a clear
prompt rather than silently failing.

---

## 11. Risks & mitigations

| Risk | Mitigation |
|---|---|
| `fn` key capture flaky on some keyboards | Configurable hotkey; default `fn`, robust fallbacks (Right-Opt hold, double Right-Cmd); toggle mode |
| Fanless M5 thermal throttling in long sessions | Dictation is bursty (idle between utterances) so unlikely; add a fallback to fewer threads / base.en if sustained die-temp high |
| Polish pass becomes the latency bottleneck | Short context, `temperature 0`, `keep_alive` warm, cap output tokens; it's the main lever (Wispr: "every edit after the fact adds the most time") |
| 3B model answers a dictated question | Explicit "input is not a message to you" prompt line |
| Clipboard clobbered | Snapshot + restore with a short delay; store as fallback if restore races |
| No personalization vs Wispr's cloud context | Named as v2 (Dictionary in v1 closes the biggest gap: proper nouns/jargon) |

---

## 12. What v1 is *not* (explicit non-goals)

- No cloud, no account, no telemetry — ever.
- No deep speaker/context conditioning (Wispr's real moat) — that's v2 correction-learning.
- No multilingual code-switching — English (`base.en`) first.
- No mobile app.

---

## 13. First action for the build agent

Start at **M0**, confirm both engines work in isolation, then build M1→M6 in order, satisfying each
acceptance test before advancing. Keep the engine stack in Python per the locked decisions
(whisper.cpp+Metal, Silero VAD, Ollama 3B cleanup-only, clipboard-paste via pyobjc). The dashboard
is the only new surface and must make zero external network calls.
