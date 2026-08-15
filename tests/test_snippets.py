"""Snippets: say a phrase you chose, get the text you saved.

The interesting cases are all about *not* firing when you didn't mean it. A
snippet that misses is a small annoyance; a snippet that fires inside an ordinary
sentence corrupts what you actually said, silently, in whatever app you were
typing into.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import snippets  # noqa: E402

MAP = {
    "my email address": "lucas@example.com",
    "my email": "wrong@example.com",
    "my linkedin": "https://linkedin.com/in/lucas",
}


def test_a_saved_phrase_becomes_its_text():
    assert snippets.expand("Send it to my email address.", MAP) == "Send it to lucas@example.com."


def test_matching_ignores_how_the_transcript_capitalised_it():
    """The cleanup pass capitalises sentences, so the stored phrase and the spoken
    one rarely agree on case. Matching on case would make snippets fire only in
    the middle of sentences, which is worse than never firing at all."""
    assert snippets.expand("My email address is there.", MAP).startswith("lucas@example.com")


def test_the_longer_phrase_wins():
    """"my email address" and "my email" both match the same opening words.
    Offering the shorter one first would leave "lucas@example.com" unreachable."""
    assert "lucas@example.com" in snippets.expand("my email address", MAP)
    assert "wrong@example.com" not in snippets.expand("my email address", MAP)


def test_a_phrase_inside_a_longer_word_does_not_fire():
    """Whole phrases only. Otherwise dictating "check my emails" would silently
    rewrite part of an ordinary sentence."""
    assert snippets.expand("check my emails later", MAP) == "check my emails later"


def test_an_expansion_containing_another_phrase_does_not_cascade():
    """One pass over the original text. A snippet whose replacement happens to
    contain another trigger must not chain, or a pair of innocuous snippets can
    run away between them."""
    chain = {"alpha": "beta", "beta": "gamma"}
    assert snippets.expand("say alpha", chain) == "say beta"


def test_a_phrase_that_expands_to_itself_terminates():
    """The self-referential case, which a naive replace-until-stable loop hangs on
    and which a user can create by accident."""
    assert snippets.expand("say loop", {"loop": "loop and loop"}) == "say loop and loop"


def test_a_runaway_expansion_pastes_the_transcript_unchanged(capsys):
    """Better to paste what you said than to drop 100KB into someone's editor.
    Pasting is not undoable in every app, so the failure has to be the safe one."""
    huge = {"boom": "x" * (snippets.MAX_EXPANDED_CHARS + 1)}
    assert snippets.expand("say boom", huge) == "say boom"
    assert "over the" in capsys.readouterr().out, "a silently skipped expansion is a bug report"


def test_nothing_configured_changes_nothing():
    assert snippets.expand("just some words", {}) == "just some words"


@pytest.mark.parametrize(
    "phrase,ok",
    [
        ("my email address", True),
        ("Lucas's number", True),
        ("two-factor code", True),
        ("x", True),
        ("", False),
        ("   ", False),
        ("...", False),
        ("@handle", False),  # word boundaries can never match a leading symbol
    ],
)
def test_only_phrases_that_can_actually_match_are_accepted(phrase, ok):
    assert snippets.is_valid_trigger(phrase) is ok


def test_snippets_survive_a_round_trip_through_the_store(tmp_path, monkeypatch):
    import engine.store as store

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "s.db")

    assert store.snippet_add("my email address", "lucas@example.com") is True
    assert store.snippet_add("...", "nope") is False, "an unmatchable phrase must be refused"
    assert store.snippet_add("my email address", "") is False, "an empty replacement is not a snippet"

    assert store.snippet_map() == {"my email address": "lucas@example.com"}

    # Re-adding the same phrase updates it rather than failing on the unique index
    assert store.snippet_add("My Email Address", "new@example.com") is True
    assert list(store.snippet_map().values()) == ["new@example.com"]

    store.snippet_delete(store.snippets()[0]["id"])
    assert store.snippet_map() == {}
