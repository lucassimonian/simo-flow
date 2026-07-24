"""Inject text at the cursor: clipboard set -> Cmd+V -> restore previous clipboard.

Paste is serialized by the single pipeline worker (see engine.__main__), so the
clipboard save/set/restore critical section here is never entered concurrently.
"""
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


def _clear_clipboard() -> None:
    NSPasteboard.generalPasteboard().clearContents()


def _press_cmd_v() -> None:
    for down in (True, False):
        ev = CGEventCreateKeyboardEvent(None, KEY_V, down)
        CGEventSetFlags(ev, kCGEventFlagMaskCommand)
        CGEventPost(kCGHIDEventTap, ev)


def paste_text(text: str, restore: bool = True) -> None:
    """Paste text at the cursor of the frontmost app; restore old clipboard after.

    If the previous clipboard held non-text content (an image, a file, rich
    text), we can't read it back to restore it — so instead of leaving the
    dictated text sitting on the pasteboard (where clipboard-history tools would
    capture it), we clear it. Losing a non-text clipboard is the lesser evil
    versus leaking dictated text.
    """
    if not text:
        return
    previous = _get_clipboard() if restore else None
    # non-text clipboard (image/file/rich) can't be read back to restore
    had_nontext = restore and previous is None and _has_any_content()
    _set_clipboard(text)
    time.sleep(0.05)  # let the pasteboard settle before the keystroke
    _press_cmd_v()
    if restore:
        time.sleep(RESTORE_DELAY)
        if previous is not None:
            _set_clipboard(previous)
        elif had_nontext:
            # unrecoverable previous content; clear rather than leave dictated
            # text lingering on the pasteboard for clipboard-history tools
            _clear_clipboard()


def _has_any_content() -> bool:
    """True if the pasteboard holds anything at all (any type)."""
    types = NSPasteboard.generalPasteboard().types()
    return bool(types) and len(types) > 0


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
