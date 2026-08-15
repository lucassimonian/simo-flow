# Simo Flow — the road to the best local dictation app on Mac

**Goal:** everything Wispr Flow does well, running entirely on your own machine,
with nothing sent anywhere — free, and good enough that thousands of people use it.

This is a programme, not a sprint. One concern per session, sequenced so each one
stands on the last. Every session has a *done when* that can be checked, not
argued about.

**Three sessions remain.** An earlier draft listed seven; that was everything
found rather than everything needed. Modes and per-context vocabulary were cut to
optional, and the backlog folded into the sessions it belongs to.

**The "thousands of people" goal is what makes packaging non-optional.** Installing
Simo Flow today means cloning a repo, building a Python environment, installing
whisper.cpp via Homebrew, downloading models, granting three permissions and
running a shell script. Almost nobody will — not because they can't, but because
they won't. A draggable `.app` is not the polish at the end of this programme; it
is the difference between five users and thousands.

---

## Where we actually are

| | |
|---|---|
| Version | v2.2.3 + PR #17 (freeze fix) |
| Tests | 87 passing |
| Guards | 31/31 proven tested by mutation sweep |
| CI | Green |
| Lines of engine code | ~1,900 across 8 modules |
| Status on your Mac | **Stopped.** LaunchAgent uninstalled. |

The code is better than its recent history suggests. `store.py` and `api.py` are
genuinely well built — parameterised queries throughout, an origin guard that
fails closed, a DNS-rebinding host guard, `0600` on every file holding speech.
The `polish.py` integrity guard, which refuses to paste anything that isn't a
deletion-only edit of what you actually said, is the most interesting code in the
repo and has no equivalent in any competitor.

What went wrong was concentrated in one place: **audio and the keyboard shared a
thread.** That is now fixed at the root.

---

## Why Wispr Flow doesn't freeze your Mac

Verified by inspecting the shipped binary (`/Applications/Wispr Flow.app`), not
assumed:

