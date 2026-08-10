"""Audio capture: the mic stream is opened only while you are actively
dictating, and closed the instant you stop.

This is deliberate. Holding the stream open continuously (the earlier "warm
mic" approach) keeps macOS's orange microphone-in-use indicator lit for the
whole time the app runs — which, for a privacy-first tool, wrongly signals
"always listening." Opening on demand means that indicator appears only while
you hold fn, matching Wispr Flow. A fresh stream per utterance also sidesteps
the dead-stream failure mode that used to feed whisper silence, which it
transcribed as "[end of transcript]" and pasted.

The stream is (re)opened in begin(); PortAudio is re-initialised first so a mic
connected or removed since the last utterance (AirPods, a headset) is picked up
rather than served from PortAudio's cached device list.
"""
import threading

import numpy as np
import sounddevice as sd

RATE = 16000
MIN_UTTERANCE_SEC = 0.3  # discard accidental taps shorter than this
SILENCE_RMS = 0.004  # whole utterance quieter than this = no speech, never paste it


def _load_coreaudio():
    """CoreAudio, for asking macOS which input device is current *right now*.

    PortAudio caches its device list at initialisation, so it cannot answer this:
    the cache is the very thing that goes stale when AirPods connect. Reading the
    property straight from CoreAudio bypasses that and costs microseconds.

    Returns None if the framework can't be loaded, in which case device-change
    detection is skipped and the reactive retry in begin() is the only safety net.
    """
    try:
        import ctypes
        import ctypes.util

        path = ctypes.util.find_library("CoreAudio")
        if not path:
            return None
        lib = ctypes.cdll.LoadLibrary(path)

        class _Address(ctypes.Structure):
            _fields_ = [
                ("mSelector", ctypes.c_uint32),
                ("mScope", ctypes.c_uint32),
                ("mElement", ctypes.c_uint32),
            ]

        fourcc = lambda s: int.from_bytes(s.encode(), "big")  # noqa: E731
        # kAudioHardwarePropertyDefaultInputDevice / kAudioObjectPropertyScopeGlobal
        return lib, _Address(fourcc("dIn "), fourcc("glob"), 0), ctypes
    except Exception:
        return None


_COREAUDIO = _load_coreaudio()


def default_input_device() -> int | None:
    """The id of the input device macOS would use right now, or None if unknown.

    Deliberately not routed through sounddevice: PortAudio would answer from the
    same cache we are trying to check.
    """
    if _COREAUDIO is None:
        return None
    lib, address, ctypes = _COREAUDIO
    try:
        dev = ctypes.c_uint32(0)
        size = ctypes.c_uint32(4)
        err = lib.AudioObjectGetPropertyData(
            ctypes.c_uint32(1),  # kAudioObjectSystemObject
            ctypes.byref(address),
            0,
            None,
            ctypes.byref(size),
            ctypes.byref(dev),
        )
        return dev.value if err == 0 else None
    except Exception:
        return None


