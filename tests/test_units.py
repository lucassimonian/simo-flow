"""Isolated unit tests — no mic, no whisper-server, no Ollama, no GUI.

These cover the pure logic and data paths that are easy to get subtly wrong,
so they can run in CI on every push. The hardware/end-to-end path lives in
tests/test_pipeline.py and the per-module __main__ self-checks.

Run:  ./.venv/bin/python -m pytest tests/test_units.py -q
"""
import sys
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# --------------------------------------------------------------------------
# audio: silence trimming + the silence guard that killed the "[end of
# transcript]" bug
# --------------------------------------------------------------------------
def test_trim_silence_strips_quiet_edges():
    from engine.audio import trim_silence, RATE

    speech = np.random.randn(RATE).astype(np.float32) * 0.2  # 1s of "speech"
    pad = np.zeros(RATE // 2, dtype=np.float32)  # 0.5s silence each side
    trimmed = trim_silence(np.concatenate([pad, speech, pad]))
    # trimmed keeps the speech (with a small margin) but drops most of the pad
    assert len(trimmed) < len(speech) + RATE  # shorter than speech + full pad
    assert len(trimmed) >= len(speech) * 0.8  # didn't eat the speech


def _fake_audio(monkeypatch, device_ids):
    """Recorder with a stubbed stream and a scripted sequence of device ids."""
    import engine.audio as audio_mod

    calls = {"reinit": 0, "open": 0}

    class FakeStream:
        def __init__(self, **kw):
            calls["open"] += 1

        def start(self):
            pass

        def stop(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(audio_mod.sd, "InputStream", FakeStream)
    monkeypatch.setattr(audio_mod.sd, "_terminate", lambda: calls.__setitem__("reinit", calls["reinit"] + 1))
    monkeypatch.setattr(audio_mod.sd, "_initialize", lambda: None)
    seq = list(device_ids)
    monkeypatch.setattr(audio_mod, "default_input_device", lambda: seq.pop(0) if seq else None)
    return audio_mod.Recorder(), calls


def test_a_changed_input_device_does_not_reinitialise_on_the_hotkey_path(monkeypatch):
    """begin() runs inside the fn-key event-tap callback on the main runloop.

    `sd._terminate()` tears down the process's whole CoreAudio client and can block —
    most likely at exactly the moment a device is switching. Blocking there stalls
    the system input pipeline, which presents as the machine freezing. v2.2.1 did
    re-initialise here on any device change; it froze a laptop mid-meeting when
    AirPods connected while another app held the microphone. Recovery is reactive
    instead: open, and only re-initialise after a failure.
    """
    rec, calls = _fake_audio(monkeypatch, device_ids=[78, 91])  # built-in, then AirPods
    rec.begin()
    rec.end()
    baseline = calls["reinit"]
    rec.begin()  # device changed underneath us
    assert calls["reinit"] == baseline, "must not tear down CoreAudio on the hotkey path"
    assert rec.is_recording is True, "and the press must still record"


def test_an_unchanged_device_costs_nothing(monkeypatch):
    """The check is on the first-word latency path, so it must not re-initialise
    PortAudio when nothing has moved — that used to cost ~70ms per press."""
    rec, calls = _fake_audio(monkeypatch, device_ids=[78, 78, 78])
    rec.begin()
    rec.end()
    baseline = calls["reinit"]
    rec.begin()
    assert calls["reinit"] == baseline, "no device change means no re-initialisation"


def test_device_detection_degrades_quietly_when_coreaudio_is_unavailable(monkeypatch):
    """If the framework can't be read we must still record, falling back to the
    reactive retry rather than refusing to open or thrashing the device list."""
    rec, calls = _fake_audio(monkeypatch, device_ids=[None, None])
    rec.begin()
    assert rec.is_recording is True
    rec.end()
    baseline = calls["reinit"]
    rec.begin()
    assert calls["reinit"] == baseline, "an unknown device id must not look like a change"


def test_default_input_device_answers_on_a_machine_with_a_microphone():
    """Guards the ctypes plumbing: a wrong fourcc or struct layout would return
    None everywhere and silently disable device-change detection, with every
    mocked test still passing. Skipped where there is no input device at all —
    CI runners have none, and failing there would say nothing about the code."""
    import sounddevice as sd

    from engine.audio import default_input_device

    if not any(d["max_input_channels"] > 0 for d in sd.query_devices()):
        pytest.skip("no input device on this machine")
    dev = default_input_device()
    assert isinstance(dev, int) and dev > 0, f"CoreAudio returned {dev!r}"


def test_mic_recovers_when_portaudios_device_list_went_stale(monkeypatch, capsys):
    """Connecting or removing a mic (AirPods) while the app runs leaves
    PortAudio's cached device list stale, and opening the stream then fails with
    an internal error. The failure used to be terminal: nothing re-initialised
    PortAudio, so every press afterwards failed identically until the app was
    restarted by hand. Observed for real — 11 consecutive failures in the log.
    """
    from engine.audio import Recorder
    import engine.audio as audio_mod

    attempts = {"open": 0, "reinit": 0}

    class FakeStream:
        def __init__(self, **kw):
            attempts["open"] += 1
            if attempts["open"] == 1:  # stale device list
                raise RuntimeError("Internal PortAudio error [PaErrorCode -9986]")

        def start(self):
            pass

    monkeypatch.setattr(audio_mod.sd, "InputStream", FakeStream)
    monkeypatch.setattr(audio_mod.sd, "_terminate", lambda: attempts.__setitem__("reinit", attempts["reinit"] + 1))
    monkeypatch.setattr(audio_mod.sd, "_initialize", lambda: None)

    r = Recorder()
    r.begin()
    # begin() deliberately returns before the device is open — it runs on the
    # fn-key tap, where blocking freezes the machine. The recovery still happens,
    # just on the audio thread, so wait for it before judging the outcome.
    assert r._drain_audio_ops(timeout=5.0), "audio thread never finished the open"
    assert r.is_recording is True, "must recover, not fail until the app is restarted"
    assert attempts["reinit"] >= 1, "PortAudio must be re-initialised before retrying"
    assert "recovered" in capsys.readouterr().out.lower()


def test_mic_that_cannot_open_at_all_says_so_plainly(monkeypatch):
    """"no audio captured" is a misleading thing to tell someone whose microphone
    never opened — it reads as "you didn't speak"."""
    from engine.audio import Recorder
    import engine.audio as audio_mod

    def always_fails(**kw):
        raise RuntimeError("Internal PortAudio error [PaErrorCode -9986]")

    monkeypatch.setattr(audio_mod.sd, "InputStream", always_fails)
    monkeypatch.setattr(audio_mod.sd, "_terminate", lambda: None)
    monkeypatch.setattr(audio_mod.sd, "_initialize", lambda: None)

    r = Recorder()
    r.begin()
    assert r._drain_audio_ops(timeout=5.0), "audio thread never finished the open"
    assert r.is_recording is False
    assert r.end() is None
    assert "microphone" in r.reject_reason.lower(), r.reject_reason
    # and the next press must try a fresh device list rather than give up for good
    assert r._needs_reinit is True


def test_silence_guard_rejects_dead_mic_buffer():
    """A bit-exact-zero buffer is now named for what it is: a dead microphone.

    This test always fed pure zeros — the dead-mic case its own name describes —
    but asserted the generic "no speech detected" wording, which reads as "you
    didn't say anything" and sends the user looking in the wrong place. Zeros can
    only mean the device delivered nothing, so it says so.
    """
    from engine.audio import Recorder, RATE

    r = Recorder()
    r._chunks = [np.zeros(512, dtype=np.float32) for _ in range(int(RATE / 512))]
    r._recording = True
    assert r.end() is None
    assert "microphone" in r.reject_reason.lower(), r.reject_reason
    assert r._needs_reinit is True  # silence flags a device refresh


def test_silence_guard_rejects_too_short():
    from engine.audio import Recorder

    r = Recorder()
    r._chunks = [np.random.randn(512).astype(np.float32) * 0.2]  # ~32ms
    r._recording = True
    assert r.end() is None
    assert r.reject_reason == "too short"


def test_recorder_accepts_real_speech():
    from engine.audio import Recorder, RATE

    r = Recorder()
    r._chunks = [np.random.randn(512).astype(np.float32) * 0.2 for _ in range(int(RATE / 512))]
    r._recording = True
    out = r.end()
    assert out is not None and len(out) > 0


# --------------------------------------------------------------------------
# stt: repetition dedupe (whisper artifact on noisy tails)
# --------------------------------------------------------------------------
def test_transcript_line_wrapping_is_flattened():
    """whisper.cpp emits its transcript wrapped into ~57-character segments, so
    the text arrives with newlines mid-sentence. Pasted verbatim that dropped
    line breaks into the middle of dictated sentences — visible in 86% of real
    transcripts. Dictation is a stream of speech, not a formatted document."""
    from engine.stt import flatten_whitespace

    wrapped = "I really appreciate you taking the time\n to respond to me."
    assert flatten_whitespace(wrapped) == "I really appreciate you taking the time to respond to me."
    # every flavour of break collapses to exactly one space
    assert flatten_whitespace("a\r\nb\tc  d\n\n\ne") == "a b c d e"
    assert flatten_whitespace("  padded  ") == "padded"
    assert flatten_whitespace("") == ""


def test_transcribe_flattens_before_returning(monkeypatch):
    """The flattening has to happen in transcribe(), not at the paste site: the
    history, the exact-mode path and the polish input all read from here."""
    import engine.stt as stt

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"text": "first part of the line\n and the wrapped remainder."}

    monkeypatch.setattr(stt, "is_up", lambda: True)
    monkeypatch.setattr(stt, "requests", type("R", (), {"post": staticmethod(lambda *a, **k: FakeResponse())}))
    import numpy as np

    out = stt.transcribe(np.zeros(16000, dtype=np.float32))
    assert "\n" not in out
    assert out == "first part of the line and the wrapped remainder."


def test_dedupe_collapses_consecutive_duplicates():
    from engine.stt import dedupe_sentences

    assert dedupe_sentences("Hello there. Hello there.") == "Hello there."
    # non-adjacent duplicates are kept
    assert dedupe_sentences("A. B. A.") == "A. B. A."
    # abbreviations aren't split/eaten
    assert "3 p.m." in dedupe_sentences("Meet at 3 p.m.")


# --------------------------------------------------------------------------
# polish: the rewrite guard — a dictated question must NEVER come back answered.
#
# Contract (see polish.py header): cleanup only ever *deletes* filler, so a valid
# output is an ordered subsequence of the spoken words, and it keeps the real
# content words (fillers may go, content may not). Anything else — reordering,
# substituting, appending an answer, or collapsing to a keyword — is a rewrite
# and must be discarded in favour of the raw transcript. These tests pin every
# answer shape we have actually observed qwen produce.
# --------------------------------------------------------------------------
def test_rewrite_guard_rejects_answer_that_adds_words():
    from engine.polish import _is_rewrite

    raw = "how do I become a top vibe coder and stand out from other candidates"
    answer = ("To become a top vibe coder, focus on foundational skills, master "
              "relevant programming languages, and stay updated with industry trends.")
    assert _is_rewrite(raw, answer) is True  # balloons length + words never spoken


def test_rewrite_guard_rejects_substituted_answer():
    from engine.polish import _is_rewrite

    # word never spoken — not a subsequence of the input
    assert _is_rewrite("what is the capital of france", "Paris") is True
    assert _is_rewrite("what's two plus two", "2 + 2") is True


def test_rewrite_guard_rejects_reordered_answer():
    from engine.polish import _is_rewrite

    # every word except "Paris" was spoken, but the model reordered them into an
    # answer — "is/Paris" cannot follow "France", so it isn't a subsequence.
    assert _is_rewrite("what is the capital of France", "The capital of France is Paris.") is True


def test_rewrite_guard_rejects_collapse_to_keyword():
    from engine.polish import _is_rewrite

    # THE REGRESSION: "Restaurant" is a subsequence of the input (it was spoken),
    # yet the model answered by dropping every other content word. The old
    # word-overlap guard missed this because zero new words were introduced.
    assert _is_rewrite("how do you spell restaurant", "Restaurant") is True


def test_rewrite_guard_rejects_dropped_negation():
    from engine.polish import _is_rewrite

    # A dropped negator is a valid subsequence that keeps most words, yet it
    # INVERTS meaning — the exact harm the guard exists to stop, reached by
    # deletion instead of addition. Every one of these must be rejected.
    assert _is_rewrite("we should not merge this branch yet", "we should merge this branch yet") is True
    assert _is_rewrite("please don't cancel the meeting", "please cancel the meeting") is True
    assert _is_rewrite("increase the budget by not more than 10 percent", "increase the budget by 10 percent") is True
    assert _is_rewrite("I never said that", "I said that") is True


def test_rewrite_guard_survives_apostrophe_and_accent_variants():
    from engine.polish import _is_rewrite

    # curly apostrophe in the transcript, straight in the model output: the same
    # word. Must NOT be a false reject (contractions are everywhere).
    assert _is_rewrite("I don’t think we should um go", "I don't think we should go.") is False
    # negation preserved across the apostrophe-style change is fine
    assert _is_rewrite("please don’t cancel", "Please don't cancel.") is False
    # accent normalized away by the model is still the same word, not a rewrite
    assert _is_rewrite("grab a café before we start", "Grab a cafe before we start.") is False


def test_rewrite_guard_allows_legit_cleanup():
    from engine.polish import _is_rewrite

    # filler removal + punctuation is not a rewrite
    assert _is_rewrite("um so i think we should meet at 3pm", "So I think we should meet at 3pm.") is False
    # hedges survive verbatim alongside filler removal
    assert _is_rewrite(
        "um so basically i think we should uh meet at 3pm tomorrow to discuss the you know the quarterly report",
        "So basically I think we should meet at 3pm tomorrow to discuss the quarterly report.",
    ) is False
    # a question cleaned and punctuated (NOT answered) is the desired output
    assert _is_rewrite("what time are we meeting tomorrow", "What time are we meeting tomorrow?") is False
    assert _is_rewrite("who wrote Romeo and Juliet", "Who wrote Romeo and Juliet?") is False
    # an imperative transcribed faithfully, not obeyed
    assert _is_rewrite("give me three ideas for dinner", "Give me three ideas for dinner.") is False
    # a stutter/repeat removed is still cleanup
    assert _is_rewrite("send me the the report", "Send me the report.") is False
    # empty output isn't flagged (handled by the caller, which falls back to raw)
    assert _is_rewrite("hello", "") is False


# --------------------------------------------------------------------------
# pipeline: junk transcripts are never pasted
# --------------------------------------------------------------------------
def test_junk_transcripts_cover_whisper_silence_outputs():
    import engine.__main__ as m

    for junk in ["[end of transcript]", "[BLANK_AUDIO]", "(silence)", ""]:
        assert junk.strip().lower() in m.JUNK_TRANSCRIPTS


# --------------------------------------------------------------------------
# store: history / dictionary / settings / insights on an isolated temp DB
# --------------------------------------------------------------------------
@pytest.fixture()
def store(tmp_path, monkeypatch):
    import engine.store as store_mod

    monkeypatch.setattr(store_mod, "DB_PATH", tmp_path / "test.db")
    return store_mod


def test_history_roundtrip_and_search(store):
    store.log_dictation("um hello", "Hello.", 500, 2.0, "unit")
    store.log_dictation("bye", "Goodbye.", 400, 1.5, "unit")
    assert store.history(1)[0]["polished_text"] == "Goodbye."  # newest first
    assert len(store.history(q="Hello")) == 1
    assert store.history(q="nothing-matches") == []


def test_dictionary_crud_and_prompt(store):
    store.dictionary_add("Simonian")
    store.dictionary_add("Simonian")  # de-duped by UNIQUE
    terms = store.dictionary_terms()
    assert [t["term"] for t in terms] == ["Simonian"]
    assert "Simonian" in store.dictionary_prompt()
    store.dictionary_delete(terms[0]["id"])
    assert store.dictionary_terms() == []
    assert store.dictionary_prompt() == ""


def test_settings_persist(store):
    assert store.get_setting("model_tier", "accurate") == "accurate"  # default
    store.set_setting("model_tier", "fast")
    assert store.get_setting("model_tier") == "fast"
    store.set_setting("model_tier", "accurate")  # upsert
    assert store.get_setting("model_tier") == "accurate"


def test_clear_history_leaves_dictionary(store):
    store.log_dictation("x", "X.", 100, 1.0)
    store.dictionary_add("keepme")
    assert store.clear_history() == 1
    assert store.history() == []
    assert any(t["term"] == "keepme" for t in store.dictionary_terms())


def test_insights_shape(store):
    store.log_dictation("a b c", "A b c.", 500, 2.0)
    ins = store.insights()
    for key in ("total_words", "total_utterances", "wpm", "fixes", "avg_latency_ms", "per_day"):
        assert key in ins
    assert ins["total_utterances"] == 1


# --------------------------------------------------------------------------
# api: the Host guard (DNS-rebind) and Origin guard (CSRF)
# --------------------------------------------------------------------------
@pytest.fixture()
def client(tmp_path, monkeypatch):
    import engine.store as store_mod

    monkeypatch.setattr(store_mod, "DB_PATH", tmp_path / "api.db")
    from starlette.testclient import TestClient
    from engine import api

    return TestClient(api.app)


GOOD_HOST = {"Host": "127.0.0.1:7331"}


def test_host_guard_blocks_dns_rebind(client):
    # a forged/rebound Host must be refused even on a read endpoint
    assert client.get("/api/history", headers={"Host": "evil.com"}).status_code == 403
    assert client.get("/api/history", headers=GOOD_HOST).status_code == 200


def test_origin_guard_blocks_cross_origin_write(client):
    bad = client.post("/api/dictionary", json={"term": "x"},
                      headers={**GOOD_HOST, "Origin": "http://evil.com"})
    assert bad.status_code == 403
    ok = client.post("/api/dictionary", json={"term": "legit"},
                     headers={**GOOD_HOST, "Origin": "http://127.0.0.1:7331"})
    assert ok.status_code == 200


# --------------------------------------------------------------------------
# privacy: the history is a full plaintext record of everything ever dictated,
# so deleting and exporting it must be reachable, not just implemented
# --------------------------------------------------------------------------
def test_history_can_be_deleted_through_the_api(client):
    client.post("/api/dictionary", json={"term": "seed"},
                headers={**GOOD_HOST, "Origin": "http://127.0.0.1:7331"})
    from engine import store

    store.log_dictation("um hello", "Hello.", 100, 1.0, "unit")
    assert len(client.get("/api/history", headers=GOOD_HOST).json()) == 1

    r = client.delete("/api/history", headers={**GOOD_HOST, "Origin": "http://127.0.0.1:7331"})
    assert r.status_code == 200
    assert r.json()["deleted"] == 1
    assert client.get("/api/history", headers=GOOD_HOST).json() == []
    # deleting history must not take the custom dictionary with it
    assert any(t["term"] == "seed" for t in client.get("/api/dictionary", headers=GOOD_HOST).json())


def test_history_delete_is_refused_cross_origin(client):
    """A page the user is browsing must not be able to wipe their history."""
    bad = client.delete("/api/history", headers={**GOOD_HOST, "Origin": "http://evil.com"})
    assert bad.status_code == 403


def test_history_flatten_is_refused_cross_origin(client):
    """It rewrites stored dictations, so it is a write like any other."""
    bad = client.post("/api/history/flatten", headers={**GOOD_HOST, "Origin": "http://evil.com"})
    assert bad.status_code == 403


SAME_ORIGIN = {**GOOD_HOST, "Origin": "http://127.0.0.1:7331"}
# fetch() omits Origin on same-origin GETs, so this is what the dashboard's own
# export request actually looks like on the wire.
SAME_REFERER = {**GOOD_HOST, "Referer": "http://127.0.0.1:7331/#privacy"}


def test_write_guard_fails_closed_without_origin_or_referer(client):
    """A client that simply omits the header must not inherit write access — for
    these endpoints this check is the only protection there is."""
    assert client.delete("/api/history", headers=GOOD_HOST).status_code == 403
    assert client.post("/api/dictionary", json={"term": "x"}, headers=GOOD_HOST).status_code == 403


def test_write_guard_accepts_referer_when_origin_absent(client):
    assert client.delete("/api/history", headers=SAME_REFERER).status_code == 200


def test_write_guard_rejects_foreign_referer(client):
    bad = {**GOOD_HOST, "Referer": "http://evil.com/page"}
    assert client.delete("/api/history", headers=bad).status_code == 403


def test_export_is_refused_cross_origin(client):
    """Unreadable cross-origin anyway, but an unbounded full-table read is worth
    denying as a trigger too."""
    bad = {**GOOD_HOST, "Origin": "http://evil.com"}
    assert client.get("/api/history/export", headers=bad).status_code == 403
    assert client.get("/api/history/export", headers=GOOD_HOST).status_code == 403


def test_export_works_the_way_the_dashboard_actually_calls_it(client):
    """Guards the regression this pairs with: the dashboard's export is a
    same-origin GET, which carries Referer and no Origin."""
    assert client.get("/api/history/export", headers=SAME_REFERER).status_code == 200


def test_no_cors_headers_are_ever_sent(client):
    """A great deal rides on the *absence* of Access-Control-Allow-Origin: it is
    what makes every read endpoint unreadable to a malicious page. Nothing else
    enforces that, so assert it — adding permissive CORS middleware later would
    silently open the entire history to any site the user visits."""
    for path in ("/", "/api/history", "/api/history/export", "/api/insights", "/api/dictionary"):
        r = client.get(path, headers={**SAME_REFERER, "Origin": "http://evil.com"})
        assert "access-control-allow-origin" not in {k.lower() for k in r.headers}, path
        assert "access-control-allow-credentials" not in {k.lower() for k in r.headers}, path


def test_history_export_returns_every_row(client):
    from engine import store

    for i in range(3):
        store.log_dictation(f"raw {i}", f"Polished {i}.", 100, 1.0, "unit")
    r = client.get("/api/history/export", headers=SAME_REFERER)
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 3
    # export is for taking your data elsewhere: it must carry the raw transcript
    # and timing, not just the cleaned text the feed shows
    assert {"raw_text", "polished_text", "ts", "audio_sec"} <= set(rows[0])


# --------------------------------------------------------------------------
# the utterance claim: exactly one of {discard timer, ✕, ✓/release} may consume a
# recording. Timer.cancel() does nothing once the timer body has begun running,
# so a ✓ pressed as the discard timer fired had both call recorder.end() — the
# timer took the audio and the user's deliberate commit was reported as "no audio
# captured" and dropped. Tested against the unbound method so no menu-bar app,
# mic, or runloop is needed.
# --------------------------------------------------------------------------
def _claimable():
    """Minimal stand-in carrying only the attributes _claim_utterance touches."""
    import threading

    class Bare:
        pass

    b = Bare()
    b._tap_lock = threading.Lock()
    b._pending_discard = None
    b._consumed = False
    b._fn_down = False
    return b


def _claim(bare, **kw):
    import importlib

    return importlib.import_module("engine.__main__").SimoFlow._claim_utterance(bare, **kw)


def test_a_failed_paste_still_records_the_dictation(monkeypatch, tmp_path):
    """A paste that can't land should cost the paste, not the words.

    The pipeline used to return before writing to history, so a revoked
    Accessibility permission destroyed the transcription outright — spoken, waited
    for, and gone, with no copy anywhere. Exercised against the unbound method so
    no menu-bar app, mic or runloop is needed.
    """
    import numpy as np

    m = _mainmod()
    import engine.inject as inject_mod
    import engine.polish as polish_mod
    import engine.store as store_mod
    import engine.stt as stt_mod

    monkeypatch.setattr(store_mod, "DB_PATH", tmp_path / "pipeline.db")
    monkeypatch.setattr(stt_mod, "transcribe", lambda *a, **k: "the paste is going to fail")
    monkeypatch.setattr(store_mod, "dictionary_prompt", lambda: "")
    monkeypatch.setattr(polish_mod, "needs_cleanup", lambda _t: False)
    monkeypatch.setattr(polish_mod, "polish", lambda t, *a, **k: t)
    monkeypatch.setattr(inject_mod, "paste_text", lambda *a, **k: False)  # refused

    flashes: list[str] = []

    class BarePill:
        def busy(self, stage=""):
            pass

        def flash(self, msg, hold_sec=1.6):
            flashes.append(msg)

        def hide(self):
            pass

    class Bare:
        exact_mode = False
        pill = BarePill()

        def _ui_title(self, _t):
            pass

    m.SimoFlow._run_pipeline(Bare(), np.zeros(16000, dtype=np.float32), None)

    rows = store_mod.history()
    assert len(rows) == 1, "the transcription must survive a refused paste"
    assert rows[0]["polished_text"] == "the paste is going to fail"
    assert flashes and "saved" in flashes[0].lower(), f"and say where it went: {flashes}"


def test_a_hold_is_timed_from_the_key_not_from_the_interface_catching_up():
    """Moving the tap off the main runloop must not change what a hold means.

    The tap now runs on its own thread and hands the state machine to the main
    thread, where the UI lives. If the timestamps were taken after that hop, a
    busy interface would compress the measured hold: press delayed by 200ms and
    release delivered on time turns a deliberate 0.4s push-to-talk into something
    under HOLD_SEC, which is discarded as a stray tap. The user speaks a sentence
    and gets nothing.

    So both stamps are taken on the hotkey thread. This drives the two callbacks
    with a deliberately late main thread and checks the measured hold survives.
    """
    m = _mainmod()

    dispatched: list = []

    class Bare:
        _on_main = staticmethod(dispatched.append)
        _tap_press = m.SimoFlow._tap_press
        _tap_release = m.SimoFlow._tap_release

        def __init__(self):
            self.stamps: list[tuple[str, float]] = []

        def _on_press(self, at=None):
            self.stamps.append(("press", at))

        def _on_release(self, at=None):
            self.stamps.append(("release", at))

    bare = Bare()
    bare._tap_press()
    time.sleep(m.HOLD_SEC + 0.08)  # a real, deliberate hold
    bare._tap_release()

    assert bare.stamps == [], "the tap thread must not run the state machine itself"

    time.sleep(0.15)  # the interface is busy and only now gets round to it
    for fn in dispatched:
        fn()

    assert [kind for kind, _ in bare.stamps] == ["press", "release"], "order must survive"
    held = bare.stamps[1][1] - bare.stamps[0][1]
    assert held >= m.HOLD_SEC, (
        f"hold measured as {held:.3f}s against a {m.HOLD_SEC}s threshold — timing it after "
        f"the hop would discard a real push-to-talk as a stray tap"
    )


def test_snippets_are_expanded_during_a_real_dictation(tmp_path, monkeypatch):
    """The feature must actually be wired in, not merely implemented.

    test_snippets.py covers the matching rules and the store, but neither would
    fail if the call vanished from the pipeline — the whole feature could be
    silently disconnected with the suite still green. This drives the real
    pipeline and asserts on what reaches the paste.

    It also pins the ordering: expansion happens after polish, so what gets pasted
    and what gets stored both contain the replacement rather than the trigger.
    """
    import numpy as np

    m = _mainmod()
    import engine.inject as inject_mod
    import engine.polish as polish_mod
    import engine.store as store_mod
    import engine.stt as stt_mod

    monkeypatch.setattr(store_mod, "DB_PATH", tmp_path / "snippet_pipeline.db")
    assert store_mod.snippet_add("my email address", "lucas@example.com") is True

    monkeypatch.setattr(stt_mod, "transcribe", lambda *a, **k: "Send it to my email address.")
    monkeypatch.setattr(store_mod, "dictionary_prompt", lambda: "")
    monkeypatch.setattr(polish_mod, "needs_cleanup", lambda _t: False)
    monkeypatch.setattr(polish_mod, "polish", lambda t, *a, **k: t)

    pasted: list[str] = []

    def fake_paste(text, focus=None, on_pasted=None, **_kw):
        pasted.append(text)
        if on_pasted:
            on_pasted()
        return True

    monkeypatch.setattr(inject_mod, "paste_text", fake_paste)

    class BarePill:
        def busy(self, stage=""):
            pass

        def flash(self, msg, hold_sec=1.6):
            pass

        def hide(self):
            pass

    class Bare:
        exact_mode = False
        pill = BarePill()

        def _ui_title(self, _t):
            pass

    m.SimoFlow._run_pipeline(Bare(), np.zeros(16000, dtype=np.float32), None)

    assert pasted == ["Send it to lucas@example.com."], f"snippet never fired: {pasted}"
    assert store_mod.history()[0]["polished_text"] == "Send it to lucas@example.com.", (
        "history must record what was actually pasted, not the trigger phrase"
    )


def test_only_the_first_consumer_claims_an_utterance():
    bare = _claimable()
    assert _claim(bare) is True, "first caller takes the recording"
    assert _claim(bare) is False, "second caller must not also end the recording"
    assert _claim(bare) is False


def test_claiming_cancels_a_pending_discard_timer_and_clears_it():
    import threading

    bare = _claimable()
    fired = []
    bare._pending_discard = threading.Timer(5.0, lambda: fired.append(1))
    bare._pending_discard.start()
    try:
        assert _claim(bare) is True
        assert bare._pending_discard is None
        assert fired == []
    finally:
        pass  # the timer was cancelled by the claim; nothing left to clean up


def test_discard_timer_will_not_take_a_recording_while_fn_is_still_held():
    """The interleaving that the first version of this fix made *worse*.

    The discard timer fires 0.5s after a lone tap. If it lands in the gap between
    the second tap of a double-tap going down and coming up, it used to claim and
    delete the audio — and the release then engaged the hands-free lock over
    nothing, so the user watched "Recording — tap fn to stop" and spoke into a
    recording that no longer existed, with no error at all. A held key means the
    tap has arrived; only its release decides what the recording becomes.
    """
    bare = _claimable()
    bare._fn_down = True  # second tap is physically down, release not seen yet
    assert _claim(bare, skip_if_key_down=True) is False
    assert bare._consumed is False, "the release path must still be able to claim"
    # once the key is up, the timer may have it
    bare._fn_down = False
    assert _claim(bare, skip_if_key_down=True) is True


def test_key_down_guard_applies_only_to_the_discard_timer():
    """✓ and ✕ are deliberate: they must work even with fn held."""
    bare = _claimable()
    bare._fn_down = True
    assert _claim(bare) is True, "an explicit commit/cancel is never deferred"


def test_a_timer_that_already_fired_loses_to_whoever_claimed_first():
    """The exact race: the discard timer's body has started, so cancel() is a
    no-op. The flag — not cancel() — is what stops a double consume."""
    bare = _claimable()
    assert _claim(bare) is True  # ✓ got there first
    bare._pending_discard = None  # timer already began; cancel() would do nothing
    assert _claim(bare) is False, "the timer must not also end the recording"


# --------------------------------------------------------------------------
# the log holds the plaintext of everything ever dictated, so it must not grow
# without bound — and it must not be written twice per line
# --------------------------------------------------------------------------
def _mainmod():
    import importlib

    return importlib.import_module("engine.__main__")


def test_log_is_trimmed_once_it_exceeds_the_cap(tmp_path):
    m = _mainmod()
    log = tmp_path / "big.log"
    line = b"x" * 999 + b"\n"
    log.write_bytes(line * 12_000)  # ~12MB
    assert log.stat().st_size > m.MAX_LOG_BYTES

    assert m._trim_log(str(log), max_bytes=m.MAX_LOG_BYTES, keep_bytes=m.KEEP_LOG_BYTES) is True
    size = log.stat().st_size
    assert size <= m.KEEP_LOG_BYTES + 4096, "must shrink to roughly the keep size"
    body = log.read_bytes()
    assert b"trimmed" in body.split(b"\n", 1)[0], "says why it is shorter than you expect"
    assert body.endswith(line), "the newest lines are the ones worth keeping"


def test_log_under_the_cap_is_left_completely_alone(tmp_path):
    m = _mainmod()
    log = tmp_path / "small.log"
    log.write_bytes(b"one line\n")
    assert m._trim_log(str(log), max_bytes=m.MAX_LOG_BYTES, keep_bytes=m.KEEP_LOG_BYTES) is False
    assert log.read_bytes() == b"one line\n"


def test_trimming_keeps_the_log_owner_only(tmp_path):
    """It is a verbatim record of everything spoken — 0600 is not optional."""
    import os
    import stat

    m = _mainmod()
    log = tmp_path / "perm.log"
    log.write_bytes(b"y" * (m.MAX_LOG_BYTES + 5000))
    os.chmod(log, 0o644)  # as an older install left it
    m._trim_log(str(log), max_bytes=m.MAX_LOG_BYTES, keep_bytes=m.KEEP_LOG_BYTES)
    assert stat.S_IMODE(log.stat().st_mode) == 0o600


def test_trim_survives_a_missing_log(tmp_path):
    m = _mainmod()
    assert m._trim_log(str(tmp_path / "nope.log")) is False


def test_old_dictations_can_be_stripped_of_whispers_line_wrapping(tmp_path, monkeypatch):
    """Rows written before v2.1.1 still break mid-sentence in the feed and exports.

    The migration must produce exactly what a new dictation would, which is why it
    reuses stt.flatten_whitespace rather than its own rule.
    """
    import engine.store as store

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "h.db")

    store.log_dictation("raw one", "It could be that we don't have enough\n candidates.", 100)
    store.log_dictation("raw two", "Already flat.", 100)

    assert store.history_needing_flatten() == 1
    assert store.flatten_history() == 1
    assert store.history_needing_flatten() == 0

    texts = [r["polished_text"] for r in store.history()]
    assert "It could be that we don't have enough candidates." in texts
    assert "Already flat." in texts, "a row that needed nothing must not be touched"


