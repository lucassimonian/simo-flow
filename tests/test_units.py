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