class Recorder:
    def __init__(self) -> None:
        self._chunks: list[np.ndarray] = []
        self._recording = False
        self.level = 0.0
        self.reject_reason = ""  # why the last end() returned None, for the UI
        self._lock = threading.Lock()
        self._stream: sd.InputStream | None = None
        self._needs_reinit = False  # set after a silent capture or a failed open
        self._open_failed = False  # the mic never opened, as opposed to hearing nothing
        self._device_id: int | None = None  # input device of the last successful open
        self.on_stream_lost = None  # retained for API compatibility; unused on-demand

    def _cb(self, indata, frames, t, status) -> None:
        if status:
            # PortAudio overflow/underflow/device errors surface here.
            print(f"[simo] audio status: {status}", flush=True)
        mono = indata[:, 0].copy()
        self.level = float(np.sqrt(np.mean(mono**2)))  # live rms for the overlay meter
        with self._lock:
            if self._recording:
                self._chunks.append(mono)

    @property
    def is_recording(self) -> bool:
        """Whether a capture is in progress. Callers asked `_recording` directly
        before this existed, which leaked the lock discipline across modules."""
        with self._lock:
            return self._recording

    # ---- capture ---------------------------------------------------------
    def begin(self) -> None:
        """Hotkey down: open a fresh mic stream and start capturing.

        Opening here (not at launch) is what keeps the macOS mic indicator off
        when idle. The ~100ms open latency is hidden by the natural pause
        between pressing fn and starting to speak.
        """
        with self._lock:
            self._chunks = []
            self._recording = True
        # Re-initialise when the device list is known to be suspect — either a
        # previous capture told us so, or macOS is now pointing at a different
        # input device than the one we last opened. That second check is what makes
        # connecting AirPods mid-session a non-event instead of a failed press:
        # PortAudio's cache is refreshed *before* we ask it for a device that
        # moved, rather than after it errors. One CoreAudio property read, so it
        # costs nothing on the common path where nothing has changed.
        device = default_input_device()
        if self._needs_reinit or (device is not None and device != self._device_id):
            self._reinit_portaudio()
            self._needs_reinit = False
        if self._open_stream():
            self._device_id = device
            return

        # A failed open almost always means PortAudio's cached device list went
        # stale — a mic was connected or removed (AirPods) while we were running.
        # Re-initialise and retry once. Without this the failure was terminal:
        # nothing ever refreshed the list, so every press afterwards failed
        # identically until the app was restarted by hand. Seen for real, eleven
        # consecutive times, and the app reported it as "no audio captured".
        print("[simo] retrying with a fresh PortAudio device list", flush=True)
        self._reinit_portaudio()
        if self._open_stream():
            self._device_id = default_input_device()
            print("[simo] mic recovered after re-initialising PortAudio", flush=True)
            return

        self._needs_reinit = True  # next press starts from a clean device list
        self._open_failed = True
        with self._lock:
            self._recording = False

    @staticmethod
    def _reinit_portaudio() -> None:
        """Drop and rebuild PortAudio's device list. Best-effort by design: if this
        fails there is nothing better to try, and it must not stop a dictation."""
        try:
            sd._terminate()
            sd._initialize()
        except Exception as e:
            print(f"[simo] PortAudio re-init failed: {e}", flush=True)

    def _open_stream(self) -> bool:
        """Open and start the mic stream. False if the device refused."""
        try:
            self._stream = sd.InputStream(
                samplerate=RATE, channels=1, dtype="float32", blocksize=512, callback=self._cb
            )
            self._stream.start()
            self._open_failed = False
            return True
        except Exception as e:
            print(f"[simo] mic open failed: {e}", flush=True)
            self._stream = None
            return False

    def _close_stream(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass  # already gone
            self._stream = None
        self.level = 0.0

    def end(self) -> np.ndarray | None:
        """Hotkey up: close the stream and return the float32 utterance, or None
        if unusable. Sets reject_reason so the caller can tell the user why."""
        with self._lock:
            self._recording = False
            chunks, self._chunks = self._chunks, []
        self._close_stream()
        self.reject_reason = ""
        if not chunks:
            self.reject_reason = (
                "microphone unavailable — see log" if self._open_failed else "no audio captured"
            )
            return None
        samples = np.concatenate(chunks)
        if len(samples) < MIN_UTTERANCE_SEC * RATE:
            self.reject_reason = "too short"
            return None
        trimmed = trim_silence(samples)
        # A dead/muted mic yields near-zeros, which whisper turns into
        # "[end of transcript]". Refuse to send silence downstream.
        if float(np.sqrt(np.mean(trimmed**2))) < SILENCE_RMS:
            # silence can mean the device changed under us — refresh PortAudio's
            # device list before the next capture so a new mic is picked up
            self._needs_reinit = True
            self.reject_reason = "no speech detected — is the mic muted?"
            return None
        return trimmed

    def stop_stream(self) -> None:
        """Cleanup hook for app quit."""
        with self._lock:
            self._recording = False
        self._close_stream()

    def start_stream(self) -> None:
        """No-op: the stream is opened on demand in begin(). Retained so callers
        that expect the old warm-mic lifecycle don't break."""
        return None


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
    # self-check: on-demand open, capture 2s from the real mic, verify shape
    import time

    rec = Recorder()
    rec.begin()  # opens the stream
    print("recording 2s... (make any noise)")
    time.sleep(2.0)
    samples = rec.end()  # closes the stream
    assert samples is not None, "no samples captured"
    dur = len(samples) / RATE
    assert 1.5 <= dur <= 2.5, f"unexpected duration {dur:.2f}s"
    assert rec._stream is None, "stream not closed after end()"
    print(f"captured {dur:.2f}s, peak={np.abs(samples).max():.3f}, stream closed OK")
    print("audio self-check OK")
