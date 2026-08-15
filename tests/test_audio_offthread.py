"""The keyboard thread must never block on audio hardware.

Simo Flow froze a MacBook mid-meeting. `begin()` opened the microphone from
inside the fn-key CGEventTap callback, which runs on the main runloop — and a
CoreAudio operation blocked there while AirPods were connecting. A tap that
blocks stalls the *system* input pipeline, so every key in every app stops:
the machine looks dead, not the app.

The v2.2.3 revert removed one route to that block (device-change teardown) but
left two others, and `sd.InputStream()` itself can block while a device is
switching. Removing individual calls is whack-a-mole; the invariant is:

    NOTHING reachable from the hotkey may wait on the audio device.

These tests hold that line the only way it can be held — by measuring the
*caller*. The device layer is wedged deliberately, and every Recorder entry
point reachable from a key press must still return promptly. They fail on the
pre-fix code, where begin() waited for the device to answer.
"""
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import audio  # noqa: E402

# The device op blocks for this long; the caller must return in well under it.
# Deliberately far apart: a threshold that needs tuning is a flaky test.
WEDGE_SEC = 1.5
CALLER_BUDGET_SEC = 0.25


@pytest.fixture
def rec(monkeypatch):
    """A Recorder whose device layer never touches real hardware."""
    monkeypatch.setattr(audio, "default_input_device", lambda: 1)
    r = audio.Recorder()
    monkeypatch.setattr(r, "_open_stream", lambda: True)
    monkeypatch.setattr(r, "_close_stream", lambda: None)
    return r


def _wedge(seconds: float):
    """A device operation that blocks, and announces when it has started."""
    started = threading.Event()

    def blocking_op(*_a, **_kw):
        started.set()
        time.sleep(seconds)
        return True

    return blocking_op, started


def _feed(rec, seconds: float = 1.0) -> None:
    """Push audible frames through the capture callback, as PortAudio would."""
    frames = int(audio.RATE * seconds)
    block = np.full((frames, 1), 0.2, dtype=np.float32)
    rec._cb(block, frames, None, None)


# ---- the invariant ------------------------------------------------------


def test_begin_returns_immediately_when_opening_the_device_blocks(rec, monkeypatch):
    """A wedged mic open must not hold the key-press thread.

    This is the freeze, reproduced: on the pre-fix code begin() waits for the
    open, so the caller — the CGEventTap callback — is stuck for WEDGE_SEC and
    the whole machine stops accepting input.
    """
    blocking_open, started = _wedge(WEDGE_SEC)
    monkeypatch.setattr(rec, "_open_stream", blocking_open)

    t0 = time.time()
    rec.begin()
    elapsed = time.time() - t0

    assert elapsed < CALLER_BUDGET_SEC, (
        f"begin() held the caller for {elapsed:.2f}s — a CGEventTap callback that "
        f"blocks this long freezes every key on the machine"
    )
    assert started.wait(WEDGE_SEC), "the mic open never ran on the audio thread"


def test_begin_returns_immediately_when_portaudio_reinit_blocks(rec, monkeypatch):
    """The exact v2.2.1 freeze: sd._terminate() wedged on the hotkey path.

    `_needs_reinit` is set by the *previous* utterance (a silent capture — which
    is what connecting AirPods mid-sentence produces), so this fires on the next
    key press with no device change needed.
    """
    blocking_reinit, started = _wedge(WEDGE_SEC)
    monkeypatch.setattr(rec, "_reinit_portaudio", blocking_reinit)
    rec._needs_reinit = True

    t0 = time.time()
    rec.begin()
    elapsed = time.time() - t0

    assert elapsed < CALLER_BUDGET_SEC, (
        f"begin() held the caller for {elapsed:.2f}s while re-initialising PortAudio — "
        f"this is the call that froze the machine mid-meeting"
    )
    assert started.wait(WEDGE_SEC), "the re-init never ran on the audio thread"


