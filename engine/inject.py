"""Inject text at the cursor: clipboard set -> Cmd+V -> restore previous clipboard.

The paste happens up to ~2s after the user stopped speaking (whisper + polish),
and the world does not hold still for that long. Three things can change
underneath us, and each used to be a real bug:

  * **The focused window.** Click elsewhere while transcribing and the text
    landed in whatever was frontmost at the end. `capture_focus()` is called on
    fn-down and the owning app is re-activated before pasting, so text lands
    where you started talking.
  * **The clipboard.** We borrow it and put the original back. If the user hit
    Cmd+C in that window, the blind restore overwrote their fresh copy.
    `changeCount` (bumped by every write from any process) tells us whether the
    pasteboard is still ours to restore.
  * **Accessibility permission.** macOS revokes it silently, often after an OS
    update, and then swallows every synthetic event. Dictation appeared to work
    while pasting nothing, forever. We refuse to proceed untrusted, and say so.

Re-activating the *app* is deliberately all we do — we don't chase the specific
text field. Every well-behaved app restores its own last-focused field when its
window comes forward, and poking at AX focus hierarchies is unreliable across
apps.

Paste is serialized by the single pipeline worker (see engine.__main__), so the
clipboard critical section here is never entered concurrently.
"""
import time

from AppKit import (
    NSData,
    NSPasteboard,
    NSPasteboardItem,
    NSPasteboardTypeString,
    NSRunningApplication,
    NSWorkspace,
)
from ApplicationServices import AXIsProcessTrusted
from Quartz import (
    CGEventCreateKeyboardEvent,
    CGEventPost,
    CGEventSetFlags,
    kCGEventFlagMaskCommand,
    kCGHIDEventTap,
)

KEY_V = 9  # kVK_ANSI_V — the position of "v" on a US layout, and the fallback
KEY_CMD = 55  # kVK_Command


def _load_keyboard_layout():
    """Carbon's Text Input Source API, for asking which key types a character.

    Virtual key codes are *positions*, not letters. kVK_ANSI_V is wherever "v"
    sits on a US keyboard — which on Dvorak is ".", and on AZERTY is something
    else again. Posting it blindly sends Cmd+whatever-lives-there, so paste
    silently did the wrong thing for anyone not on QWERTY.

    Returns None if the framework can't be loaded, in which case the US position
    is used and behaviour is exactly what it was before.
    """
    try:
        import ctypes
        import ctypes.util

        carbon = ctypes.cdll.LoadLibrary(
            ctypes.util.find_library("Carbon")
            or "/System/Library/Frameworks/Carbon.framework/Carbon"
        )
        cf = ctypes.cdll.LoadLibrary(
            ctypes.util.find_library("CoreFoundation")
            or "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        carbon.TISCopyCurrentKeyboardLayoutInputSource.restype = ctypes.c_void_p
        carbon.TISGetInputSourceProperty.restype = ctypes.c_void_p
        carbon.TISGetInputSourceProperty.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        carbon.LMGetKbdType.restype = ctypes.c_uint8
        carbon.UCKeyTranslate.restype = ctypes.c_int32
        carbon.UCKeyTranslate.argtypes = [
            ctypes.c_void_p, ctypes.c_uint16, ctypes.c_uint16, ctypes.c_uint32,
            ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_uint16),
        ]
        cf.CFDataGetBytePtr.restype = ctypes.c_void_p
        cf.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        prop = ctypes.c_void_p.in_dll(carbon, "kTISPropertyUnicodeKeyLayoutData")
        return carbon, cf, prop, ctypes
    except Exception as e:  # pragma: no cover - only on a broken/odd system
        print(f"[simo] keyboard layout unavailable ({e}) — assuming a US layout", flush=True)
        return None


_LAYOUT = _load_keyboard_layout()

_UC_KEY_ACTION_DISPLAY = 3
_UC_NO_DEAD_KEYS = 1
_MAX_VIRTUAL_KEYCODE = 128  # the whole ANSI/ISO/JIS range; nothing useful sits above


