"""Isolated unit tests — no mic, no whisper-server, no Ollama, no GUI.

These cover the pure logic and data paths that are easy to get subtly wrong,
so they can run in CI on every push. The hardware/end-to-end path lives in
tests/test_pipeline.py and the per-module __main__ self-checks.

Run:  ./.venv/bin/python -m pytest tests/test_units.py -q
"""
import sys
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


def test_silence_guard_rejects_dead_mic_buffer():
    from engine.audio import Recorder, RATE

    r = Recorder()
    r._chunks = [np.zeros(512, dtype=np.float32) for _ in range(int(RATE / 512))]
    r._recording = True
    assert r.end() is None
    assert "no speech" in r.reject_reason
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
# polish: a failure here used to be completely silent, so a dead Ollama meant
# unpolished output forever with no signal anywhere
# --------------------------------------------------------------------------
def test_polish_failure_is_logged_not_swallowed(monkeypatch, capsys):
    from engine import polish as polish_mod

    def boom(*_a, **_kw):
        raise ConnectionError("ollama is not running")

    monkeypatch.setattr(polish_mod.requests, "post", boom)
    raw = "um so basically the quarterly report"
    assert polish_mod.polish(raw) == raw  # still falls back to the user's words
    out = capsys.readouterr().out.lower()
    assert "polish" in out and "ollama is not running" in out
