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

    def _handle(self, proxy, etype, event, refcon):
        # macOS disables slow taps; re-enable and let the event pass
        if etype in (kCGEventTapDisabledByTimeout, kCGEventTapDisabledByUserInput):
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
