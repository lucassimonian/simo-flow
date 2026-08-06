"""Property-based tests for the dictation-integrity guard in engine.polish.

`_is_rewrite` is the last thing standing between a 3B chat model's instinct to be
helpful and text landing at the user's cursor. It already shipped one
meaning-inverting bug (v2.0.2) that a suite of hand-picked examples passed
happily — a dropped negator ("don't cancel" -> "cancel") is a legal subsequence
that barely moves the retention ratio.

Hand-written examples can only test the cases someone thought of, which is
exactly the wrong shape of coverage for a guard whose job is to survive inputs
nobody anticipated. These state the *contract* and let Hypothesis attack it:

  accept  — output is the input with only filler words deleted
  reject  — anything that adds, substitutes, reorders, drops a negator, or
            collapses the utterance

Run:  ./.venv/bin/python -m pytest tests/test_polish_properties.py -q
"""
import string
import sys
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.polish import _FILLERS, _NEGATORS, _is_rewrite  # noqa: E402

# Content words: real words the cleanup must preserve. Excludes fillers (which
# may be deleted) and negators (which have their own mandatory-retain rule), so
# each property tests one behaviour rather than a mix.
_content_word = st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=8).filter(
    lambda w: w not in _FILLERS and w not in _NEGATORS and not w.endswith("n't")
)
_filler = st.sampled_from(sorted(_FILLERS))
_negator = st.sampled_from(sorted(_NEGATORS))


def _distinct(words: list[str]) -> list[str]:
    """Order-preserving dedupe. Repeated words make subsequence reasoning
    ambiguous — "a b a" genuinely contains "a a" — so properties about ordering
    and deletion are stated over distinct words only."""
    seen, out = set(), []
    for w in words:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out


# --------------------------------------------------------------------------
# accept: valid cleanups must never be rejected (a false reject silently
# downgrades every dictation to raw output, so this is not a cosmetic property)
# --------------------------------------------------------------------------
@given(st.lists(_content_word, min_size=1, max_size=12))
@settings(max_examples=300)
def test_unchanged_output_is_never_a_rewrite(words):
    """The model echoing the input is a no-op cleanup, always legal."""
    text = " ".join(words)
    assert _is_rewrite(text, text) is False


@given(
    st.lists(_content_word, min_size=1, max_size=10),
    st.lists(_filler, min_size=1, max_size=6),
)
@settings(max_examples=400)
def test_removing_only_fillers_is_never_a_rewrite(content, fillers):
    """Interleave fillers into real speech, delete exactly the fillers: that is
    the function's entire job and must always be accepted."""
    content = _distinct(content)
    raw_words = []
    for i, word in enumerate(content):
        if i < len(fillers):
            raw_words.append(fillers[i])
        raw_words.append(word)
    raw_words.extend(fillers[len(content):])
    assert _is_rewrite(" ".join(raw_words), " ".join(content)) is False


@given(st.lists(_content_word, min_size=1, max_size=10), _negator)
@settings(max_examples=300)
def test_keeping_the_negator_is_never_a_rewrite(content, negator):
    """Negators are mandatory-retain, not forbidden — keeping one is fine."""
    content = _distinct(content)
    raw = " ".join([*content, negator])
    assert _is_rewrite(raw, raw) is False


# --------------------------------------------------------------------------
# reject: the four ways a chat model corrupts a transcript
# --------------------------------------------------------------------------
@given(st.lists(_content_word, min_size=1, max_size=10), _content_word)
@settings(max_examples=400)
def test_adding_a_word_is_always_a_rewrite(words, intruder):
    """Cleanup only deletes. A word that was never spoken cannot appear —
    this is what rejects an answered question ("Paris") outright."""
    words = _distinct(words)
    intruder = intruder + "zzq"  # guaranteed absent from `words`
    raw = " ".join(words)
    assert _is_rewrite(raw, " ".join([*words, intruder])) is True
    assert _is_rewrite(raw, " ".join([intruder, *words])) is True


@given(st.lists(_content_word, min_size=2, max_size=8))
@settings(max_examples=400)
def test_reordering_is_always_a_rewrite(words):
    """Deletion preserves order, so a reversal can never be a valid cleanup."""
    words = _distinct(words)
    if len(words) < 2:
        return  # a single word reversed is itself
    assert _is_rewrite(" ".join(words), " ".join(reversed(words))) is True


@given(st.lists(_content_word, min_size=1, max_size=10), _negator)
@settings(max_examples=400)
def test_dropping_a_negator_is_always_a_rewrite(content, negator):
    """The v2.0.2 bug, as a law: losing polarity inverts meaning even though the
    result is a perfectly legal subsequence with high word retention."""
    content = _distinct(content)
    raw = " ".join([*content, negator])
    assert _is_rewrite(raw, " ".join(content)) is True


@given(st.lists(_content_word, min_size=5, max_size=14))
@settings(max_examples=400)
def test_collapsing_to_one_word_is_always_a_rewrite(words):
    """"how do you spell restaurant" -> "Restaurant" is a subsequence and still
    destroys the utterance. Retention has to catch it."""
    words = _distinct(words)
    if len(words) < 3:
        return  # too short for a 1-word answer to count as a collapse
    assert _is_rewrite(" ".join(words), words[-1]) is True


# --------------------------------------------------------------------------
# structural invariants
# --------------------------------------------------------------------------
@given(st.lists(_content_word, min_size=1, max_size=10))
@settings(max_examples=200)
def test_empty_output_is_not_flagged_so_the_caller_can_fall_back(words):
    """polish() treats empty as "use the raw transcript"; the guard must not
    claim a rewrite and send it down a different path."""
    assert _is_rewrite(" ".join(words), "") is False


@given(st.lists(_content_word, min_size=1, max_size=8))
@settings(max_examples=200)
def test_guard_is_deterministic(words):
    """Same inputs, same verdict — every time. A guard that flickers would make
    dictation non-reproducible and impossible to debug."""
    raw, out = " ".join(words), " ".join(words[:-1]) if len(words) > 1 else " ".join(words)
    assert _is_rewrite(raw, out) == _is_rewrite(raw, out)