def _keycode_for_character(char: str) -> int | None:
    """The physical key that types `char` on the layout in use right now.

    Read live rather than cached: switching input source is something people do
    mid-sentence, and the whole scan costs microseconds because it stops at the
    first match — on a US layout, ten iterations.
    """
    if _LAYOUT is None:
        return None
    carbon, cf, prop, ctypes = _LAYOUT
    source = None
    try:
        source = carbon.TISCopyCurrentKeyboardLayoutInputSource()
        if not source:
            return None
        data = carbon.TISGetInputSourceProperty(source, prop)
        if not data:
            return None  # some input sources (handwriting, IMEs) expose no layout
        layout = cf.CFDataGetBytePtr(data)
        if not layout:
            return None
        kbd_type = carbon.LMGetKbdType()
        for code in range(_MAX_VIRTUAL_KEYCODE):
            dead = ctypes.c_uint32(0)
            length = ctypes.c_ulong(0)
            buf = (ctypes.c_uint16 * 4)()
            err = carbon.UCKeyTranslate(
                layout, code, _UC_KEY_ACTION_DISPLAY, 0, kbd_type, _UC_NO_DEAD_KEYS,
                ctypes.byref(dead), 4, ctypes.byref(length), buf,
            )
            if err == 0 and length.value == 1 and chr(buf[0]) == char:
                return code
        return None
    except Exception as e:
        print(f"[simo] could not read the keyboard layout ({e})", flush=True)
        return None
    finally:
        if source:
            cf.CFRelease(source)


def _paste_keycode() -> int:
    """The key to press with Command to paste, on this user's keyboard."""
    return _keycode_for_character("v") or KEY_V

# ponytail: a flat cap, not a streaming copy. Holding the pasteboard in memory for
# ~300ms is fine for documents and screenshots; a 4K video or a huge file promise
# is not worth the RAM, and above this we degrade to the old behaviour (clear it)
# and say so. Raise it if someone hits the limit with something they cared about.
MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024

RESTORE_DELAY = 0.3  # how long the target app gets to consume the paste
ACTIVATE_DELAY = 0.35  # ceiling on waiting for the re-activated app to take focus
_POLL = 0.005  # 5ms — one poll costs ~10µs, so this is free next to a sleep


def _wait_until(predicate, timeout: float) -> bool:
    """Poll `predicate` until true, or `timeout` elapses. Returns whether it came true.

    Deliberately not a fixed sleep. A fixed sleep is a guess that is simultaneously
    too slow in the common case and too short in the bad one: the pasteboard is
    usually readable inside a millisecond, while a cold app being activated can
    need a few hundred. Waiting on the real condition is faster *and* more robust.
    """
    deadline = time.monotonic() + timeout
    while True:
        # Checked before any sleep: an already-true condition costs nothing,
        # which is the common case for both call sites.
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL)


# ---- thin wrappers over macOS -------------------------------------------
# Each of these is one macOS call and nothing else. Keeping them separate is what
# makes the orchestration in paste_text() testable without a GUI.
def _get_clipboard() -> str | None:
    return NSPasteboard.generalPasteboard().stringForType_(NSPasteboardTypeString)


def _set_clipboard(text: str) -> None:
    pb = NSPasteboard.generalPasteboard()
    pb.clearContents()
    pb.setString_forType_(text, NSPasteboardTypeString)


def _clear_clipboard() -> None:
    NSPasteboard.generalPasteboard().clearContents()


def _snapshot_pasteboard() -> list[list[tuple[str, bytes]]] | None:
    """Every item on the pasteboard, as its full set of (type, data) pairs.

    Reading the pasteboard back as a *string* is lossy in two different ways, and
    both destroyed user data. An image or file reference has no string form at
    all, so it came back as None and was cleared. Styled text does have one, so it
    came back — as plain text, silently stripped of its formatting.

    Capturing the raw representations avoids interpreting the content at all, so
    anything the user copied survives the round trip unchanged.

    Returns None if the pasteboard is larger than we are willing to hold in
    memory, in which case the caller falls back to clearing it.
    """
    try:
        pb = NSPasteboard.generalPasteboard()
        items = pb.pasteboardItems()
        if items is None:
            return []
        snapshot: list[list[tuple[str, bytes]]] = []
        total = 0
        for item in items:
            pairs: list[tuple[str, bytes]] = []
            for uti in item.types() or ():
                data = item.dataForType_(uti)
                if data is None:
                    continue  # a promised type whose provider declined; nothing to keep
                # Size is checked *before* materialising into Python bytes. Testing
                # afterwards would bound the check but not the allocation — a
                # multi-gigabyte file promise would already be resident by then,
                # which is the one thing the cap exists to prevent.
                total += int(data.length())
                if total > MAX_SNAPSHOT_BYTES:
                    print(
                        f"[simo] clipboard is over {MAX_SNAPSHOT_BYTES // 1_000_000}MB — "
                        "it cannot be preserved across this dictation",
                        flush=True,
                    )
                    return None
                pairs.append((uti, bytes(data)))
            if pairs:
                snapshot.append(pairs)
        return snapshot
    except Exception as e:
        # Must never raise. paste_text() promises to always return, and its caller
        # only saves the transcript *after* it returns — so an exception escaping
        # here would cost the user the words they just spoke, not merely their
        # clipboard. Degrade to "nothing to restore", which clears rather than
        # preserves: worse than succeeding, far better than losing the dictation.
        print(f"[simo] could not read the clipboard ({type(e).__name__}: {e})", flush=True)
        return None


