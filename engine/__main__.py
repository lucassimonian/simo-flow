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
        # _pending_discard is written from the main runloop and from the Timer's
        # own thread when it fires; the lock keeps cancel-vs-fire deterministic.
        self._pending_discard: threading.Timer | None = None
        self._tap_lock = threading.Lock()
        # Exactly one of {discard timer, ✕, ✓/release} may consume a recording.
        # See _claim_utterance for why Timer.cancel() can't be trusted for this.
        self._consumed = False
        self._fn_down = False  # fn physically held right now (see _claim_utterance)
        self._focus: dict | None = None  # window focused when recording started
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
        with self._tap_lock:
            self._fn_down = True
        if self._locked:
            return  # stop handled on release
        with self._tap_lock:
            if self._pending_discard:  # second tap arriving — keep recording alive
                self._pending_discard.cancel()
                self._pending_discard = None
        if not self.recorder.is_recording:
            with self._tap_lock:
                self._consumed = False  # new utterance, up for grabs again
            self.recorder.begin()
            # Where the user is *now* is where the text belongs. By the time the
            # pipeline finishes (up to ~2s) they may have clicked elsewhere.
            self._focus = inject.capture_focus()
        self._show_recording()

    def _on_release(self) -> None:
        with self._tap_lock:
            self._fn_down = False
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

    def _claim_utterance(self, *, skip_if_key_down: bool = False) -> bool:
        """True only for the first caller to consume the current recording.

        Three paths can end one utterance — the discard timer, ✕, and ✓/release —
        and `Timer.cancel()` is not authoritative: once the timer body has started
        running, cancel() does nothing and returns as if it had worked. Without a
        shared flag, a ✓ pressed at the moment the timer fires had both threads
        call `recorder.end()`; the timer took the audio and the user's deliberate
        commit was reported as "no audio captured" and silently dropped.

        So consumers agree through this flag rather than by cancelling. Cancelling
        is still attempted, because skipping a timer that hasn't started is
        cheaper than letting it run and lose the race.
        """
        with self._tap_lock:
            if self._pending_discard is not None:
                self._pending_discard.cancel()
                self._pending_discard = None
            # `skip_if_key_down` is the discard timer's guard. The timer's job is
            # "no second tap arrived, so bin this" — but a tap that is physically
            # still held *has* arrived; we just haven't seen its release yet. The
            # timer firing in that gap would delete a recording the user is still
            # speaking into, and the release would then engage the hands-free lock
            # over nothing. Checked inside the same lock as the claim so the key
            # state can't change between the two.
            if skip_if_key_down and self._fn_down:
                return False
            if self._consumed:
                return False
            self._consumed = True
            return True

    def _discard(self) -> None:
        """Runs on a threading.Timer thread — dispatch UI teardown to main."""
        if not self._claim_utterance(skip_if_key_down=True):
            return  # ✓ or ✕ got here first; the audio is theirs
        if not self._locked:
            self.recorder.end()  # drop the audio
            self._on_main(self._hide_recording)

    # ---- pill buttons (AppKit button actions — already on main) --------
    def _cancel_clicked(self) -> None:
        self._locked = False
        if not self._claim_utterance():
            return
        self.recorder.end()  # drop the audio
        self._hide_recording()

    def _commit_clicked(self) -> None:
        self._locked = False
        self._commit()  # claims the utterance itself

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
        if not self._claim_utterance():
            # Someone else already ended this recording. Put the UI back rather
            # than leaving "Recording — tap fn to stop" on screen for ever.
            self.status_item.title = READY_STATUS
            self.title = IDLE_TITLE
            return
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
        self.pill.busy("Transcribing")
        # The focus snapshot travels with the audio: the worker may run after the
        # user has already moved on, and it needs where they *were*.
        self._work.put((samples, self._focus))
        self._focus = None

    def _worker(self) -> None:
        """Single background thread: one utterance processed at a time, in
        order. Serialization is what prevents two pastes racing the clipboard."""
        while True:
            samples, focus = self._work.get()
            self._run_pipeline(samples, focus)

    def _run_pipeline(self, samples, focus: dict | None = None) -> None:
        t0 = time.time()
        try:
            raw = stt.transcribe(samples, initial_prompt=store.dictionary_prompt())
            if not raw or raw.strip().lower() in JUNK_TRANSCRIPTS:
                # whisper emits these on silence/noise — never paste them
                self.pill.flash("no speech detected")
                self._ui_title(IDLE_TITLE)
                return
            if self.exact_mode:
                cleaned = raw
            else:
                # polish() owns the skip decision; needs_cleanup is consulted here
                # only so the pill doesn't announce a stage that won't happen.
                if polish.needs_cleanup(raw):
                    self.pill.busy("Polishing")
                cleaned = polish.polish(raw)
            # Stopped when the keystroke lands, not when paste_text returns:
            # the clipboard restore afterwards is housekeeping the user never
            # waits for, and counting it made every reported latency ~300ms
            # pessimistic.
            seen_at: list[float] = []
            pasted = inject.paste_text(
                cleaned, focus=focus, on_pasted=lambda: seen_at.append(time.time())
            )
            dt = ((seen_at[0] if seen_at else time.time()) - t0) * 1000
            # Recorded even when the paste failed. Returning early here used to
            # skip this, so a refused paste (revoked Accessibility, target app
            # gone) destroyed the transcription outright — the user had spoken,
            # waited, and got nothing, with no copy anywhere. Saved, it is still
            # recoverable from the dashboard.
            store.log_dictation(raw, cleaned, int(dt), audio_sec=len(samples) / 16000)
            if not pasted:
                # inject already logged the specific reason.
                self.pill.flash("couldn't paste — saved to dashboard")
                self._ui_title(IDLE_TITLE)
                return
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


