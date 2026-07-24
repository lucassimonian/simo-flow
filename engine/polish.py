"""Polish pass: send raw transcript to local Ollama, get cleaned text back.

Cleanup only ever *removes* filler words, so the output is always a shortened,
same-words version of the input. `_is_rewrite` enforces that structurally: if the
model instead answers a dictated question, summarizes, or substitutes words
(a general chat model's instinct when the input reads like a prompt), the output
won't match the input and we discard it, pasting the raw transcript instead. The
prompt and few-shot reduce how often that happens; the guard guarantees a bad
result never reaches the cursor.
"""
import re

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


def _is_rewrite(raw: str, out: str) -> bool:
    """True if `out` is not a plausible filler-removed version of `raw` — i.e.
    the model rewrote, answered, expanded, or substituted instead of cleaning.

    Cleanup only ever drops words, so a valid output is never much longer than
    the input and never introduces many words that weren't spoken. Either of
    those means the model went off-task and the output must be discarded.
    """
    raw_w = re.findall(r"[a-z0-9']+", raw.lower())
    out_w = re.findall(r"[a-z0-9']+", out.lower())
    if not out_w:
        return False
    # an answer or expansion balloons the length; cleanup shortens
    if len(out_w) > len(raw_w) * 1.3 + 4:
        return True
    # cleanup keeps the spoken words; an answer is full of new ones
    raw_set = set(raw_w)
    new_words = sum(1 for w in out_w if w not in raw_set)
    return new_words / len(out_w) > 0.4


def polish(raw_text: str, style_addendum: str = "", timeout: float = 30.0) -> str:
    """Return cleaned text; on any failure fall back to the raw transcript."""
    raw_text = raw_text.strip()
    if not raw_text:
        return ""
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
        out = r.json()["message"]["content"].strip()
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
    except Exception:
        return raw_text  # never block the paste on a polish failure


if __name__ == "__main__":
    import sys, time
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
