"""The fn-key event tap — the most consequential code in the app.

This tap sits *inside* the system input pipeline. Everything it does is felt by
every key in every application: return late and macOS disables it, so the fn key
dies silently; consume the wrong event and someone's keystroke vanishes; miss a
key-up and the app believes fn is held forever.

It had no tests at all until now, which is how a file earns the right to freeze a
MacBook. These characterise the real behaviour of the callback so it can be
changed with evidence rather than hope. No Quartz calls are made: the two module
functions that touch the OS are replaced, and the callback is driven directly the
way macOS drives it.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import hotkey  # noqa: E402

SHIFT_FLAG = 0x20000  # any modifier that is not fn


@pytest.fixture
def tap(monkeypatch):
    """A listener whose OS calls are recorded rather than performed."""
    events = {"press": 0, "release": 0, "reenabled": 0}
    listener = hotkey.HotkeyListener(
        on_press=lambda: events.__setitem__("press", events["press"] + 1),
        on_release=lambda: events.__setitem__("release", events["release"] + 1),
    )
    listener._tap = object()  # stand-in for the CFMachPort
    monkeypatch.setattr(
        hotkey, "CGEventTapEnable", lambda _t, _on: events.__setitem__("reenabled", events["reenabled"] + 1)
    )
    listener.events = events
    return listener


def _flags(monkeypatch, value):
    monkeypatch.setattr(hotkey, "CGEventGetFlags", lambda _e: value)


# ---- the fn key itself ---------------------------------------------------


def test_pressing_fn_fires_once_and_is_swallowed(tap, monkeypatch):
    """Consuming the event is the whole reason this is a filtering tap.

    Returning the event would let macOS run its own fn action as well — the emoji
    picker or Apple's dictation — so the user gets two things happening at once
    and, if the other one is a dictation tool, the same sentence transcribed twice.
    """
    _flags(monkeypatch, hotkey.FN_FLAG)

    result = tap._handle(None, hotkey.kCGEventFlagsChanged, object(), None)

    assert result is None, "the fn event must be consumed, not passed to the system"
    assert tap.events["press"] == 1
    assert tap.events["release"] == 0


def test_releasing_fn_fires_release_and_is_swallowed(tap, monkeypatch):
    _flags(monkeypatch, hotkey.FN_FLAG)
    tap._handle(None, hotkey.kCGEventFlagsChanged, object(), None)

    _flags(monkeypatch, 0)
    result = tap._handle(None, hotkey.kCGEventFlagsChanged, object(), None)

    assert result is None
    assert tap.events == {"press": 1, "release": 1, "reenabled": 0}


def test_the_same_fn_state_reported_twice_does_not_fire_twice(tap, monkeypatch):
    """macOS sends flagsChanged for *every* modifier transition, so the tap sees
    plenty of events where fn has not actually changed. Firing on each would start
    a second recording while the first is still running."""
    _flags(monkeypatch, hotkey.FN_FLAG)
    tap._handle(None, hotkey.kCGEventFlagsChanged, object(), None)
    tap._handle(None, hotkey.kCGEventFlagsChanged, object(), None)

    assert tap.events["press"] == 1, "a repeated down-state must not start a second utterance"


# ---- everything that is not fn ------------------------------------------


def test_another_modifier_passes_straight_through(tap, monkeypatch):
    """Shift, command and the rest must reach the system untouched. Swallowing
    them would break typing everywhere, which is the worst thing this file could
    plausibly do."""
    sentinel = object()
    _flags(monkeypatch, SHIFT_FLAG)

    result = tap._handle(None, hotkey.kCGEventFlagsChanged, sentinel, None)

    assert result is sentinel, "a non-fn modifier must be returned to the system"
    assert tap.events == {"press": 0, "release": 0, "reenabled": 0}


def test_fn_held_together_with_another_modifier_still_counts_as_fn(tap, monkeypatch):
    """Shift+fn is still fn. Testing equality against the flag mask rather than a
    bitwise AND would miss it, and hold-to-talk would fail whenever the user
    happened to be holding another key."""
    _flags(monkeypatch, hotkey.FN_FLAG | SHIFT_FLAG)

    assert tap._handle(None, hotkey.kCGEventFlagsChanged, object(), None) is None
    assert tap.events["press"] == 1


# ---- macOS disabling the tap -------------------------------------------


def test_a_tap_disabled_by_timeout_is_re_enabled(tap, monkeypatch):
    """macOS switches off a tap that answers too slowly, and it does NOT switch it
    back on. Without this the fn key is dead until the app restarts, with nothing
    on screen to say why — the failure mode a competing app has an open bug for.
    """
    result = tap._handle(None, hotkey.kCGEventTapDisabledByTimeout, object(), None)

    assert tap.events["reenabled"] == 1, "the tap must be switched back on"
    assert result is not None, "the disable notification is not ours to swallow"


def test_a_tap_disabled_by_user_input_is_also_re_enabled(tap):
    tap._handle(None, hotkey.kCGEventTapDisabledByUserInput, object(), None)
    assert tap.events["reenabled"] == 1


def test_being_disabled_while_fn_is_held_releases_the_stuck_key(tap, monkeypatch):
    """The key-up arrives while the tap is off, so it is never seen. Left alone the
    app believes fn is still down for ever: recording never ends and every later
    press is ignored. Ending the utterance is the recoverable choice."""
    _flags(monkeypatch, hotkey.FN_FLAG)
    tap._handle(None, hotkey.kCGEventFlagsChanged, object(), None)
    assert tap._down is True

    tap._handle(None, hotkey.kCGEventTapDisabledByTimeout, object(), None)

    assert tap._down is False, "fn must not be left latched down"
    assert tap.events["release"] == 1, "the in-flight utterance must be ended, not abandoned"

    # and a genuine press afterwards still works
    tap._handle(None, hotkey.kCGEventFlagsChanged, object(), None)
    assert tap.events["press"] == 2