def test_flattening_history_backs_the_database_up_first(tmp_path, monkeypatch):
    """It rewrites words the user actually spoke. A restore path is not optional."""
    import engine.store as store

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "h.db")
    store.log_dictation("raw", "wrapped\n text", 100)

    store.flatten_history()

    backups = list(tmp_path.glob("h.db.bak-*"))
    assert len(backups) == 1, f"no backup was taken; found {backups}"
    assert backups[0].stat().st_size > 0

    import stat as stat_mod

    assert stat_mod.S_IMODE(backups[0].stat().st_mode) == 0o600, "backup left readable by others"


def test_the_backup_is_a_real_database_holding_the_pre_migration_text(tmp_path, monkeypatch):
    """A backup that might not open is not a restore path.

    Two threads write this database on separate connections — the dictation
    pipeline and the dashboard's API server — so copying the file while one is
    mid-transaction can produce a torn, unopenable snapshot. SQLite's own backup
    API is the only thing that guarantees a consistent one.
    """
    import sqlite3

    import engine.store as store

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "h.db")
    store.log_dictation("raw one", "It breaks\n mid sentence.", 100)

    assert store.flatten_history() == 1

    backup = next(iter(tmp_path.glob("h.db.bak-*")))
    conn = sqlite3.connect(backup)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        saved = conn.execute("SELECT polished_text FROM history").fetchone()[0]
    finally:
        conn.close()

    assert "\n" in saved, "the backup must hold the text as it was BEFORE the rewrite"
    assert store.history()[0]["polished_text"] == "It breaks mid sentence."


