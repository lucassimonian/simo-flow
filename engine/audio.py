"""Audio capture: mic stream stays open (warm) with a short pre-roll ring buffer.

The stream runs continuously so the mic hardware never sleeps (avoids the
documented 2-5s wake lag / clipped first word on Apple Silicon). A small
pre-roll ring means audio from just *before* the hotkey registered is kept.
"""
import threading
from collections import deque

import numpy as np
import sounddevice as sd

RATE = 16000
PREROLL_SEC = 0.5
MIN_UTTERANCE_SEC = 0.3  # discard accidental taps shorter than this


class Recorder:
    def __init__(self) -> None:
        self._preroll: deque[np.ndarray] = deque(maxlen=int(PREROLL_SEC * RATE / 512) + 1)
        self._chunks: list[np.ndarray] = []
        self._recording = False
        self.level = 0.0
        self._lock = threading.Lock()
        self._stream = sd.InputStream(
            samplerate=RATE, channels=1, dtype="float32", blocksize=512, callback=self._cb
        )

    def _cb(self, indata, frames, t, status) -> None:
        mono = indata[:, 0].copy()
        self.level = float(np.sqrt(np.mean(mono**2)))  # live rms for the overlay meter
        with self._lock:
            self._preroll.append(mono)
            if self._recording:
                self._chunks.append(mono)

    def start_stream(self) -> None:
        self._stream.start()

    def stop_stream(self) -> None:
        self._stream.stop()
        self._stream.close()

    def begin(self) -> None:
        """Hotkey down: start collecting, seeded with the pre-roll."""
        with self._lock:
            self._chunks = list(self._preroll)
            self._recording = True

    def end(self) -> np.ndarray | None:
        """Hotkey up: return float32 utterance, or None if too short."""
        with self._lock:
            self._recording = False
            chunks, self._chunks = self._chunks, []
        if not chunks:
            return None
        samples = np.concatenate(chunks)
        if len(samples) < MIN_UTTERANCE_SEC * RATE:
            return None
        return trim_silence(samples)


def trim_silence(samples: np.ndarray, frame_ms: int = 30, margin_sec: float = 0.2) -> np.ndarray:
    """Cut leading/trailing low-energy audio; whisper hallucinates repeats on noisy tails."""
    frame = int(RATE * frame_ms / 1000)
    n = len(samples) // frame
    if n < 3:
        return samples
    rms = np.sqrt(np.mean(samples[: n * frame].reshape(n, frame) ** 2, axis=1))
    # threshold relative to peak: room noise sits well under 8% of speech peaks
    thresh = max(0.004, float(rms.max()) * 0.08)
    active = np.flatnonzero(rms > thresh)
    if len(active) == 0:
        return samples
    margin = int(margin_sec * RATE)
    start = max(0, active[0] * frame - margin)
    end = min(len(samples), (active[-1] + 1) * frame + margin)
    return samples[start:end]


if __name__ == "__main__":
    # self-check: capture 2s from the real mic and verify shape/duration
    import time

    rec = Recorder()
    rec.start_stream()
    time.sleep(0.8)  # warm-up + fill preroll
    rec.begin()
    print("recording 2s... (make any noise)")
    time.sleep(2.0)
    samples = rec.end()
    rec.stop_stream()
    assert samples is not None, "no samples captured"
    dur = len(samples) / RATE
    assert 2.0 <= dur <= 3.0, f"unexpected duration {dur:.2f}s"
    print(f"captured {dur:.2f}s (includes {PREROLL_SEC}s preroll), peak={np.abs(samples).max():.3f}")
    print("audio self-check OK")
