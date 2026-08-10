"""Stop developer tooling from typing into the user's window.

This exists because a latency benchmark called `inject.paste_text()` to time the
paste path, which posted twelve real ⌘V keystrokes into whatever window happened
to be focused — the user's terminal. Their prompt filled with the test sentence
twelve times over and it looked exactly like the app malfunctioning.

Timing the paste path does not require *delivering* the keystroke. Any tool that
measures or exercises `engine.inject` must call `stub_input()` first, so the four
CGEvents are counted instead of posted.

The one deliberate exception is `tools/rehearse_paste.py`, which exists precisely
to verify that a real paste lands in the right window, and which the user runs
knowingly via `./simo rehearse`.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import inject  # noqa: E402


def stub_input() -> list[tuple]:
    """Neuter real key posting. Returns the list events are recorded into.

    Also stubs the clipboard so a tool can't leave the user's pasteboard holding
    test text if it crashes between staging and restoring.
    """
    posted: list[tuple] = []
    clipboard = {"value": inject._get_clipboard()}

    inject._post_key = lambda keycode, down, flags: posted.append((keycode, down, flags))
    inject._set_clipboard = lambda text: clipboard.__setitem__("value", text)
    inject._get_clipboard = lambda: clipboard["value"]
    inject._clear_clipboard = lambda: clipboard.__setitem__("value", None)
    print("[tool] real keyboard/clipboard output disabled — nothing will be typed", flush=True)
    return posted
