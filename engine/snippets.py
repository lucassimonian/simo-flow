"""Snippets: say a phrase you chose, get the text you saved.

"my email address" becomes your address. "my LinkedIn" becomes your profile URL.
The things you retype constantly and always get slightly wrong out loud.

Every snippet lives in ~/.simo-flow.db, which is outside this repository and
ignored by git. Nobody who clones this project gets anybody's snippets, and no
example here uses a real address — a documentation placeholder that happens to be
someone's actual contact details is how personal data ends up in a public repo
and stays there.

Deliberately applied *after* the polish pass, never inside it. `polish` guarantees
its output is your own words with fillers removed — an ordered subsequence of what
you said — and a snippet does the opposite: it puts in words you did not speak.
Running expansion inside that pass would either be rejected by the integrity guard
or, worse, force the guard to be loosened for everything.

Applied in Exact mode too. Exact means the cleanup model may not reword you; it
does not mean the shortcuts you deliberately configured stop working.
"""
import re

# A cap on how far one utterance can inflate. Snippets are addresses, links and
# short boilerplate; anything approaching this is a runaway rule rather than a
# shortcut, and pasting 100KB into someone's editor is not a recoverable mistake.
# ponytail: a flat limit, not a per-snippet budget. Raise it if anyone has a
# legitimate snippet that big.
MAX_EXPANDED_CHARS = 20_000


def expand(text: str, snippets: dict[str, str]) -> str:
    """Replace each spoken trigger phrase with its saved text.

    Matching is case-insensitive and on whole phrases only, so "my email" never
    fires inside "my emails". Longer triggers are tried first, so "my work email"
    wins over "my email" rather than losing a race to it.

    One pass over the original string: replacements are never themselves
    rescanned, so a snippet whose text contains another trigger cannot cascade,
    and a snippet that mentions its own trigger cannot loop.
    """
    if not text or not snippets:
        return text

    usable = {t.strip().lower(): v for t, v in snippets.items() if t.strip() and v}
    if not usable:
        return text

    # Longest first: alternation is first-match-wins, so "my work email" has to be
    # offered before "my email" or the shorter one always claims the prefix.
    ordered = sorted(usable, key=len, reverse=True)
    pattern = re.compile(
        r"\b(?:" + "|".join(re.escape(t) for t in ordered) + r")\b", re.IGNORECASE
    )

    expanded = pattern.sub(lambda m: usable[m.group(0).lower()], text)
    if len(expanded) > MAX_EXPANDED_CHARS:
        print(
            f"[simo] snippet expansion would produce {len(expanded)} characters — "
            f"over the {MAX_EXPANDED_CHARS} limit, pasting the transcript unchanged",
            flush=True,
        )
        return text
    return expanded


def is_valid_trigger(phrase: str) -> bool:
    """Whether a phrase can be matched reliably when spoken.

    Word characters only, because matching uses word boundaries: a trigger that
    starts or ends with punctuation can never match, and one made only of
    punctuation would match nothing at all. Rejecting these at the point of entry
    beats saving a snippet that silently never fires.
    """
    phrase = phrase.strip()
    return bool(phrase) and bool(re.fullmatch(r"[\w][\w '-]*[\w]|[\w]", phrase))


if __name__ == "__main__":
    # self-check: the behaviours worth getting wrong
    s = {"my email address": "lucas@example.com", "my email": "other@example.com"}
    assert expand("Send it to my email address.", s) == "Send it to lucas@example.com."
    assert expand("MY EMAIL ADDRESS please", s) == "lucas@example.com please"
    assert expand("check my emails", s) == "check my emails", "matched inside a longer word"
    assert expand("hello", {}) == "hello"
    # a snippet mentioning another trigger must not cascade
    assert expand("say alpha", {"alpha": "beta", "beta": "gamma"}) == "say beta"
    assert is_valid_trigger("my email address") and not is_valid_trigger("...")
    print("snippets self-check OK")