def _restore_pasteboard(snapshot: list[list[tuple[str, bytes]]]) -> bool:
    """Put a snapshot back. False if it could not be done, for any reason."""
    try:
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        rebuilt = []
        for pairs in snapshot:
            item = NSPasteboardItem.alloc().init()
            for uti, payload in pairs:
                item.setData_forType_(NSData.dataWithBytes_length_(payload, len(payload)), uti)
            rebuilt.append(item)
        return bool(pb.writeObjects_(rebuilt))
    except Exception as e:
        # Same contract as above, and the same reason: this runs after the paste
        # has landed but before the caller records the transcript.
        print(f"[simo] could not rebuild the clipboard ({type(e).__name__}: {e})", flush=True)
        return False


def _has_any_content() -> bool:
    """True if the pasteboard holds anything at all (any type)."""
    return bool(NSPasteboard.generalPasteboard().types())


def _change_count() -> int:
    """AppKit bumps this on every pasteboard write, from any process. Comparing
    it before and after our own write is how we detect a third-party copy."""
    return NSPasteboard.generalPasteboard().changeCount()


def _is_trusted() -> bool:
    """Accessibility permission, without prompting. The prompting variant
    (AXIsProcessTrustedWithOptions) would fire a system dialog on every paste."""
    return bool(AXIsProcessTrusted())


def _capture_focus() -> dict | None:
    """The app owning the frontmost window, as {pid, bundle_id}.

    NSWorkspace rather than an AX focused-element walk: we only ever re-activate
    the *application*, so the extra traversal would buy nothing and would need
    Accessibility permission just to record where we were.
    """
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    if app is None:
        return None
    return {"pid": int(app.processIdentifier()), "bundle_id": app.bundleIdentifier()}


def _activate_pid(pid: int) -> bool:
    """Bring the app owning `pid` forward. False if macOS refused.

    macOS 14 deprecated activateWithOptions: for a cooperative pattern — the
    caller yields activation rights to the target, then the target activates.
    Probed with respondsToSelector: so this stays correct either side of that
    change; pyobjc does not expose `activate` on every build.
    """
    target = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if target is None:
        return False
    current = NSRunningApplication.currentApplication()
    if current is not None and current.respondsToSelector_("yieldActivationToApplication:"):
        current.yieldActivationToApplication_(target)
        if target.respondsToSelector_("activate"):
            return bool(target.activate())
    # NSApplicationActivateIgnoringOtherApps = 1 << 1
    return bool(target.activateWithOptions_(2))


def _post_key(keycode: int, down: bool, flags: int) -> None:
    ev = CGEventCreateKeyboardEvent(None, keycode, down)
    CGEventSetFlags(ev, flags)
    CGEventPost(kCGHIDEventTap, ev)


def _press_cmd_v() -> None:
    """The full four-event sequence, not just the V pair.

    Chromium — so Slack, VS Code, Discord, Notion, every Electron app — tracks
    modifier state from the Cmd key's own flagsChanged event. Post only
    V-with-Cmd-flag and those apps intermittently drop the paste.

    The final event — Cmd going up — must carry NO flags. Sending "Command
    released" while the Command flag is still set is self-contradictory, and macOS
    latches it: `CGEventSourceFlagsState` then reports Command as held after every
    dictation, so the user's next keystroke is read as a shortcut. Real hardware
    clears the flag on release, and so must we. Verified by reading the live
    modifier state before and after.
    """
    cmd = kCGEventFlagMaskCommand
    v = _paste_keycode()  # position of "v" on *this* layout, not on a US one
    _post_key(KEY_CMD, True, cmd)
    _post_key(v, True, cmd)
    _post_key(v, False, cmd)
    _post_key(KEY_CMD, False, 0)


# ---- public API ---------------------------------------------------------
def _same_app(a: dict | None, b: dict | None) -> bool:
    """Whether two focus snapshots are the same running application.

    Bundle id is compared as well as pid, because a pid alone is not an identity.
    macOS reuses pids, and up to two seconds pass between the snapshot and the
    paste: if the target app quits in that window and its pid is reissued, a
    pid-only match would activate an unrelated process and paste the user's words
    into it — reintroducing the exact bug this module exists to prevent.

    Both bundle ids being None (a process without one) falls back to pid equality;
    that is the best available answer, not a silent guess.
    """
    if not a or not b:
        return False
    if a.get("pid") != b.get("pid"):
        return False
    return a.get("bundle_id") == b.get("bundle_id")


