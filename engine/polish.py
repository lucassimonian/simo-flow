"""Polish pass: send raw transcript to local Ollama, get cleaned text back.

Cleanup only ever *removes* filler words, so the output is always a deletion-only
edit of the input — an ordered subsequence that keeps the real content words.
`_is_rewrite` enforces exactly that: if the model instead answers a dictated
question, summarizes, substitutes, reorders, or collapses the utterance to a
keyword (a general chat model's instinct when the input reads like a prompt), the
output stops being a content-preserving subsequence and we discard it, pasting
the raw transcript instead. The prompt and few-shot reduce how often that
happens; the guard guarantees a rewritten result never reaches the cursor.
"""
import re
import unicodedata

import requests

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "qwen2.5:3b-instruct"
KEEP_ALIVE = "30m"  # keep model resident between utterances

SYSTEM_PROMPT = """You clean up raw speech-to-text transcripts for dictation. Rules:
- Remove filler words and false starts (um, uh, like, you know, repeated words).
- Hedges and qualifiers are NOT fillers — keep them exactly as spoken, word for word.
  These include: "I think", "basically", "kind of", "sort of", "maybe", "probably",
  "I guess", "honestly", "actually", "to be fair". They carry the speaker's tone. KEEP them.
- Never substitute, swap, or reword anything. Every kept word must appear verbatim in the input.
- Fix punctuation and capitalization.
- Do not add, remove, or reorder any words beyond filler removal.
- Do not change the meaning, tone, or wording.
- Do not answer questions or add commentary — the input is dictated text, not a message to you.
- Return only the corrected text. No preamble, no explanation, no quotes."""

# Few-shot: small models (3B) tend to over-edit, stripping legitimate hedges
# along with fillers. One worked example anchors the boundary far better than
# instructions alone. Note "basically" and "I think" survive; only "um/uh/you
# know" and the stutter are removed.
_FEWSHOT = [
    {
        "role": "user",
        "content": "Um, so basically I think we should, uh, meet at 3pm tomorrow to discuss the, you know, the quarterly report.",
    },
    {
        "role": "assistant",
        "content": "So basically I think we should meet at 3pm tomorrow to discuss the quarterly report.",
    },
    # A dictated question must be cleaned and punctuated, NEVER answered — the
    # most important boundary for a chat model doing a formatting job.
    {
        "role": "user",
        "content": "so like what time are we meeting tomorrow for the um the review call",
    },
    {
        "role": "assistant",
        "content": "So what time are we meeting tomorrow for the review call?",
    },
]


# Disfluencies cleanup may delete freely. Deliberately does NOT include hedges
# ("I think", "basically", "actually", ...) — those are content we keep verbatim
# (see SYSTEM_PROMPT). Calibration knob: add whisper disfluencies here if real
# cleanups get rejected. Err on the small side — a false reject only pastes the
# raw transcript (your exact words), never a wrong or invented one.
_FILLERS = frozenset(
    {"um", "uh", "uhh", "uhm", "erm", "er", "hmm", "mm", "mmm", "ah", "like"}
)

# Cleanup drops fillers, not real words. If less than this share of the spoken
# content words survives, the model collapsed the utterance into an answer or
# keyword rather than cleaning it. Knob: raise toward 1.0 to be stricter (more
# raw-transcript fallbacks), lower to tolerate heavier trims.
_MIN_CONTENT_RETENTION = 0.5

# Polarity words are meaning-critical: dropping one is a valid subsequence that
# barely dents the retention ratio yet INVERTS the sentence ("don't cancel" ->
# "cancel"). Retention counts words; it can't see that the one dropped was a
# negator. So negators are mandatory-retain: if the transcript had more of them
# than the output, the meaning changed and we reject regardless of ratio. (A
# token is a negator if it's in this set or is an n't-contraction like "don't".)
_NEGATORS = frozenset(
    {"not", "no", "never", "none", "nothing", "nobody", "neither", "nor",
     "cannot", "without", "nowhere", "hardly", "barely", "scarcely"}
)

_WORD = re.compile(r"[a-z0-9']+")
# ASR and small LLMs disagree on apostrophe style (' vs ’ ‘ ʼ); normalize so a
# contraction is one token on both sides and doesn't read as a substitution.
_APOSTROPHES = str.maketrans({"’": "'", "‘": "'", "ʼ": "'", "′": "'"})


def _tokens(s: str) -> list[str]:
    # Fold apostrophe style and diacritics (café -> cafe) so the same word
    # matches on both sides whether or not the model normalized it.
    s = unicodedata.normalize("NFKD", s.translate(_APOSTROPHES))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return _WORD.findall(s.lower())


def _negator_count(words: list[str]) -> int:
    return sum(1 for w in words if w in _NEGATORS or w.endswith("n't"))


def _is_subsequence(needle: list[str], haystack: list[str]) -> bool:
    """True if every word of `needle` appears in `haystack` in order (a
    deletion-only alignment). Consumes `haystack` left-to-right via an iterator,
    so each match must come after the previous one."""
    it = iter(haystack)
    return all(w in it for w in needle)


