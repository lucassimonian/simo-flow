"""Paste-path unit tests — no GUI, no real keystrokes, no real pasteboard.

Everything AppKit/Quartz touches is behind a thin module-level wrapper in
engine.inject, so these tests exercise the *orchestration* — which is where all
four v2.1.0 paste bugs lived:

  1. pasting into whatever window happens to be frontmost when the pipeline
     finishes, instead of the one focused when the user started speaking
  2. restoring the old clipboard on top of something the user copied during the
     paste window
  3. sending only the V key events, which Electron/Chromium apps drop because
     they track modifier state from the Cmd key's own flagsChanged event
  4. posting events with Accessibility revoked, which macOS swallows silently —
     so dictation "worked" forever while pasting nothing

Run:  ./.venv/bin/python -m pytest tests/test_inject.py -q
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


STRING_TYPE = "public.utf8-plain-text"


class FakeMac:
    """Stand-in for the macOS surfaces inject.py talks to.

    Models the two behaviours that matter and are easy to get wrong:

      * the pasteboard's changeCount increments on *every* write, from any process
      * the pasteboard is a list of items, each carrying the same content in
        several representations — an image, a file reference, or rich text is not
        a string and is destroyed by anything that round-trips it as one
    """

    def __init__(self, clipboard="old clipboard", trusted=True, frontmost=501):
        self.items: list[list[tuple[str, bytes]]] = []
        self.clipboard = clipboard
        self.trusted = trusted
        self.change_count = 1
        self.frontmost = frontmost
        self.activated: list[int] = []
        self.activate_ok = True
        self.keys: list[tuple] = []
        self.logs: list[str] = []

    # --- pasteboard ---
    # `clipboard` stays a plain string for the tests that only care about text;
    # underneath, the pasteboard is modelled properly as typed items.
    @property
    def clipboard(self):
        for item in self.items:
            for uti, data in item:
                if uti == STRING_TYPE:
                    return data.decode()
        return None

    @clipboard.setter
    def clipboard(self, text):
        self.items = [[(STRING_TYPE, text.encode())]] if text is not None else []

    def hold_image(self, payload=b"\x89PNG\r\n\x1a\n-fake-image"):
        """The user copied a picture. No string representation exists."""
        self.items = [[("public.png", payload)]]
        self.change_count += 1

    def hold_rich_text(self, rtf=rb"{\rtf1 bold thing}", plain="bold thing"):
        """The user copied styled text. It *does* have a string representation —
        which is exactly the trap: reading it back as a string loses the styling."""
        self.items = [[("public.rtf", rtf), (STRING_TYPE, plain.encode())]]
        self.change_count += 1

    def get_clipboard(self):
        return self.clipboard

    def set_clipboard(self, text):
        self.clipboard = text
        self.change_count += 1

    def clear_clipboard(self):
        self.items = []
        self.change_count += 1

    def has_any_content(self):
        return bool(self.items)

    def snapshot_pasteboard(self):
        return [list(item) for item in self.items]

    def restore_pasteboard(self, snapshot):
        self.items = [list(item) for item in snapshot]
        self.change_count += 1
        return True

    def get_change_count(self):
        return self.change_count

    # --- permissions / focus / keys ---
    def is_trusted(self):
        return self.trusted

    def capture_focus(self):
        return {"pid": self.frontmost, "bundle_id": f"app.{self.frontmost}"}

    def activate_pid(self, pid):
        self.activated.append(pid)
        if self.activate_ok:
            self.frontmost = pid
        return self.activate_ok

    def post_key(self, keycode, down, flags):
        self.keys.append((keycode, down, flags))

    def third_party_copies(self, text):
        """Simulate the user hitting Cmd+C mid-paste-window."""
        self.clipboard = text
        self.change_count += 1


@pytest.fixture()
def mac(monkeypatch):
    import engine.inject as inject

    fake = FakeMac()
    monkeypatch.setattr(inject, "_get_clipboard", fake.get_clipboard)
    monkeypatch.setattr(inject, "_set_clipboard", fake.set_clipboard)
    monkeypatch.setattr(inject, "_clear_clipboard", fake.clear_clipboard)
    monkeypatch.setattr(inject, "_has_any_content", fake.has_any_content)
    monkeypatch.setattr(inject, "_snapshot_pasteboard", fake.snapshot_pasteboard)
    monkeypatch.setattr(inject, "_restore_pasteboard", fake.restore_pasteboard)
    monkeypatch.setattr(inject, "_change_count", fake.get_change_count)
    monkeypatch.setattr(inject, "_is_trusted", fake.is_trusted)
    monkeypatch.setattr(inject, "_capture_focus", fake.capture_focus)
    monkeypatch.setattr(inject, "_activate_pid", fake.activate_pid)
    monkeypatch.setattr(inject, "_post_key", fake.post_key)
    monkeypatch.setattr(inject, "RESTORE_DELAY", 0.0)
    monkeypatch.setattr(inject, "ACTIVATE_DELAY", 0.0)
    fake.inject = inject
    return fake


# --------------------------------------------------------------------------
# 4. Accessibility revoked must fail loudly, not silently
# --------------------------------------------------------------------------
def test_paste_refuses_when_accessibility_not_granted(mac, capsys):
    mac.trusted = False
    assert mac.inject.paste_text("hello") is False
    # the user's clipboard must be left completely alone
    assert mac.clipboard == "old clipboard"
    assert mac.keys == []
    # and the failure must be visible — this is the bug that made dictation
    # look broken forever after a macOS update revoked the permission
    assert "accessibility" in capsys.readouterr().out.lower()


# --------------------------------------------------------------------------
# 3. The full four-event Cmd+V sequence
# --------------------------------------------------------------------------
def test_paste_sends_cmd_down_v_down_v_up_cmd_up(mac):
    assert mac.inject.paste_text("hello") is True
    kinds = [(k, down) for k, down, _flags in mac.keys]
    assert kinds == [
        (mac.inject.KEY_CMD, True),
        (mac.inject.KEY_V, True),
        (mac.inject.KEY_V, False),
        (mac.inject.KEY_CMD, False),
    ], "Electron/Chromium apps drop the paste unless the Cmd key event is posted too"

    cmd = mac.inject.kCGEventFlagMaskCommand
    flags = [f for _k, _d, f in mac.keys]
    assert flags[:3] == [cmd, cmd, cmd], "the held phase must carry the Command flag"
    # The release must NOT. Sending "Command released" with the Command flag still
    # set is self-contradictory and macOS latches it: the modifier then reads as
    # held after every dictation, so the user's next keystroke becomes a shortcut
    # and their keyboard appears broken. Real hardware clears it on release.
    assert flags[3] == 0, "Cmd key-up must clear the modifier or it stays stuck"


# --------------------------------------------------------------------------
# 2. Clipboard guard
# --------------------------------------------------------------------------
def test_clipboard_is_restored_when_nobody_else_wrote(mac):
    mac.inject.paste_text("dictated words")
    assert mac.clipboard == "old clipboard"


def test_clipboard_not_restored_when_user_copied_during_paste(mac, monkeypatch):
    """The user hits Cmd+C while transcription finishes. Their copy must win."""
    real_press = mac.inject._press_cmd_v

    def press_then_user_copies():
        real_press()
        mac.third_party_copies("something the user just copied")

    monkeypatch.setattr(mac.inject, "_press_cmd_v", press_then_user_copies)
    mac.inject.paste_text("dictated words")
    assert mac.clipboard == "something the user just copied"


# --------------------------------------------------------------------------
# 1. Focus captured at record-start, restored before pasting
# --------------------------------------------------------------------------
def test_a_clipboard_that_cannot_be_read_never_raises(monkeypatch):
    """A failure here must cost the clipboard, never the dictation.

    paste_text() promises to always return, and its caller only saves the
    transcript *after* it returns (__main__._run_pipeline). An exception escaping
    the snapshot would therefore destroy the words the user just spoke — the
    primary result — to protect a side effect. That is lesson 007 exactly.
    """
    import engine.inject as inject

    class Exploding:
        @staticmethod
        def generalPasteboard():
            raise RuntimeError("pasteboard unavailable")

    monkeypatch.setattr(inject, "NSPasteboard", Exploding)

    assert inject._snapshot_pasteboard() is None
    assert inject._restore_pasteboard([[("public.png", b"x")]]) is False


def test_an_oversized_clipboard_is_measured_before_it_is_copied(monkeypatch):
    """The cap must bound the allocation, not merely the check.

    Reading the bytes and *then* testing the size means a multi-gigabyte file
    promise is already resident in memory by the time the limit trips — which is
    the one thing the limit exists to prevent.
    """
    import engine.inject as inject

    materialised: list[str] = []

    class HugeData:
        @staticmethod
        def length():
            return inject.MAX_SNAPSHOT_BYTES + 1

        def __bytes__(self):
            # Must succeed, not raise: a failing bytes() would be swallowed by the
            # snapshot's own error handling and the test would pass for the wrong
            # reason — which is precisely what the mutation sweep caught.
            materialised.append("copied")
            return b"\0" * 1024

    class Item:
        @staticmethod
        def types():
            return ["public.movie"]

        @staticmethod
        def dataForType_(_uti):
            return HugeData()

    class Pasteboard:
        @staticmethod
        def generalPasteboard():
            return Pasteboard()

        @staticmethod
        def pasteboardItems():
            return [Item()]

    monkeypatch.setattr(inject, "NSPasteboard", Pasteboard)

    assert inject._snapshot_pasteboard() is None, "oversized clipboard must not be snapshotted"
    assert materialised == [], "the payload was copied into memory before being measured"


def test_paste_uses_the_key_that_types_v_on_this_layout(mac, monkeypatch):
    """Virtual key codes are positions, not letters.

    kVK_ANSI_V is wherever "v" sits on a US keyboard. On Dvorak that position is
    ".", on AZERTY something else again — so posting it blindly sent Cmd+the wrong
    key and paste silently did nothing (or worse) for anyone not on QWERTY.
    """
    monkeypatch.setattr(mac.inject, "_keycode_for_character", lambda ch: 47 if ch == "v" else None)

    assert mac.inject.paste_text("hello") is True

    pressed = [code for code, down, _flags in mac.keys if down and code != mac.inject.KEY_CMD]
    assert pressed == [47], f"pasted with the US position instead of this layout's: {mac.keys}"


def test_paste_falls_back_to_the_us_position_when_the_layout_is_unreadable(mac, monkeypatch):
    """Some input sources — handwriting, certain IMEs — expose no layout at all.
    Pasting with the US position is what the app always did, so it is the right
    thing to degrade to. Failing to paste would be far worse than guessing."""
    monkeypatch.setattr(mac.inject, "_keycode_for_character", lambda ch: None)

    assert mac.inject.paste_text("hello") is True

    pressed = [code for code, down, _flags in mac.keys if down and code != mac.inject.KEY_CMD]
    assert pressed == [mac.inject.KEY_V]


def test_the_real_layout_lookup_agrees_with_the_us_constant_on_a_us_layout(mac):
    """Guards the ctypes plumbing itself, not the orchestration around it.

    A wrong signature or a misread property would return None and be invisible —
    the fallback would quietly paste correctly on the machine running the tests
    and wrongly everywhere else.
    """
    import engine.inject as real

    found = real._keycode_for_character("v")
    if found is None:
        pytest.skip("no readable keyboard layout on this machine")
    assert found == real.KEY_V, (
        f"the layout API says 'v' is keycode {found} but kVK_ANSI_V is {real.KEY_V} — "
        f"either this machine is not on a US layout, or the lookup is wrong"
    )


def test_a_copied_image_survives_dictating(mac):
    """Dictating must not destroy something the user copied.

    An image has no string representation, so reading the pasteboard back as text
    returns nothing — and the old code cleared it on the grounds that leaving the
    dictation there was worse. Both options lose the picture. Snapshotting the
    typed data loses neither.
    """
    mac.hold_image()

    assert mac.inject.paste_text("dictated words") is True

    types = [uti for item in mac.items for uti, _ in item]
    assert "public.png" in types, f"the copied image was destroyed; clipboard now {types}"
    assert mac.clipboard != "dictated words", "the dictation was left on the pasteboard"


def test_rich_text_keeps_its_formatting(mac):
    """The subtler half of the same bug, and not in the issue.

    Styled text *does* have a string representation, so the old code restored it
    happily — as plain text, silently dropping every bit of formatting. A restore
    that quietly downgrades the content is still data loss.
    """
    mac.hold_rich_text()

    assert mac.inject.paste_text("dictated words") is True

    types = [uti for item in mac.items for uti, _ in item]
    assert "public.rtf" in types, f"formatting was lost on restore; clipboard now {types}"
    assert mac.clipboard == "bold thing"


def test_an_empty_clipboard_is_left_empty(mac):
    """Nothing was copied, so nothing should be put back — least of all ours."""
    mac.clear_clipboard()

    assert mac.inject.paste_text("dictated words") is True

    assert mac.items == [], f"pasteboard should be empty, holds {mac.items}"


def test_paste_activates_the_window_focused_when_recording_started(mac):
    focus = mac.inject.capture_focus()  # user presses fn while in app 501
    mac.frontmost = 999  # they click away during transcription
    assert mac.inject.paste_text("hello", focus=focus) is True
    assert mac.activated == [501], "must return to the window the user started in"
    assert mac.frontmost == 501


def test_paste_skips_activation_when_focus_never_moved(mac):
    focus = mac.inject.capture_focus()
    assert mac.inject.paste_text("hello", focus=focus) is True
    assert mac.activated == [], "no need to activate an app that is already frontmost"


def test_paste_aborts_before_touching_clipboard_if_activation_fails(mac, capsys):
    """Target app quit mid-transcription. Failing must cost the user nothing."""
    focus = mac.inject.capture_focus()
    mac.frontmost = 999
    mac.activate_ok = False
    assert mac.inject.paste_text("hello", focus=focus) is False
    assert mac.clipboard == "old clipboard", "clipboard must survive a failed paste"
    assert mac.keys == []
    assert "activate" in capsys.readouterr().out.lower()


def test_paste_aborts_if_focus_never_actually_arrives(mac, capsys, monkeypatch):
    """activate_pid can return success while focus stays put (a modal sheet, a
    system panel holding the foreground). Keying Cmd+V then would type into the
    wrong window — the exact bug this path exists to stop."""
    focus = mac.inject.capture_focus()
    mac.frontmost = 999

    def activate_but_focus_stays(pid):
        mac.activated.append(pid)
        return True  # macOS said yes; the foreground disagrees

    monkeypatch.setattr(mac.inject, "_activate_pid", activate_but_focus_stays)
    monkeypatch.setattr(mac.inject, "ACTIVATE_DELAY", 0.02)
    assert mac.inject.paste_text("hello", focus=focus) is False
    assert mac.clipboard == "old clipboard"
    assert mac.keys == []
    assert "foreground" in capsys.readouterr().out.lower()


def test_activation_wait_returns_immediately_when_already_focused(mac):
    """The wait must cost nothing in the common case — no fixed sleep."""
    import time as _t

    focus = mac.inject.capture_focus()
    t0 = _t.perf_counter()
    assert mac.inject.paste_text("hello", focus=focus) is True
    assert (_t.perf_counter() - t0) < 0.05, "already-correct focus must not sleep"


def test_activation_wait_polls_until_focus_actually_lands(mac, monkeypatch):
    """Drive _wait_until through real iterations, not just its already-true branch.

    Every other activation test flips the fake's focus synchronously, so the poll
    loop's body was never executed — an accidental early return in it would have
    gone unnoticed. Here focus arrives only on the third check, as a cold app
    coming forward genuinely does.
    """
    focus = mac.inject.capture_focus()
    mac.frontmost = 999
    checks = {"n": 0}

    def slow_focus():
        checks["n"] += 1
        if checks["n"] >= 3:  # lands on the third poll
            return {"pid": 501, "bundle_id": "app.501"}
        return {"pid": 999, "bundle_id": "app.999"}

    def activate_without_moving_focus(pid):
        mac.activated.append(pid)
        return True  # macOS accepted it; focus follows a moment later

    monkeypatch.setattr(mac.inject, "_capture_focus", slow_focus)
    monkeypatch.setattr(mac.inject, "_activate_pid", activate_without_moving_focus)
    monkeypatch.setattr(mac.inject, "ACTIVATE_DELAY", 1.0)

    assert mac.inject.paste_text("hello", focus=focus) is True
    assert checks["n"] >= 3, "the poll loop body must actually have run"
    assert mac.keys, "the paste should proceed once focus lands"


def test_pid_reuse_with_a_different_app_is_not_treated_as_the_same_window(mac, monkeypatch):
    """A pid is not an identity. If the target quits and macOS reissues its pid to
    an unrelated process, a pid-only match would paste the user's words into that
    process — the very bug this module exists to prevent."""
    focus = {"pid": 501, "bundle_id": "com.apple.TextEdit"}
    # same pid, different app: the impostor is already frontmost
    monkeypatch.setattr(
        mac.inject, "_capture_focus", lambda: {"pid": 501, "bundle_id": "com.evil.app"}
    )
    monkeypatch.setattr(mac.inject, "ACTIVATE_DELAY", 0.02)
    assert mac.inject.paste_text("secret words", focus=focus) is False
    assert mac.clipboard == "old clipboard", "must not stage text for the wrong app"
    assert mac.keys == []


def test_on_pasted_fires_when_the_text_lands_not_when_cleanup_finishes(mac, monkeypatch):
    """The callback is what the reported latency is measured against, so it has to
    mark the moment the user sees their text. Timing the whole call instead
    counted ~300ms of clipboard housekeeping nobody waits for."""
    monkeypatch.setattr(mac.inject, "RESTORE_DELAY", 0.05)
    order = []
    monkeypatch.setattr(
        mac.inject, "_set_clipboard", lambda t: (order.append(f"set:{t}"), mac.set_clipboard(t))[1]
    )
    # The restore now rebuilds every typed representation rather than writing a
    # string back, so the spy has to watch that call instead of _set_clipboard.
    monkeypatch.setattr(
        mac.inject,
        "_restore_pasteboard",
        lambda s: (order.append("restored"), mac.restore_pasteboard(s))[1],
    )
    mac.inject.paste_text("hello", on_pasted=lambda: order.append("pasted"))
    assert order.index("pasted") > order.index("set:hello"), "fires after the keystroke"
    assert order[-1] != "pasted", "the clipboard restore must still happen afterwards"
    assert order[-1] == "restored", "and it is the clipboard restore that comes last"


def test_paste_still_works_without_a_focus_snapshot(mac):
    """Backwards compatible: no snapshot means paste wherever we are."""
    assert mac.inject.paste_text("hello") is True
    assert mac.activated == []


# --------------------------------------------------------------------------
# unrecoverable non-text clipboard: clear rather than leak the dictation
# --------------------------------------------------------------------------
def test_nontext_clipboard_is_cleared_not_left_holding_dictation(mac):
    mac.clipboard = None  # an image: unreadable as text, but present
    mac.has_any_content = lambda: True
    mac.inject._has_any_content = lambda: True
    mac.inject.paste_text("dictated words")
    assert mac.clipboard is None, "dictation must not be left on the pasteboard"