def test_tidying_nothing_writes_nothing(tmp_path, monkeypatch):
    """Pressing the button twice must not leave a second copy of every word you
    have ever dictated sitting in your home folder."""
    import engine.store as store

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "h.db")
    store.log_dictation("clean raw", "Already flat.", 100)

    assert store.flatten_history() == 0
    assert list(tmp_path.glob("h.db.bak-*")) == [], "backed up with nothing to back up"


def test_boot_log_is_made_owner_only(tmp_path, monkeypatch):
    """launchd creates the boot log 644 — readable by every account on the machine.

    This guard shipped untested: the mutation sweep flagged it as surviving, which
    means it could have been deleted or broken and nothing would have failed.
    """
    import os
    import stat

    m = _mainmod()
    boot = tmp_path / "boot.log"
    boot.write_text("startup\n")
    os.chmod(boot, 0o644)  # exactly as launchd leaves it
    monkeypatch.setattr(m, "BOOT_LOG_PATH", str(boot))

    m._harden_boot_log()

    assert stat.S_IMODE(boot.stat().st_mode) == 0o600, "boot log left world-readable"


def test_hardening_survives_a_boot_log_that_is_not_there(tmp_path, monkeypatch):
    """Run by hand rather than under launchd, there is no boot log at all — and a
    missing file must never stop the app starting."""
    m = _mainmod()
    monkeypatch.setattr(m, "BOOT_LOG_PATH", str(tmp_path / "absent.log"))
    m._harden_boot_log()  # must not raise