def _is_rewrite(raw: str, out: str) -> bool:
    """True if `out` is not a filler-removed version of `raw` — i.e. the model
    rewrote, answered, expanded, substituted, reordered, or collapsed instead of
    cleaning, so `out` must be discarded in favour of the raw transcript.

    The contract is deletion-only, which has an exact structural form that no
    answer can satisfy:

      1. `out` must be an ordered SUBSEQUENCE of `raw` — cleanup only removes
         words, so it can never introduce, reorder, or substitute one. This
         alone rejects every answer that adds or moves words ("Paris", "The
         capital of France is Paris").
      2. `out` must keep every NEGATOR it was given — dropping "not"/"don't" is
         a valid subsequence that inverts meaning ("don't cancel" -> "cancel"),
         which retention (a word count) can't catch. Negators are mandatory.
      3. `out` must RETAIN most of the spoken content words — an answer that
         collapses the utterance to a word you did say ("how do you spell
         restaurant" -> "Restaurant") is a valid subsequence but drops the
         real content. Fillers don't count toward retention; content does.
    """
    raw_w = _tokens(raw)
    out_w = _tokens(out)
    if not out_w:
        return False  # empty handled by caller (falls back to raw)
    # (1) deletion-only: reordering, substituting, or appending breaks this.
    if not _is_subsequence(out_w, raw_w):
        return True
    # (2) meaning-critical polarity: a dropped negator flips the sentence.
    if _negator_count(out_w) < _negator_count(raw_w):
        return True
    # (3) content retention: a collapse-to-keyword is a subsequence but guts the
    # content. Compare non-filler words kept vs non-filler words spoken.
    content = [w for w in raw_w if w not in _FILLERS]
    if content:
        kept = sum(1 for w in out_w if w not in _FILLERS)
        if kept / len(content) < _MIN_CONTENT_RETENTION:
            return True
    return False


# Doubled words that are ordinary English rather than a stutter. Without these,
# "I said that that was fine" would be sent for cleanup it doesn't need.
_LEGITIMATE_DOUBLES = frozenset({"that", "had", "has", "very", "no", "so", "ha", "she", "he"})


def needs_cleanup(raw: str) -> bool:
    """Whether there is anything here for the LLM to remove.

    Cleanup that changes nothing is pure latency, and it is the common case:
    measured over 40 real dictations, the model returned the transcript
    completely unchanged 72% of the time, while costing ~450ms plus ~50-80ms per
    word of generation. On a long utterance that is several seconds of waiting to
    be handed back exactly what whisper already said.

    The test is deliberately narrow — fillers, or a stuttered repeat — because
    those are the only things cleanup is allowed to remove. Everything else the
    model might do (substituting, reordering, answering) is forbidden by the
    prompt and rejected by `_is_rewrite` anyway, so skipping cannot lose it.

    What a skip *can* cost is a punctuation nit the model would have tidied.
    Measured against the same 40 dictations: 4 such cases, one of which was the
    model wrongly stripping a hedge the prompt tells it to keep — so the skip was
    the better answer there. Knob: widen `_FILLERS` to send more utterances for
    cleanup, narrow it to keep more of them instant.
    """
    words = _tokens(raw)
    if any(w in _FILLERS for w in words):
        return True
    return any(a == b and a not in _LEGITIMATE_DOUBLES for a, b in zip(words, words[1:]))


def polish(raw_text: str, style_addendum: str = "", timeout: float = 30.0) -> str:
    """Return cleaned text; on any failure fall back to the raw transcript."""
    raw_text = raw_text.strip()
    if not raw_text:
        return ""
    if not needs_cleanup(raw_text):
        # Nothing to remove — skip the model entirely rather than spend a second
        # confirming that. This is the majority of dictations.
        return raw_text
    system = SYSTEM_PROMPT + ("\n" + style_addendum if style_addendum else "")
    try:
        r = requests.post(
            OLLAMA_URL,
            json={
                "model": MODEL,
                "messages": [
                    {"role": "system", "content": system},
                    *_FEWSHOT,
                    {"role": "user", "content": raw_text},
                ],
                "stream": False,
                "keep_alive": KEEP_ALIVE,
                "options": {"temperature": 0},
            },
            timeout=timeout,
        )
        r.raise_for_status()
        # Flattened for the same reason the transcript is (see
        # stt.flatten_whitespace): a 3B model asked to "fix punctuation" will
        # occasionally return the text wrapped, and that would paste hard line
        # breaks into the middle of a dictated sentence.
        out = " ".join(r.json()["message"]["content"].split())
        # ponytail: strip accidental wrapping quotes, the only 3B misfire seen in testing
        if len(out) > 1 and out[0] == out[-1] and out[0] in "\"'":
            out = out[1:-1]
        # Structural safety net: if the model answered/rewrote instead of cleaning
        # (e.g. it "helpfully" answered a dictated question), paste the raw words
        # the user actually spoke, never the model's invention.
        if _is_rewrite(raw_text, out):
            print(f"[simo] polish rejected as a rewrite, using raw transcript: {out!r}", flush=True)
            return raw_text
        return out or raw_text
    except Exception as e:
        # Never block the paste on a polish failure — but never hide it either.
        # Silent here meant a stopped Ollama or an evicted model degraded every
        # dictation to raw output indefinitely, with no signal anywhere.
        print(f"[simo] polish unavailable ({type(e).__name__}: {e}) — pasting raw transcript", flush=True)
        return raw_text


if __name__ == "__main__":
    import sys
    import time
    DEFAULT = "Um, so basically, I think we should meet at 3pm tomorrow to discuss the, you know, the quarterly report."
    sample = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    t0 = time.time()
    cleaned = polish(sample)
    dt = (time.time() - t0) * 1000
    print(f"RAW:     {sample}")
    print(f"CLEAN:   {cleaned}")
    print(f"latency: {dt:.0f}ms")
    # self-check (default sample only): fillers gone, content kept, no commentary
    low = cleaned.lower()
    assert "um" not in low.split() and "uh" not in low.split(), "filler survived"
    assert "you know" not in low, "filler survived"
    if sample == DEFAULT:
        assert "quarterly report" in low, "content lost"
        assert "3pm" in low or "3 pm" in low or "three pm" in low, "time lost"
    print("polish self-check OK")
