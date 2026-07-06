"""Simo Flow engine: menu-bar app wiring hotkey -> audio -> stt -> polish -> paste.

fn interactions:
  - HOLD fn and speak, release to commit (push-to-talk)
  - DOUBLE-TAP fn to lock recording hands-free; single tap to stop & commit

Run:  .venv/bin/python -m engine
"""
import threading
import time

import rumps

from engine import audio, inject, polish, store, stt

IDLE_TITLE = "🎤"
REC_TITLE = "🔴"
BUSY_TITLE = "⏳"

HOLD_SEC = 0.35  # fn held longer than this = push-to-talk
DOUBLE_SEC = 0.5  # two taps within this = lock recording


class SimoFlow(rumps.App):
    def __init__(self) -> None:
        super().__init__("Simo Flow", title=IDLE_TITLE, quit_button="Quit Simo Flow")
        self.status_item = rumps.MenuItem("Status: starting...")
        self.mode_item = rumps.MenuItem("Mode: Clean ✨ (click for Exact)", callback=self._toggle_mode)
        self.dash_item = rumps.MenuItem("Open Dashboard", callback=self._open_dashboard)
        self.menu = [self.status_item, self.mode_item, self.dash_item]
        self.recorder = audio.Recorder()
        self.exact_mode = False
        # tap state machine
        self._t_down = 0.0
        self._t_last_tap = 0.0
        self._locked = False
        self._pending_discard: threading.Timer | None = None
        self._meter: rumps.Timer | None = None

    def boot(self) -> None:
        stt.start_server()
        self.recorder.start_stream()
        polish.polish("warm up")  # pull the LLM into memory

        from engine import api

        api.start_in_background()  # dashboard at http://127.0.0.1:7331

        from engine.hotkey import HotkeyListener
        from engine.overlay import RecordingPill

        self.pill = RecordingPill(on_cancel=self._cancel_clicked, on_commit=self._commit_clicked)
        self.listener = HotkeyListener(self._on_press, self._on_release)
        self.listener.attach()
        self.status_item.title = "Status: hold fn — or double-tap to lock"
        self.title = IDLE_TITLE

    def _open_dashboard(self, _item) -> None:
        import webbrowser

        webbrowser.open("http://127.0.0.1:7331")

    # ---- mode toggle -------------------------------------------------
    def _toggle_mode(self, item) -> None:
        self.exact_mode = not self.exact_mode
        item.title = (
            "Mode: Exact 🎯 (click for Clean)" if self.exact_mode else "Mode: Clean ✨ (click for Exact)"
        )

    # ---- fn state machine --------------------------------------------
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
            self.status_item.title = "Status: 🔒 recording — tap fn to stop"
            return
        self._t_last_tap = now
        # lone tap: give a second tap DOUBLE_SEC to arrive, else discard quietly
        self._pending_discard = threading.Timer(DOUBLE_SEC, self._discard)
        self._pending_discard.start()

    def _discard(self) -> None:
        self._pending_discard = None
        if not self._locked:
            self.recorder.end()  # drop the audio
            self._hide_recording()

    # ---- pill buttons ---------------------------------------------------
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

    # ---- pipeline -------------------------------------------------------
    def _commit(self) -> None:
        if self._meter and self._meter.is_alive():
            self._meter.stop()
        samples = self.recorder.end()
        if samples is None:
            self._hide_recording()
            return
        self.title = BUSY_TITLE
        self.pill.busy()
        threading.Thread(target=self._pipeline, args=(samples,), daemon=True).start()

    def _pipeline(self, samples) -> None:
        t0 = time.time()
        try:
            raw = stt.transcribe(samples, initial_prompt=store.dictionary_prompt())
            if raw:
                cleaned = raw if self.exact_mode else polish.polish(raw)
                inject.paste_text(cleaned)
                dt = (time.time() - t0) * 1000
                store.log_dictation(raw, cleaned, int(dt), audio_sec=len(samples) / 16000)
                print(f"[simo] {dt:.0f}ms exact={self.exact_mode} raw={raw!r} pasted={cleaned!r}", flush=True)
        except Exception as e:  # never crash the app on one bad utterance
            print(f"[simo] pipeline error: {e}", flush=True)
        finally:
            self.pill.hide()
            self.title = IDLE_TITLE


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


def main() -> None:
    _lock = _acquire_singleton()
    app = SimoFlow()
    # boot after the runloop starts so the event tap attaches to the right loop
    rumps.Timer(lambda t: (t.stop(), app.boot()), 0.5).start()
    app.run()


if __name__ == "__main__":
    main()