def _describe(focus: dict) -> str:
    """Human-readable focus target for log lines."""
    return f"{focus.get('bundle_id') or 'pid ' + str(focus.get('pid'))} (pid {focus.get('pid')})"


def capture_focus() -> dict | None:
    """Record where the user is at the moment they start dictating.

    Call on fn-down and hand the result to paste_text(). One AppKit call, so it
    stays off the latency path.
    """
    return _capture_focus()


def paste_text(
    text: str,
    focus: dict | None = None,
    restore: bool = True,
    on_pasted=None,
) -> bool:
    """Paste `text` at the cursor. True only if the paste was delivered.

    `focus` is a snapshot from capture_focus() taken when recording began; the
    owning app is re-activated first so the text lands where the user started
    rather than wherever focus drifted. Every failure path returns False having
    left the user's clipboard exactly as it was.

    `on_pasted` fires the instant the keystroke is delivered — before the restore
    wait. That is the moment the user actually sees their text, and it is the only
    honest thing to call the latency: timing the whole call instead adds ~300ms of
    post-paste clipboard housekeeping the user never waits for.
    """
    if not text:
        return False

    # Untrusted means macOS discards our events without telling us. Bailing here
    # turns a permanent silent failure into a visible one.
    if not _is_trusted():
        print(
            "[simo] paste refused — Accessibility permission is not granted. "
            "Grant it in System Settings > Privacy & Security > Accessibility, "
            "then restart Simo Flow.",
            flush=True,
        )
        return False

    # Return to the window the user was in. Done before any clipboard write, so a
    # refusal (target app quit, trust revoked mid-flight) costs them nothing.
    if focus and focus.get("pid") and not _same_app(_capture_focus(), focus):
        if not _activate_pid(focus["pid"]):
            print(
                f"[simo] paste aborted — could not activate {_describe(focus)} "
                "(did the app quit during transcription?); clipboard untouched",
                flush=True,
            )
            return False
        # Continue the instant the app is genuinely frontmost, rather than after a
        # fixed guess. Pasting before focus lands sends Cmd+V to the wrong window —
        # the bug this whole path exists to prevent.
        if not _wait_until(lambda: _same_app(_capture_focus(), focus), ACTIVATE_DELAY):
            print(
                f"[simo] paste aborted — {_describe(focus)} did not come to the "
                f"foreground within {ACTIVATE_DELAY * 1000:.0f}ms; clipboard untouched",
                flush=True,
            )
            return False

    # Snapshot every representation rather than reading the clipboard as a string:
    # an image has no string form and used to be cleared, and styled text has one
    # that silently drops its formatting. None means "too large to hold" — see
    # MAX_SNAPSHOT_BYTES — and is the one case where clearing is still the answer.
    previous = _snapshot_pasteboard() if restore else None

    _set_clipboard(text)
    # Our own write; anything past this value isn't ours. No wait is needed before
    # keying Cmd+V: NSPasteboard writes are synchronous and in-process, so the
    # data is already on the pasteboard by the time _set_clipboard returns. (The
    # original 50ms sleep here was cargo — and the condition-poll that briefly
    # replaced it was provably true on its first check, every time.)
    expected = _change_count()
    _press_cmd_v()
    if on_pasted is not None:
        on_pasted()  # the text is on screen now; everything below is cleanup

    if restore:
        time.sleep(RESTORE_DELAY)
        if _change_count() != expected:
            # Someone — almost always the user's own Cmd+C — wrote to the
            # pasteboard inside our window. Their copy is newer than our
            # snapshot, so restoring would destroy it.
            print("[simo] clipboard changed during paste — leaving it alone", flush=True)
        elif previous:
            # Never leave our dictation on the pasteboard: if putting the user's
            # content back fails, an empty clipboard is the lesser harm — a
            # clipboard-history tool would otherwise capture the transcript.
            if not _restore_pasteboard(previous):
                print("[simo] could not restore the clipboard — clearing it", flush=True)
                _clear_clipboard()
        else:
            # Empty before, or too large to snapshot. Either way ours must not stay.
            _clear_clipboard()
    return True


if __name__ == "__main__":
    # self-check: clipboard round-trip + permission report (no keystroke sent)
    print(f"accessibility trusted: {_is_trusted()}")
    print(f"focus snapshot:        {_capture_focus()}")
    sentinel = "simo-flow-selfcheck-§"
    before = _get_clipboard()
    start = _change_count()
    _set_clipboard(sentinel)
    assert _get_clipboard() == sentinel, "clipboard set failed"
    assert _change_count() > start, "changeCount did not advance on write"
    if before is not None:
        _set_clipboard(before)
        assert _get_clipboard() == before, "clipboard restore failed"
    print("inject self-check OK (paste keystroke not exercised headlessly)")
