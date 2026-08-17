"""Global hotkey: fn to dictate, via a CONSUMING CGEventTap.

We swallow pure-fn flagsChanged events so macOS never sees them — this kills
the system emoji picker / dictation popup regardless of the user's
"Press fn key to..." setting. All other events pass through untouched.

Needs Input Monitoring + Accessibility permissions.
"""
from Quartz import (
    CFMachPortCreateRunLoopSource,
    CFRunLoopAddSource,
    CFRunLoopGetCurrent,
    CFRunLoopRun,
    CFRunLoopStop,
    CFRunLoopWakeUp,
    CGEventGetFlags,
    CGEventTapCreate,
    CGEventTapEnable,
    kCFRunLoopCommonModes,
    kCGEventFlagsChanged,
    kCGEventTapDisabledByTimeout,
    kCGEventTapDisabledByUserInput,
    kCGEventTapOptionDefault,
    kCGHeadInsertEventTap,
    kCGSessionEventTap,
)

FN_FLAG = 0x800000  # kCGEventFlagMaskSecondaryFn


class HotkeyListener:
    """Calls on_press() when fn goes down, on_release() when it comes up.

    Consumes the fn flagsChanged events (returns None) so the system's own
    fn-key action (emoji picker, Apple dictation) never fires.
    """

    def __init__(self, on_press, on_release) -> None:
        self.on_press = on_press
        self.on_release = on_release
        self._down = False
        self._tap = None
        self._loop = None  # the runloop serve() is driving, for stop()

    def _handle(self, proxy, etype, event, refcon):
        # macOS disables slow taps; re-enable and let the event pass
        if etype in (kCGEventTapDisabledByTimeout, kCGEventTapDisabledByUserInput):
            # Reset state: we may have missed the matching key-up while disabled,
            # which would otherwise leave fn stuck "down" and unresponsive.
            if self._down:
                self._down = False
                self.on_release()
            CGEventTapEnable(self._tap, True)
            return event
        if etype == kCGEventFlagsChanged:
            fn_now = bool(CGEventGetFlags(event) & FN_FLAG)
            if fn_now != self._down:
                self._down = fn_now
                (self.on_press if fn_now else self.on_release)()
                return None  # CONSUME: system never sees the fn press
        return event

    def attach(self) -> None:
        """Create the tap and add it to the current runloop. Raises if blocked.

        Try the HID-level tap first: hardware fn events reach it BEFORE the
        system's globe-key handler, so consuming there kills the emoji picker
        for real keyboards too. Fall back to session level if denied.
        """
        from Quartz import kCGHIDEventTap as _hid

        for location in (_hid, kCGSessionEventTap):
            self._tap = CGEventTapCreate(
                location,
                kCGHeadInsertEventTap,
                kCGEventTapOptionDefault,  # filtering tap (can consume), not listen-only
                1 << kCGEventFlagsChanged,
                self._handle,
                None,
            )
            if self._tap is not None:
                break
        if self._tap is None:
            raise PermissionError(
                "CGEventTap failed — grant Input Monitoring (and Accessibility) to this app "
                "in System Settings > Privacy & Security, then relaunch."
            )
        source = CFMachPortCreateRunLoopSource(None, self._tap, 0)
        CFRunLoopAddSource(CFRunLoopGetCurrent(), source, kCFRunLoopCommonModes)
        CGEventTapEnable(self._tap, True)

    def serve(self) -> None:
        """Drive this thread's runloop until stop(). Call on the thread that attached.

        Deliberately its own thread rather than the app's main runloop. macOS
        switches off a tap that does not answer promptly, and the main runloop is
        where every AppKit redraw, menu update and timer already competes — so a
        busy interface can get the fn key disabled with nothing on screen to say
        why. The recovery path in _handle puts it back, but recovery is a worse
        guarantee than never being disabled: while it is off, key-ups are missed.

        The tap must be added to the runloop that services it, so attach() and
        serve() have to run on the same thread.
        """
        self._loop = CFRunLoopGetCurrent()
        CFRunLoopRun()

    def stop(self) -> None:
        """End serve(). Safe to call from another thread — which is the point, since
        the serving thread is blocked inside CFRunLoopRun and cannot end itself."""
        if self._loop is not None:
            CFRunLoopStop(self._loop)
            CFRunLoopWakeUp(self._loop)  # a runloop idle with no sources ignores stop alone


if __name__ == "__main__":
    # manual check: run, hold/release fn, see events print. Ctrl+C to quit.
    from Quartz import CFRunLoopRun

    listener = HotkeyListener(
        on_press=lambda: print("fn DOWN — recording would start"),
        on_release=lambda: print("fn UP — transcribe/polish/paste would run"),
    )
    listener.attach()
    print("listening for fn key (consuming)... (Ctrl+C to quit)")
    CFRunLoopRun()
