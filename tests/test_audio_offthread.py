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

# How long a wedged device operation blocks for. Nothing asserts against a
# stopwatch — see _wedge — so this only needs to be comfortably longer than the
# time the test itself takes to reach its assertion.
WEDGE_SEC = 1.5


@pytest.fixture
def rec(monkeypatch):
    """A Recorder whose device layer never touches real hardware."""
    monkeypatch.setattr(audio, "default_input_device", lambda: 1)
    r = audio.Recorder()
    monkeypatch.setattr(r, "_open_stream", lambda: True)
    monkeypatch.setattr(r, "_close_stream", lambda: None)
    return r


def _wedge(seconds: float):
    """A device operation that blocks, announcing when it starts and finishes.

    `finished` is what makes these tests exact rather than a stopwatch. If a
    hotkey entry point waited for the device, the operation would necessarily have
    completed by the time it returned — so `finished` being set is proof of
    blocking, with no wall-clock threshold to tune and nothing to go flaky on a
    slow or loaded machine. An earlier version asserted "returned in under 250ms"
    and failed on CI while passing locally, which is the exact failure mode the
    project's own rules warn about.
    """
    started = threading.Event()
    finished = threading.Event()

    def blocking_op(*_a, **_kw):
        started.set()
        time.sleep(seconds)
        finished.set()
        return True

    return blocking_op, started, finished


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
    blocking_open, started, finished = _wedge(WEDGE_SEC)
    monkeypatch.setattr(rec, "_open_stream", blocking_open)

    rec.begin()

    assert not finished.is_set(), (
        "begin() waited for the microphone to open — a CGEventTap callback that "
        "blocks freezes every key on the machine, not just this app"
    )
    assert started.wait(WEDGE_SEC), "the mic open never ran on the audio thread"


def test_begin_returns_immediately_when_portaudio_reinit_blocks(rec, monkeypatch):
    """The exact v2.2.1 freeze: sd._terminate() wedged on the hotkey path.

    `_needs_reinit` is set by the *previous* utterance (a silent capture — which
    is what connecting AirPods mid-sentence produces), so this fires on the next
    key press with no device change needed.
    """
    blocking_reinit, started, finished = _wedge(WEDGE_SEC)
    monkeypatch.setattr(rec, "_reinit_portaudio", blocking_reinit)
    rec._needs_reinit = True

    rec.begin()

    assert not finished.is_set(), (
        "begin() waited for PortAudio to re-initialise — this is the call that "
        "froze the machine mid-meeting"
    )
    assert started.wait(WEDGE_SEC), "the re-init never ran on the audio thread"


def test_end_returns_immediately_when_closing_the_device_blocks(rec, monkeypatch):
    """Releasing the key must not block either — same tap, same consequence."""
    rec.begin()
    _feed(rec, 1.0)
    blocking_close, started, finished = _wedge(WEDGE_SEC)
    monkeypatch.setattr(rec, "_close_stream", blocking_close)

    samples = rec.end()

    assert not finished.is_set(), "end() waited for the microphone to close"
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


def test_a_slow_open_from_a_previous_press_does_not_hijack_the_current_one(rec, monkeypatch):
    """A stale open that finally *succeeds* must not publish its stream.

    The generation guard originally covered only the failure path. A slow open
    that eventually worked would hand its stream to whichever utterance owned the
    recorder by then: frames from the new press start landing in it, and the close
    queued by the *old* press shuts it mid-sentence. Nothing reports that — no
    error, no reject_reason, just a hole in what the user said.
    """
    release = threading.Event()
    closes: list[int] = []

    def slow_open():
        release.wait(WEDGE_SEC)
        return True

    monkeypatch.setattr(rec, "_open_stream", slow_open)
    monkeypatch.setattr(rec, "_close_stream", lambda: closes.append(1))

    rec.begin()  # press 1 — its open blocks
    rec.end()  # released before the mic ever opened; queues a close
    rec.begin()  # press 2 — a new utterance now owns the recorder
    release.set()  # press 1's open completes, far too late to be relevant
    assert rec._drain_audio_ops(timeout=WEDGE_SEC * 3), "audio thread never settled"

    # Two closes: the one press 1's end() queued, and press 1's own stale stream
    # being shut immediately rather than left live for press 2 to inherit.
    assert len(closes) == 2, (
        f"expected the stale stream to be closed on the spot, saw {len(closes)} close(s) — "
        f"press 1's stream was handed to press 2"
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
