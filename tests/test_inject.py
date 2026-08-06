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


class FakeMac:
    """Stand-in for the macOS surfaces inject.py talks to.

    Models the one behaviour that matters and is easy to get wrong: the
    pasteboard's changeCount increments on *every* write, from any process.
    """

    def __init__(self, clipboard="old clipboard", trusted=True, frontmost=501):
        self.clipboard = clipboard
        self.trusted = trusted
        self.change_count = 1
        self.frontmost = frontmost
        self.activated: list[int] = []
        self.activate_ok = True
        self.keys: list[tuple] = []
        self.logs: list[str] = []

    # --- pasteboard ---
    def get_clipboard(self):
        return self.clipboard

    def set_clipboard(self, text):
        self.clipboard = text
        self.change_count += 1

    def clear_clipboard(self):
        self.clipboard = None
        self.change_count += 1

    def has_any_content(self):
        return self.clipboard is not None

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
    # every event carries the Command flag so its flagsChanged form matches hardware
    assert all(flags for _k, _d, flags in mac.keys)


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
    mac.inject.paste_text("hello", on_pasted=lambda: order.append("pasted"))
    assert order.index("pasted") > order.index("set:hello"), "fires after the keystroke"
    assert order[-1] != "pasted", "the clipboard restore must still happen afterwards"
    assert order[-1].startswith("set:old"), "and it is the restore that comes last"


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
