"""Audio capture: the mic stream is opened only while you are actively
dictating, and closed the instant you stop.

This is deliberate. Holding the stream open continuously (the earlier "warm
mic" approach) keeps macOS's orange microphone-in-use indicator lit for the
whole time the app runs — which, for a privacy-first tool, wrongly signals
"always listening." Opening on demand means that indicator appears only while
you hold fn, matching Wispr Flow. A fresh stream per utterance also sidesteps
the dead-stream failure mode that used to feed whisper silence, which it
transcribed as "[end of transcript]" and pasted.

Every operation that touches the audio device runs on a dedicated thread, never
on the caller's. begin() and end() are reached from the fn-key CGEventTap
callback on the main runloop, and a tap that blocks stalls the *system* input
pipeline — every key in every app stops until it returns. That is not a figure
of speech: it froze a MacBook mid-meeting, when a CoreAudio teardown blocked
here while AirPods were connecting.

So the hotkey path only ever flips flags and hands the device work to the audio
thread. Whatever CoreAudio does with that work — answer in 5ms, or wedge for
two seconds while a device switches — costs at most one dictation.
"""
import queue
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
        # Bumped by every begin(). A device open runs on another thread and can
        # take seconds when it has to retry, by which time the user may have
        # pressed fn again — so its result is only allowed to touch recorder state
        # if it still belongs to the current utterance. See _prepare_device.
        self._generation = 0
        # Every device call is queued here and executed by one dedicated thread,
        # so no caller ever waits on CoreAudio. See _audio_worker.
        self._audio_q: queue.Queue = queue.Queue()
        threading.Thread(target=self._audio_worker, daemon=True, name="simo-audio").start()

    # ---- the audio thread ------------------------------------------------
    def _audio_worker(self) -> None:
        """Run queued device operations, one at a time, in the order requested.

        One thread rather than a pool, because ordering is correctness here: an
        open that overtook the close following it would leave the microphone live
        with the macOS indicator lit — the exact "always listening" impression
        the on-demand design exists to avoid.
        """
        while True:
            op = self._audio_q.get()
            try:
                op()
            except Exception as e:
                # Never let one bad operation kill this thread: it would leave the
                # app unable to record for the rest of the session, silently.
                print(f"[simo] audio operation failed: {type(e).__name__}: {e}", flush=True)

    def _drain_audio_ops(self, timeout: float = 5.0) -> bool:
        """Wait for queued device work to finish. False if it didn't in time.

        NEVER call this from the hotkey path — waiting here is exactly the freeze
        this design prevents. It exists for shutdown and for tests. The queue is
        FIFO, so a sentinel reaching the front means everything before it is done.
        """
        done = threading.Event()
        self._audio_q.put(done.set)
        return done.wait(timeout)

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
        """Hotkey down: arm capture now, open the microphone off-thread.

        Returns in microseconds, and that is the whole point. This is called from
        the fn-key CGEventTap callback on the main runloop; blocking here stops
        every key on the machine, not just this app. v2.2.1 blocked here and froze
        a MacBook mid-meeting.

        Arming is two assignments. Opening the device — the part CoreAudio can sit
        on for as long as it likes while AirPods connect — goes to the audio
        thread. Frames only accumulate once the stream is live, so what the user
        feels is unchanged: the ~100ms open still hides inside the natural pause
        between pressing fn and starting to speak.
        """
        with self._lock:
            self._chunks = []
            self._recording = True
            self._open_failed = False
            self._generation += 1
            generation = self._generation
        self._audio_q.put(lambda: self._prepare_device(generation))

    def _claim(self, generation: int, device: int | None) -> bool:
        """Record a successful open — unless a newer press already owns the recorder.

        Staleness has to be checked on success, not only on failure. A slow open
        that finally succeeds after the user has released and pressed again would
        otherwise publish its stream into the *new* utterance: frames from the new
        press start landing in it, and the close queued by the old press then shuts
        it mid-sentence. The audio lost that way is silent — no error, no
        reject_reason, just a gap in what the user said.
        """
        with self._lock:
            if generation != self._generation:
                return False
            self._device_id = device
            return True

    def _prepare_device(self, generation: int) -> None:
        """Open the mic, recovering from a stale device list. Audio thread only.

        Deliberately never holds `self._lock` across a device call. The capture
        callback and the UI both take that lock, so a device call holding it would
        block them — re-creating, one level down, the freeze this design exists to
        prevent.

        `generation` identifies the press this open belongs to. The retry path
        below can take seconds, and a user whose mic just failed presses fn again
        immediately — so by the time a failure is known, the recorder may already
        belong to a newer utterance that is recording perfectly well. Reporting
        the old failure onto it would kill a good recording.
        """
        device = default_input_device()  # recorded on success as _device_id
        if self._needs_reinit:
            self._reinit_portaudio()
            self._needs_reinit = False
        if self._open_stream():
            if not self._claim(generation, device):
                self._close_stream()
            return

        # A failed open almost always means PortAudio's cached device list went
        # stale — a mic was connected or removed (AirPods) while we were running.
        # Re-initialise and retry once. Without this the failure was terminal:
        # nothing ever refreshed the list, so every press afterwards failed
        # identically until the app was restarted by hand. Seen for real, eleven
        # consecutive times, and the app reported it as "no audio captured".
        #
        # Recovery stays *reactive* — we do not pre-emptively re-initialise just
        # because the device id changed. v2.2.1 did that and froze the machine.
        # It is safe to re-initialise here, on this thread, but a device switch in
        # flight is still the worst possible moment to tear CoreAudio down, and
        # trying the open first usually just works.
        print("[simo] retrying with a fresh PortAudio device list", flush=True)
        self._reinit_portaudio()
        if self._open_stream():
            if not self._claim(generation, default_input_device()):
                self._close_stream()
                return
            print("[simo] mic recovered after re-initialising PortAudio", flush=True)
            return

        self._needs_reinit = True  # next press starts from a clean device list
        with self._lock:
            if generation != self._generation:
                return  # a newer press owns the recorder now; leave it alone
            self._open_failed = True
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
            open_failed = self._open_failed
        # Closing blocks for the same reasons opening does, and this runs on the
        # key-release path — so it goes to the audio thread too. The samples are
        # already in memory; they do not need the device to come back first.
        self._audio_q.put(self._close_stream)
        self.level = 0.0  # drop the meter now, not whenever CoreAudio answers
        self.reject_reason = ""
        if not chunks:
            self.reject_reason = (
                "microphone unavailable — see log" if open_failed else "no audio captured"
            )
            return None
        samples = np.concatenate(chunks)
        # Exactly zero is not quiet — it is a microphone that opened, reported
        # success, and delivered nothing. Real silence in a real room is never
        # bit-exact zero: a live input floor always carries some noise.
        #
        # Worth separating because the causes are ones the user can fix and would
        # never guess from "no speech detected": a hardware mute switch, a
        # revoked microphone permission, or — once this ships as a signed app —
        # a missing audio-input entitlement, where CoreAudio raises no error at
        # all and simply hands over zeroed buffers on schedule. A comparable app
        # reports every one of those as "no speech detected", which reads as
        # "you didn't say anything" and sends people looking in the wrong place.
        if not np.any(samples):
            self._needs_reinit = True
            print(
                f"[simo] captured {len(samples) / RATE:.1f}s of bit-exact silence — the "
                "microphone opened but delivered no signal (muted, or permission revoked)",
                flush=True,
            )
            self.reject_reason = "microphone delivered silence — check it isn't muted"
            return None
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
        """Cleanup hook for app quit.

        Waits, briefly, unlike everything else here: quit is the one moment the
        microphone must actually be released before the process goes away, and
        there is no keyboard tap left to stall. Bounded so a wedged device can
        delay the quit but never prevent it.
        """
        with self._lock:
            self._recording = False
        self._audio_q.put(self._close_stream)
        if not self._drain_audio_ops(timeout=2.0):
            print("[simo] mic did not close within 2s — quitting anyway", flush=True)

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
    samples = rec.end()  # queues the close; the samples are already in hand
    assert samples is not None, "no samples captured"
    dur = len(samples) / RATE
    assert 1.5 <= dur <= 2.5, f"unexpected duration {dur:.2f}s"
    assert rec._drain_audio_ops(timeout=5.0), "audio thread did not finish closing"
    assert rec._stream is None, "stream not closed after end()"
    print(f"captured {dur:.2f}s, peak={np.abs(samples).max():.3f}, stream closed OK")
    print("audio self-check OK")
