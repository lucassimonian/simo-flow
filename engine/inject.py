"""Inject text at the cursor: clipboard set -> Cmd+V -> restore previous clipboard."""
import time

from AppKit import NSPasteboard, NSPasteboardTypeString
from Quartz import (
    CGEventCreateKeyboardEvent,
    CGEventPost,
    CGEventSetFlags,
    kCGEventFlagMaskCommand,
    kCGHIDEventTap,
)

KEY_V = 9  # kVK_ANSI_V
RESTORE_DELAY = 0.3  # let the paste land before restoring clipboard


def _get_clipboard() -> str | None:
    return NSPasteboard.generalPasteboard().stringForType_(NSPasteboardTypeString)


def _set_clipboard(text: str) -> None:
    pb = NSPasteboard.generalPasteboard()
    pb.clearContents()
    pb.setString_forType_(text, NSPasteboardTypeString)


def _press_cmd_v() -> None:
    for down in (True, False):
        ev = CGEventCreateKeyboardEvent(None, KEY_V, down)
        CGEventSetFlags(ev, kCGEventFlagMaskCommand)
        CGEventPost(kCGHIDEventTap, ev)


def paste_text(text: str, restore: bool = True) -> None:
    """Paste text at the cursor of the frontmost app; restore old clipboard after."""
    if not text:
        return
    previous = _get_clipboard() if restore else None
    _set_clipboard(text)
    time.sleep(0.05)  # let the pasteboard settle before the keystroke
    _press_cmd_v()
    if restore and previous is not None:
        time.sleep(RESTORE_DELAY)
        _set_clipboard(previous)


if __name__ == "__main__":
    # self-check: clipboard save/set/restore round-trip (no keystroke sent)
    sentinel = "simo-flow-selfcheck-§"
    before = _get_clipboard()
    _set_clipboard(sentinel)
    assert _get_clipboard() == sentinel, "clipboard set failed"
    if before is not None:
        _set_clipboard(before)
        assert _get_clipboard() == before, "clipboard restore failed"
    print("inject clipboard self-check OK (paste keystroke not exercised headlessly)")
