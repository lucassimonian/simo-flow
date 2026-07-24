"""Simo Flow engine: menu-bar app wiring hotkey -> audio -> stt -> polish -> paste.

fn interactions:
  - HOLD fn and speak, release to commit (push-to-talk)
  - DOUBLE-TAP fn to lock recording hands-free; single tap to stop & commit

Run:  .venv/bin/python -m engine
"""
import queue
import threading
import time

import rumps
from Foundation import NSOperationQueue

from engine import audio, inject, polish, store, stt

IDLE_TITLE = "🎤"
REC_TITLE = "🔴"
BUSY_TITLE = "⏳"

HOLD_SEC = 0.35  # fn held longer than this = push-to-talk
DOUBLE_SEC = 0.5  # two taps within this = lock recording
WARM_PING_SEC = 240  # re-warm the LLM every 4min (Ollama keep_alive is 30m, but
#                      a device sleep can evict early; cheap insurance)

# whisper's output on silence/noise — these are not speech, never paste them
JUNK_TRANSCRIPTS = {"[end of transcript]", "[blank_audio]", "[ inaudible ]", "(silence)", ""}

READY_STATUS = "Ready — hold fn to dictate"


class SimoFlow(rumps.App):
    def __init__(self) -> None:
        # quit_button=None: we install our own Quit item so we can tear down the
        # whisper-server child and mic stream before terminating (rumps' default
        # quit calls NSApp.terminate directly, skipping any cleanup).
        super().__init__("Simo Flow", title=IDLE_TITLE, quit_button=None)
        self.recorder = audio.Recorder()
        self.exact_mode = False
        self.tier = store.get_setting("model_tier", stt.DEFAULT_TIER)

        # Native, terse menu. Mode and Model are submenus with a checkmark on the
        # active choice — the macOS idiom — instead of "(click for X)" labels.
        # A callback-less status row renders greyed, as an info line should.
        self.status_item = rumps.MenuItem("Starting…")

        self.mi_clean = rumps.MenuItem("Clean", callback=self._pick_mode)
        self.mi_exact = rumps.MenuItem("Exact", callback=self._pick_mode)
        self.mi_accurate = rumps.MenuItem("Accurate", callback=self._pick_model)
        self.mi_fast = rumps.MenuItem("Fast", callback=self._pick_model)

        self.dash_item = rumps.MenuItem("Open Dashboard", callback=self._open_dashboard)
        self.quit_item = rumps.MenuItem("Quit Simo Flow", callback=self._quit)
        self.menu = [
            self.status_item,
            None,
            ["Mode", [self.mi_clean, self.mi_exact]],
            ["Model", [self.mi_accurate, self.mi_fast]],
            None,
            self.dash_item,
            None,
            self.quit_item,
        ]
        self._sync_mode()
        self._sync_model()
        # tap state machine (all touched only from the main runloop except where noted)
        self._t_down = 0.0
        self._t_last_tap = 0.0
        self._locked = False
        self._pending_discard: threading.Timer | None = None
        self._meter: rumps.Timer | None = None
        # one worker drains this queue, so pipelines never overlap and can't race
        # each other on the single system clipboard (would paste wrong text)
        self._work: queue.Queue = queue.Queue()

    # ---- main-thread dispatch -------------------------------------------
    # AppKit requires UI mutation on the main thread. Anything touched from a
    # background thread (worker, watchdog, discard timer) routes through here.
    @staticmethod
    def _on_main(fn) -> None:
        NSOperationQueue.mainQueue().addOperationWithBlock_(fn)

    def _ui_title(self, text: str) -> None:
        self._on_main(lambda: setattr(self, "title", text))

    def _ui_status(self, text: str) -> None:
        self._on_main(lambda: setattr(self.status_item, "title", text))

    def boot(self) -> None:
        stt.start_server(self.tier)
        # mic stream is opened on demand in the recorder (keeps the macOS mic
        # indicator off when idle); nothing to start here.
        polish.polish("warm up")  # pull the LLM into memory

        from engine import api

        api.start_in_background()  # dashboard at http://127.0.0.1:7331

        from engine.hotkey import HotkeyListener
        from engine.overlay import RecordingPill

        self.pill = RecordingPill(on_cancel=self._cancel_clicked, on_commit=self._commit_clicked)
        self.listener = HotkeyListener(self._on_press, self._on_release)
        self._try_attach()  # tolerant of missing permissions; retries until granted

        # single serialized pipeline worker
        threading.Thread(target=self._worker, daemon=True, name="simo-pipeline").start()

        # Keep the LLM resident so the first dictation after an idle spell is
        # ~450ms, not ~9s (Ollama evicts the model after its keep_alive window).
        self._warm = rumps.Timer(lambda _t: threading.Thread(
            target=lambda: polish.polish("warm up"), daemon=True).start(), WARM_PING_SEC)
        self._warm.start()

    def _try_attach(self, _timer=None) -> None:
        """Attach the fn-key listener. If Input Monitoring/Accessibility aren't
        granted yet (common on a fresh login-launch), stay alive with a clear
        status and retry — instead of crashing, which under launchd's KeepAlive
        turns into a restart loop."""
        try:
            self.listener.attach()
        except PermissionError:
            self.title = "⚠️"
            self.status_item.title = "Grant Input Monitoring + Accessibility…"
            self.status_item.set_callback(self._open_privacy)
            rumps.Timer(self._retry_attach_once, 3.0).start()
            print("[simo] fn listener needs permissions — retrying every 3s", flush=True)
            return
        self.status_item.title = READY_STATUS
        self.status_item.set_callback(None)
        self.title = IDLE_TITLE

    def _retry_attach_once(self, timer) -> None:
        timer.stop()
        self._try_attach()

    def _open_privacy(self, _item) -> None:
        import webbrowser

        webbrowser.open(
            "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent"
        )

    def _open_dashboard(self, _item) -> None:
        import webbrowser

        webbrowser.open("http://127.0.0.1:7331")

    def _quit(self, _item) -> None:
        """Tear down child process and mic stream, then terminate."""
        print("[simo] quitting — stopping whisper-server and mic stream", flush=True)
        try:
            stt.stop_server()
        except Exception:
            pass
        try:
            self.recorder.stop_stream()
        except Exception:
            pass
        rumps.quit_application()

    # ---- mode submenu (Clean / Exact) --------------------------------
    def _sync_mode(self) -> None:
        self.mi_clean.state = 0 if self.exact_mode else 1
        self.mi_exact.state = 1 if self.exact_mode else 0

    def _pick_mode(self, item) -> None:
        self.exact_mode = item.title == "Exact"
        self._sync_mode()

    # ---- model submenu (Accurate / Fast) -----------------------------
    def _sync_model(self) -> None:
        self.mi_accurate.state = 1 if self.tier == "accurate" else 0
        self.mi_fast.state = 1 if self.tier == "fast" else 0

    def _pick_model(self, item) -> None:
        new = "fast" if item.title == "Fast" else "accurate"
        if new == self.tier:
            return
        self.tier = new
        store.set_setting("model_tier", self.tier)
        self._sync_model()
        self.status_item.title = "Switching model…"
        # swapping the whisper model restarts its server (~2-6s) — off the main
        # thread so the menu bar stays responsive
        threading.Thread(target=self._apply_tier, daemon=True).start()

    def _apply_tier(self) -> None:
        try:
            stt.set_tier(self.tier)
            self._ui_status(READY_STATUS)
        except Exception as e:
            print(f"[simo] model switch failed: {e}", flush=True)
            self._ui_status("Model switch failed — see log")

    # ---- fn state machine (runs on the main runloop) -----------------
    def _on_press(self) -> None:
        self._t_down = time.time()
        if self._locked:
            return  # stop handled on release
        if self._pending_discard:  # second tap arriving — keep recording alive
            self._pending_discard.cancel()
            self._pending_discard = None
        if not self.recorder._recording:
            self.recorder.begin()
        self._show_recording()

    def _on_release(self) -> None:
        now = time.time()
        held = now - self._t_down

        if self._locked:  # any fn release while locked = stop & commit
            self._locked = False
            self._commit()
            return

        if held >= HOLD_SEC:  # push-to-talk release
            self._commit()
            return

        # short tap
        if now - self._t_last_tap <= DOUBLE_SEC:  # second tap: lock on
            self._t_last_tap = 0.0
            self._locked = True
            self.status_item.title = "Recording — tap fn to stop"
            return
        self._t_last_tap = now
        # lone tap: give a second tap DOUBLE_SEC to arrive, else discard quietly
        self._pending_discard = threading.Timer(DOUBLE_SEC, self._discard)
        self._pending_discard.start()

    def _discard(self) -> None:
        """Runs on a threading.Timer thread — dispatch UI teardown to main."""
        self._pending_discard = None
        if not self._locked:
            self.recorder.end()  # drop the audio
            self._on_main(self._hide_recording)

    # ---- pill buttons (AppKit button actions — already on main) --------
    def _cancel_clicked(self) -> None:
        self._locked = False
        if self._pending_discard:
            self._pending_discard.cancel()
            self._pending_discard = None
        self.recorder.end()  # drop the audio
        self._hide_recording()

    def _commit_clicked(self) -> None:
        self._locked = False
        if self._pending_discard:
            self._pending_discard.cancel()
            self._pending_discard = None
        self._commit()

    # ---- overlay ------------------------------------------------------
    def _show_recording(self) -> None:
        self.title = REC_TITLE
        self.pill.show()
        if self._meter is None:
            self._meter = rumps.Timer(self._tick_meter, 0.1)
        if not self._meter.is_alive():
            self._meter.start()

    def _tick_meter(self, _timer) -> None:
        self.pill.set_level(self.recorder.level)

    def _hide_recording(self) -> None:
        if self._meter and self._meter.is_alive():
            self._meter.stop()
        self.pill.hide()
        self.title = IDLE_TITLE

    # ---- pipeline -----------------------------------------------------
    def _commit(self) -> None:
        """Runs on the main runloop. Hands the utterance to the serialized
        worker; never runs the pipeline inline (would block the UI)."""
        self.status_item.title = READY_STATUS  # clear any "Recording…" lock text
        if self._meter and self._meter.is_alive():
            self._meter.stop()
        samples = self.recorder.end()
        if samples is None:
            # Don't paste silence; tell the user why nothing happened.
            reason = self.recorder.reject_reason or "nothing captured"
            if reason != "too short":  # a stray tap isn't worth a message
                self.pill.flash(reason)
            else:
                self._hide_recording()
            self.title = IDLE_TITLE
            return
        self.title = BUSY_TITLE
        self.pill.busy()
        self._work.put(samples)

    def _worker(self) -> None:
        """Single background thread: one utterance processed at a time, in
        order. Serialization is what prevents two pastes racing the clipboard."""
        while True:
            samples = self._work.get()
            self._run_pipeline(samples)

    def _run_pipeline(self, samples) -> None:
        t0 = time.time()
        try:
            raw = stt.transcribe(samples, initial_prompt=store.dictionary_prompt())
            if not raw or raw.strip().lower() in JUNK_TRANSCRIPTS:
                # whisper emits these on silence/noise — never paste them
                self.pill.flash("no speech detected")
                self._ui_title(IDLE_TITLE)
                return
            cleaned = raw if self.exact_mode else polish.polish(raw)
            inject.paste_text(cleaned)
            dt = (time.time() - t0) * 1000
            store.log_dictation(raw, cleaned, int(dt), audio_sec=len(samples) / 16000)
            print(f"[simo] {dt:.0f}ms exact={self.exact_mode} raw={raw!r} pasted={cleaned!r}", flush=True)
            self.pill.hide()
        except Exception as e:  # never crash the app on one bad utterance
            print(f"[simo] pipeline error: {e}", flush=True)
            self.pill.flash("error — see log")
        finally:
            self._ui_title(IDLE_TITLE)


