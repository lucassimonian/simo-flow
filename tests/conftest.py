"""Shared test setup.

The one job here is stopping audio work from outliving the test that started it.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def _finish_audio_work_before_fakes_are_removed(monkeypatch):
    """Wait for every Recorder's queued device work before the test's fakes go.

    Device operations run on a background thread by design — that is what keeps
    the fn-key tap from blocking and freezing the machine. The side effect in
    tests is that a queued operation can still be running when pytest restores
    the real `sounddevice`, at which point it tears down PortAudio for real while
    another test is opening a stream. CoreAudio's real-time thread then reads
    freed memory: a segmentation fault about once every seven runs, with every
    test reported as passing and no failure to point at.

    Depending on `monkeypatch` is what makes this work. Finalisers run in reverse
    order of setup, so requesting it here forces it to be set up first and torn
    down last — meaning this drain happens while the fakes are still installed.
    """
    yield
    from engine import audio

    assert audio.drain_all(), "a Recorder never finished its queued device work"