def test_a_launchd_redirected_stream_is_never_kept_as_a_sink(tmp_path):
    """The boot log must never receive dictated speech.

    launchd redirects stdout/stderr to ~/.simo-flow.boot.log, which it creates at
    the default umask — mode 644, readable by every account on the machine. Keeping
    those streams as sinks wrote every pipeline line, transcripts included, into it
    for the whole process lifetime. Found in production at 45KB / 36 transcript
    lines.

    Tested as a pure decision rather than by calling _tee_logs(): that function
    reassigns global sys.stdout, and a test which does so then hangs the rest of the
    suite through pytest's capture. Found the hard way.
    """
    m = _mainmod()
    boot = tmp_path / "boot.log"
    boot.write_text("")
    with open(boot, "a") as file_backed:
        assert m._keep_stream(file_backed) is False, "launchd's redirect must be dropped"
    assert m._keep_stream(None) is False

    class FakeTTY:
        def isatty(self):
            return True

    class Broken:
        def isatty(self):
            raise ValueError("closed")

    assert m._keep_stream(FakeTTY()) is True, "a terminal must still receive output"
    assert m._keep_stream(Broken()) is False, "an unusable stream is not a sink"


def test_boot_log_path_is_separate_from_the_transcript_log():
    """Two distinct files: one launchd owns for pre-startup crashes, one the app owns
    and trims. Pointing both at the same path is what caused every line to be
    written twice in v2.1.1."""
    m = _mainmod()
    assert m.BOOT_LOG_PATH != m.LOG_PATH
    assert "boot" in m.BOOT_LOG_PATH


