"""Polish pass: send raw transcript to local Ollama, get cleaned text back."""
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
]


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