| | Simo Flow (before PR #17) | Wispr Flow |
|---|---|---|
| Keyboard | Consuming `CGEventTap` — sits *inside* the system input pipeline | `globalShortcut` (Electron). **Zero `CGEventTap` references** |
| Audio | Opened the mic **on the keyboard callback** | `getUserMedia` / `AudioWorklet` — Chromium's stack, **a separate process** |
| Device list | Managed CoreAudio directly, tore it down and rebuilt it | **Never touches CoreAudio directly** — zero references |

Wispr can't freeze your Mac because its microphone and its keyboard handling
aren't in the same process. An AirPods switch physically cannot reach the code
watching your keys.

**Important caveat:** Simo Flow can't simply copy this. Wispr uses
`globalShortcut`, which cannot *consume* the fn key — so it cannot suppress
macOS's own emoji picker. Simo Flow's consuming tap is a deliberate, better
choice, and it comes with the obligation that the callback never blocks. That
obligation is now enforced by tests.

---

## The competitive picture

Researched rather than assumed. Sources at the bottom.

**Wispr Flow's real weaknesses — all of them structural, none fixable by them
without changing what they are:**

- Cloud-only. No offline mode at all.
- Audio routed through third-party servers (OpenAI, Meta).
- Captures screenshots of your active window every few seconds for "context".
- Trustpilot 2.7 / 5.
- In August 2026 staff published word-frequency analyses of user dictations on
  LinkedIn. Under default settings, your words are in that corpus.

**But "local" alone is not a moat.** Superwhisper, MacWhisper, Voibe, Spokenly
and Vibe Transcribe all run locally already. The honest differentiators available
to Simo Flow are:

| Differentiator | Status |
|---|---|
| **Free and MIT-licensed** | Competitors charge $149–$249 lifetime |
| **The integrity guard** | Genuinely novel — nothing else refuses to paste a rewrite |
| **Consuming fn key** | Kills the emoji picker; `globalShortcut` apps can't |
| **Speed** | Not yet competitive — see Session 4 |
| **Modes / vocabulary** | Behind Superwhisper — see Session 5 |

---

## What Wispr Flow actually ships

Read out of the shipped bundle, not from marketing. This is the list to take from
and the list to refuse.

| Their feature | Evidence | Simo Flow | Call |
|---|---|---|---|
| **Voice commands** — say "press enter", "new line", "select all" and it acts instead of typing it | 305 / 30 / 58 references | ❌ | **Take.** Cheap, and the most-used thing they have |
| **Streaming / interim results** — text appears while you talk | `streaming`, `interim` | ❌ waits for key release | **Take.** This is why they feel instant |
| **Hands-free on its own hotkey** — "Dictate hands-free by pressing this hotkey to start and stop", plus a warning when it overlaps push-to-talk | `settings_hotkey_dialog_hands_free_*`, `ptt_hands_free_overlap` | double-tap only | **Take** — and it is *safer* than issue #12's proposal, see below |
| **Style / tone matching** per app | `styleDetection`, `toneMatch` | ❌ | Take later |
| **Multilingual** | 6,145 language references | English-first | Take later |
| **Screen + active-app context** | `screenContext`, `activeApp` | ❌ | **Refuse.** This is the screenshot behaviour behind their 2.7/5 rating. It is their liability, not their moat |
| Custom vocabulary | `vocabulary` | ✅ | Parity already |
| Refuses to reword what you said | — | ✅ **only Simo Flow** | Keep. Nothing else has it |
| Nothing leaves the machine | — | ✅ **only Simo Flow** | Keep. They structurally cannot match it |

**The method, not just the list:** take what is genuinely good, refuse what is
cheap, ship the good part done properly. Copying their context-awareness would
import the exact criticism they get.

## Session 1 — Kill the freeze ✅

**Shipped in PR #17.**

The v2.2.3 revert closed one of three routes to the blocking call. Two survived,
and the dangerous one needed no device change to fire: a silent capture (exactly
what AirPods connecting produces) set `_needs_reinit`, so the *next* fn press
would call `sd._terminate()` on the event thread.

Every device call now runs on a dedicated `simo-audio` thread. Seven tests hold
the invariant by wedging the device layer for 1.5s and asserting every hotkey
entry point returns in under 250ms.

**Done when:** ✅ tests green, ✅ 31/31 mutations caught, ✅ CI green,
⬜ **confirmed against real AirPods** — the one remaining step.

---

## Session 2 — The hotkey never dies silently

**Why this is next:** it's the remaining class of "the app is just broken and I
don't know why". Highest user-visible risk now that the freeze is gone.

Found during research, not yet in any issue:

1. **Secure Input.** When any password field has focus — 1Password, the login
   screen, `sudo` in a terminal — macOS blocks event taps entirely. fn stops
   working with no message. Simo Flow has no detection for this. Worse, apps are
   known to leave Secure Input stuck on after sleep/wake.
2. **Silent recovery.** The tap *does* correctly re-enable itself after a timeout
   disable (better than Ghostty, which has an open bug for exactly this) — but it
   logs nothing, so a recurring problem would be invisible.
3. **Code-signing disable race** — a documented failure mode for event taps.
4. **`_kill_port` SIGKILLs whatever is listening on :7332** without checking it's
   whisper-server. Unlikely to bite, but it's an unconditional kill of another
   process.

**Done when:** fn either works or tells you why, in every state — password field
focused, after sleep/wake, after permission changes. Each state has a test or a
documented manual check.

---

## Session 3 — Privacy at rest

**Why this matters most strategically:** it's the one place Simo Flow can be
categorically better than Wispr rather than incrementally better.

Right now on your Mac:

| File | Size | Contents |
|---|---|---|
| `~/.simo-flow.log` | 2.1 MB | Plaintext of everything dictated |
| `~/.simo-flow.db` | 159 KB | Same, structured |

Both are `0600`, so no other *account* can read them. But any process running as
you can, and they persist forever by default.

Options, cheapest first:

1. **Retention policy** — auto-delete history older than N days. Small, obvious,
   removes most of the exposure.
2. **Encrypt at rest** — SQLCipher with the key in the macOS Keychain. Real
   protection; adds a dependency and a migration path.
3. **Opt-out of history entirely** — a mode that never writes transcripts.

**Done when:** you can state in one sentence what Simo Flow retains and for how
long, and a test proves it.

---

## Session 4 — Latency: actually beat Wispr

Current measured: `base.en` ~85ms, `large-v3-turbo` ~520ms, plus the Ollama
polish pass.

Two structural wins available:

1. **Parakeet v2** runs up to 300× realtime on Apple Silicon — a different speed
   class from whisper.cpp.
2. **Streaming.** Today transcription starts only *after* you release fn. Feeding
   audio to the model while you speak is the single biggest perceived-latency win
   available, and is a large part of why Superwhisper feels near-instant.

Also: `is_up()` does an HTTP round trip before every transcription, and `store.py`
re-runs schema creation plus a migration check on every single query. Both are
small, both are free to fix.

**Done when:** end-to-end latency is measured honestly on real dictations (the
figures have been wrong before — a previous release's numbers were ~300ms
pessimistic) and beats the current baseline by a stated margin.

---

## Issue #12 — hands-free, reshaped by what Wispr actually does

The issue proposes: hold fn to talk, tap a second key mid-hold to lock recording
on. That would mean surgery on `_claim_utterance` — the logic deciding which of
three paths owns a recording, which has already produced one user-visible bug
where a deliberate commit was thrown away and reported as "no audio captured".

**Nobody ships that.** Wispr gives hands-free its own configurable hotkey: press
to start, press again to stop. They also warn when it overlaps the push-to-talk
key, which says they treat the interaction between the two modes as a hazard —
the same concern, reached independently.

A separate hotkey is a *new entry point*. It never touches the ownership state
machine, so it is both the industry answer and the lower-risk one.

**Plan:** hands-free on its own hotkey, double-tap kept for anyone used to it, and
the hold-then-lock framing closed as the wrong shape with the reasoning recorded.

## Optional — modes, and a dictionary that learns

Superwhisper's most-praised feature is **modes**: per-context prompts and
vocabularies — one for code, one for email, one for meeting notes. Simo Flow has
Clean and Exact only. Genuine feature parity, but a want rather than a need.

**Worth more than modes, and much cheaper: a dictionary that fills itself.**
Whisper transcribed "AirPods" as "iPods" — not because it misheard the sound, but
because both are plausible words for that sound. Adding "AirPods" to the custom
dictionary fixed it immediately, because the dictionary is fed to whisper as an
`initial_prompt` (`__main__.py:361`).

The upgrade is to stop making that manual: when a dictation is corrected shortly
after it lands, record the raw → corrected pair and offer to add it. Note this is
a *language* fix, not an acoustic one — teaching the model a new pronunciation
would mean adapting the acoustic model, which is a different and far larger
problem for no extra benefit here.

**Done when:** the same word is never mis-transcribed twice.

---

## Session 6 — Ship as a real Mac app

Today Simo Flow runs from a Python venv via a LaunchAgent. That is not an
Apple-standard product, and it's why permissions and PATH have caused so many
problems.

- Signed and notarised `.app` bundle
- Sparkle or equivalent for updates
- Permissions requested properly at first run, with real explanations

**Done when:** it installs by dragging to Applications, and a stranger can use it.

---

## Session 7 — The existing backlog

The 14 audit findings (source needed — not in the repo) and these 6 open issues:

| # | Issue |
|---|---|
| 14 | No coverage beyond one machine |
| 13 | README screenshots stale, show real dictation history |
| 12 | Hold-then-lock: hands-free mid-sentence |
| 11 | A copied image is destroyed by dictating |
| 10 | Layout-aware paste keycode (Dvorak, AZERTY) |
| 9 | Historical dictations still contain whisper's line wrapping |

**#11 belongs in Session 3** — destroying a copied image is a data-loss bug, not a
feature request.

---

## Your 20-point pre-launch checklist, mapped honestly

Most of that list targets hosted web apps with logins and databases. Simo Flow has
no accounts, no sessions, no uploads and no public surface, so much of it is
genuinely not applicable — saying otherwise would be theatre.

**Already done:**

| Item | Where |
|---|---|
| 2. Purge git secrets | gitleaks scans full history in CI |
| 13. Parameterise queries | Every query in `store.py` |
| 14. Validate input | Pydantic models on the API |
| 20. Scan dependencies | Partially — see gaps below |

**Real gaps, worth doing:**

| Item | Why it applies here |
|---|---|
| **5. Encrypt sensitive data** | The single most relevant item. Session 3. |
| **18. Security headers** | The dashboard sends none — no CSP, no `X-Content-Type-Options`. Cheap fix. |
| **17. Trim API responses** | `/api/history` returns full transcripts to any local caller, ungated (unlike export, which is origin-guarded). |
| **20. Scan dependencies** | No Dependabot or `pip-audit` in CI. |

**Not applicable:** hide API keys, public DB key, row-level security, server-side
auth, record/field access control, hash passwords, secure session cookies, rate
limit login, bot protection, restrict file uploads, force HTTPS. There are no
accounts, no server, and no network surface beyond `127.0.0.1`.

---

## Sources

- [Wispr Flow review — features, privacy concerns, pricing](https://www.getvoibe.com/resources/wispr-flow-review/)
- [Wispr Flow vs Superwhisper vs MacWhisper, tested](https://spokenly.app/blog/wispr-flow-vs-superwhisper-vs-macwhisper)
- [Mac dictation tools compared](https://jamesm.blog/ai/mac-dictation-tools-comparison/)
- [Apple — `CGEventType.tapDisabledByTimeout`](https://developer.apple.com/documentation/coregraphics/cgeventtype/tapdisabledbytimeout)
- [Apple Forums — does a listen-only CGEventTap block event handling?](https://developer.apple.com/forums/thread/734237)
- [Secure Input blocking other apps' event taps](https://www.1password.community/1password-at-work-58/secure-input-blocking-other-apps-event-taps-25015)
- [CGEvent taps and code signing: the silent disable race](https://danielraffel.me/til/2026/02/19/cgevent-taps-and-code-signing-the-silent-disable-race/)
- [Ghostty — global keybind stops working after sleep/wake](https://github.com/ghostty-org/ghostty/discussions/11819)