def test_same_file_detects_a_stream_already_pointed_at_the_log(tmp_path):
    """launchd used to redirect stdout to the very file we tee into, so every
    line landed twice. Detecting that is what stops the duplication."""
    m = _mainmod()
    log = tmp_path / "dup.log"
    other = tmp_path / "other.log"
    log.write_text("")
    other.write_text("")
    with open(log, "a") as a, open(other, "a") as b:
        assert m._same_file(a, str(log)) is True
        assert m._same_file(b, str(log)) is False
    assert m._same_file(None, str(log)) is False  # no stream at all under launchd


# --------------------------------------------------------------------------
# polish: a failure here used to be completely silent, so a dead Ollama meant
# unpolished output forever with no signal anywhere
# --------------------------------------------------------------------------
def test_clean_speech_skips_the_model_entirely(monkeypatch):
    """72% of real dictations came back from the model unchanged. Spending a
    second to be handed back exactly what whisper said is pure latency.

    Counts calls rather than raising from the stub. An earlier version of this test
    raised AssertionError if the model was called — which `polish()` caught with its
    `except Exception` fallback, returned the raw text, and satisfied the assertion
    anyway. The test passed whether or not the skip existed. Found by
    tools/mutation_sweep.py, not by reading it.
    """
    from engine import polish as polish_mod

    calls: list[str] = []
    monkeypatch.setattr(polish_mod.requests, "post", lambda *a, **k: calls.append("called"))
    clean = "I want to check the onboarding flow before we ship it."
    assert polish_mod.polish(clean) == clean
    assert calls == [], "the model must not be called when there is nothing to remove"