def _acquire_singleton() -> object:
    """One engine max — stacked instances each paste, tripling output."""
    import fcntl
    import os

    lock = open(os.path.expanduser("~/.simo-flow.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit("Simo Flow is already running (found ~/.simo-flow.lock held).")
    return lock  # keep a reference so the fd stays open


def _tee_logs() -> None:
    """Mirror stdout/stderr to ~/.simo-flow.log so failures survive a closed
    terminal (or a .app launch with no terminal at all). The log holds plaintext
    transcripts, so it is created 0600 (owner-only)."""
    import os
    import sys

    path = os.path.expanduser("~/.simo-flow.log")
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    os.chmod(path, 0o600)  # tighten even if it pre-existed world-readable
    log = os.fdopen(fd, "a", buffering=1)

    class _Tee:
        def __init__(self, real, *extra):
            self._real = real  # the genuine stream (may be None under launchd)
            self._streams = [s for s in (real, *extra) if s is not None]

        def write(self, data):
            for s in self._streams:
                s.write(data)

        def flush(self):
            for s in self._streams:
                s.flush()

        def __getattr__(self, name):
            # isatty/fileno/encoding/etc. — libraries like uvicorn probe these.
            # Delegate to the real stream, or a sane default if there isn't one.
            if self._real is not None:
                return getattr(self._real, name)
            if name == "isatty":
                return lambda: False
            raise AttributeError(name)

    sys.stdout = _Tee(sys.__stdout__, log)
    sys.stderr = _Tee(sys.__stderr__, log)


def _install_shutdown_hooks() -> None:
    """Reap the whisper-server child on exit, including the SIGTERM that
    launchctl stop/restart and logout deliver (which otherwise skips the menu
    Quit path). Best-effort — start_server also reaps orphans on next launch."""
    import atexit
    import signal as _signal

    atexit.register(stt.stop_server)

    def _term(_signum, _frame):
        stt.stop_server()
        raise SystemExit(0)

    _signal.signal(_signal.SIGTERM, _term)


def main() -> None:
    _tee_logs()
    _lock = _acquire_singleton()
    _install_shutdown_hooks()
    app = SimoFlow()
    # boot after the runloop starts so the event tap attaches to the right loop
    rumps.Timer(lambda t: (t.stop(), app.boot()), 0.5).start()
    app.run()


if __name__ == "__main__":
    main()