LOG_PATH = "~/.simo-flow.log"
# launchd-owned; catches failures occurring before _tee_logs() runs (a broken
# venv, a missing interpreter). Never a sink for anything we print ourselves.
BOOT_LOG_PATH = "~/.simo-flow.boot.log"
# The log is a verbatim record of everything ever dictated, so it can't grow for
# ever. Trimmed at startup rather than on a timer: it's the one moment nothing
# else is writing, and a dictation app is restarted often enough.
MAX_LOG_BYTES = 8_000_000
KEEP_LOG_BYTES = 2_000_000


def _same_file(stream, path: str) -> bool:
    """Whether `stream` is already writing to `path` (same device + inode).

    The LaunchAgent used to redirect stdout and stderr to the very file
    _tee_logs() opens, so every line was written twice — once by launchd, once by
    us — doubling a file that already held plaintext transcripts. The plist no
    longer does that, but an install that predates the change still will, so the
    duplication is detected rather than assumed away.
    """
    import os

    try:
        a = os.fstat(stream.fileno())
        b = os.stat(path)
    except (OSError, ValueError, AttributeError):
        return False  # no stream (launchd), closed fd, or nothing at that path
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def _trim_log(path: str, max_bytes: int = MAX_LOG_BYTES, keep_bytes: int = KEEP_LOG_BYTES) -> bool:
    """Keep the newest `keep_bytes` once the log passes `max_bytes`. True if trimmed.

    Truncates in place instead of renaming: another writer may hold this file
    open, and a renamed inode would keep receiving their writes while the fresh
    file stayed empty.
    """
    import os

    try:
        if os.path.getsize(path) <= max_bytes:
            return False
        with open(path, "rb") as f:
            f.seek(-keep_bytes, os.SEEK_END)
            f.readline()  # drop the partial line we landed in the middle of
            tail = f.read()
        with open(path, "wb") as f:  # truncates the existing inode
            f.write(b"[simo] log trimmed to the most recent entries\n")
            f.write(tail)
        os.chmod(path, 0o600)
        return True
    except OSError:
        return False  # a log we can't trim must never stop the app starting


def _keep_stream(stream) -> bool:
    """Whether `stream` should stay a sink once we have our own log open.

    A tty is a developer running the app by hand and should still see output.
    Anything file-backed is launchd's redirect to the boot log, and keeping it wrote
    dictated transcripts into a world-readable file for the life of the process.

    Asking "is this a terminal?" rather than "is this one specific path?" is the
    point: the previous check only recognised the path launchd used to use, and
    silently stopped protecting anything the moment that path changed.
    """
    try:
        return stream is not None and stream.isatty()
    except (OSError, ValueError, AttributeError):
        return False


def _harden_boot_log() -> None:
    """Make the launchd boot log owner-only.

    launchd creates it with the default umask — mode 644, readable by every
    account on the machine. Nothing else hardens it, and a crash trace can still
    name file paths even now that transcripts never reach it.

    A module-level function rather than four lines inside _tee_logs() because
    _tee_logs() replaces the process's stdout and stderr, which makes it
    effectively untestable — and this guard shipped untested for exactly that
    reason. CI caught it as a surviving mutation.
    """
    import os

    try:
        boot = os.path.expanduser(BOOT_LOG_PATH)
        if os.path.exists(boot):
            os.chmod(boot, 0o600)
    except OSError:
        pass  # a boot log we can't chmod must not stop the app starting


def _tee_logs() -> None:
    """Mirror stdout/stderr to ~/.simo-flow.log so failures survive a closed
    terminal (or a .app launch with no terminal at all). The log holds plaintext
    transcripts, so it is created 0600 (owner-only) and trimmed when oversized.

    Under launchd, stdout and stderr are redirected to the boot log, and those
    streams are deliberately dropped rather than kept as additional sinks — see the
    comment at the tee assignment. A terminal is kept, so running the app by hand
    still prints where you can see it."""
    import os
    import sys

    path = os.path.expanduser(LOG_PATH)
    _trim_log(path)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    os.chmod(path, 0o600)  # tighten even if it pre-existed world-readable
    log = os.fdopen(fd, "a", buffering=1)

    _harden_boot_log()

    # An older LaunchAgent points stdout/stderr at our own log. Adding our handle
    # on top of that writes every line twice, so let the real stream be the only
    # writer.
    if _same_file(sys.__stdout__, path) or _same_file(sys.__stderr__, path):
        print("[simo] launchd already logs to this file — run ./simo install to "
              "stop every line being recorded twice", file=sys.__stdout__, flush=True)
        return

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

    # A redirected (file-backed) stream is launchd's boot log, and must NOT stay a
    # sink: every pipeline line carries the raw and cleaned transcript, so keeping
    # it wrote dictated speech into a world-readable file for the whole life of the
    # process. Found in production at 45KB and 36 transcript lines, mode 644.
    #
    # A tty means a developer is running the app by hand and should still see
    # output. So the test is "is this a terminal?", not "which file is it?" — the
    # previous check only recognised the one specific path launchd used to use, and
    # silently stopped protecting anything the moment that path changed.
    sys.stdout = _Tee(sys.__stdout__ if _keep_stream(sys.__stdout__) else None, log)
    sys.stderr = _Tee(sys.__stderr__ if _keep_stream(sys.__stderr__) else None, log)


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