def test_end_returns_immediately_when_closing_the_device_blocks(rec, monkeypatch):
    """Releasing the key must not block either — same tap, same consequence."""
    rec.begin()
    _feed(rec, 1.0)
    blocking_close, started = _wedge(WEDGE_SEC)
    monkeypatch.setattr(rec, "_close_stream", blocking_close)

    t0 = time.time()
    samples = rec.end()
    elapsed = time.time() - t0

    assert elapsed < CALLER_BUDGET_SEC, (
        f"end() held the caller for {elapsed:.2f}s closing the stream"
    )
    assert samples is not None, "the audio must still come back while the close runs"
    assert started.wait(WEDGE_SEC), "the close never ran on the audio thread"


# ---- behaviour that must survive moving off the thread -------------------


def test_audio_captured_between_press_and_release_still_arrives(rec):
    """The whole point of the app: speech in, samples out."""
    rec.begin()
    _feed(rec, 1.0)
    samples = rec.end()

    assert samples is not None, "no audio returned"
    assert len(samples) > audio.MIN_UTTERANCE_SEC * audio.RATE


def test_device_operations_run_in_press_then_release_order(rec, monkeypatch):
    """Open must never be applied after the close that follows it.

    Off-thread work invites reordering, and an open landing after its own close
    leaves the microphone live with the indicator lit — the exact "always
    listening" impression the on-demand design exists to avoid.
    """
    order: list[str] = []
    monkeypatch.setattr(rec, "_open_stream", lambda: (order.append("open"), True)[1])
    monkeypatch.setattr(rec, "_close_stream", lambda: order.append("close"))

    rec.begin()
    _feed(rec, 1.0)
    rec.end()
    rec._drain_audio_ops(timeout=WEDGE_SEC)

    assert order == ["open", "close"], f"device ops ran out of order: {order}"


def test_a_late_open_failure_cannot_kill_the_press_that_replaced_it(rec, monkeypatch):
    """A stale failure must not cancel the recording the user is speaking into.

    Moving the open off-thread introduced this: the retry path takes seconds, and
    someone whose microphone just failed presses fn again straight away — which is
    exactly what happened during the eleven consecutive failures in the log. The
    first press's failure then landed on the second press's recording and killed
    it, so the mic looked broken even once it was working again.
    """
    release = threading.Event()
    attempts = {"n": 0}

    def open_stream():
        attempts["n"] += 1
        if attempts["n"] == 1:
            release.wait(WEDGE_SEC)  # hold press 1 open until press 2 has landed
            return False
        if attempts["n"] == 2:
            return False  # press 1's retry fails too — the mic really was gone
        return True  # press 2 opens cleanly

    monkeypatch.setattr(rec, "_open_stream", open_stream)
    monkeypatch.setattr(rec, "_reinit_portaudio", lambda: None)

    rec.begin()  # press 1 — its open is stuck, and will fail
    rec.begin()  # press 2 — the user tries again rather than waiting
    release.set()  # press 1's failure now arrives, too late to be relevant
    assert rec._drain_audio_ops(timeout=WEDGE_SEC * 2), "audio thread never settled"

    assert rec.is_recording is True, (
        "a failure from the previous press cancelled the current recording — the "
        "user speaks into a mic that is actually working and gets nothing"
    )


def test_a_microphone_that_never_opens_is_reported_not_silently_swallowed(rec, monkeypatch):
    """A failed open must still reach the user.

    Moving the open off the caller's thread means its failure is no longer
    available as a return value, and the tempting shortcut is to drop it. A mic
    that silently records nothing is the worst outcome: the user speaks, waits,
    and gets an empty paste with no reason given.
    """
    monkeypatch.setattr(rec, "_open_stream", lambda: False)
    monkeypatch.setattr(rec, "_reinit_portaudio", lambda: None)

    rec.begin()
    rec._drain_audio_ops(timeout=WEDGE_SEC)
    samples = rec.end()

    assert samples is None
    assert "unavailable" in rec.reject_reason, (
        f"a mic that never opened was reported as {rec.reject_reason!r} — the user "
        f"cannot tell a broken mic from having said nothing"
    )