def test_needs_cleanup_spots_what_cleanup_is_allowed_to_remove():
    from engine.polish import needs_cleanup

    assert needs_cleanup("um so I think we should meet") is True  # filler
    assert needs_cleanup("I I think we should meet") is True  # stutter
    assert needs_cleanup("Let's meet at 3pm tomorrow.") is False


def test_needs_cleanup_does_not_mistake_real_english_for_a_stutter():
    """"I said that that was fine" is not a stutter, and sending it for cleanup
    would give the model a chance to rewrite a sentence that was already correct."""
    from engine.polish import needs_cleanup

    assert needs_cleanup("I said that that was fine.") is False
    assert needs_cleanup("She had had enough by then.") is False


def test_hedges_never_trigger_cleanup_on_their_own():
    """Hedges are content the prompt tells the model to keep. Sending an utterance
    for cleanup purely because it contains one invites the model to strip it —
    which is exactly what it did on real data."""
    from engine.polish import needs_cleanup

    for hedge in ("I think", "basically", "kind of", "sort of", "honestly", "to be fair"):
        assert needs_cleanup(f"We should {hedge} ship it tomorrow.") is False, hedge


def test_polish_failure_is_logged_not_swallowed(monkeypatch, capsys):
    from engine import polish as polish_mod

    def boom(*_a, **_kw):
        raise ConnectionError("ollama is not running")

    monkeypatch.setattr(polish_mod.requests, "post", boom)
    raw = "um so basically the quarterly report"
    assert polish_mod.polish(raw) == raw  # still falls back to the user's words
    out = capsys.readouterr().out.lower()
    assert "polish" in out and "ollama is not running" in out
