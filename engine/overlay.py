"""Wispr-style recording pill: black capsule, ✕ cancel circle, dotted voice
meter, white ✓ commit circle. Clickable without stealing focus (non-activating
panel). All public methods are safe to call from any thread.
"""
from collections import deque

import objc
from Foundation import NSObject, NSOperationQueue

from AppKit import (
    NSBackingStoreBuffered,
    NSButton,
    NSColor,
    NSFont,
    NSMakeRect,
    NSMutableAttributedString,
    NSPanel,
    NSScreen,
    NSStatusWindowLevel,
    NSTextAlignmentCenter,
    NSTextField,
    NSView,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
)

W, H, BOTTOM = 170, 48, 80
BTN = 32
PAD = (H - BTN) / 2
N_DOTS = 9


class _PillTarget(NSObject):
    """Objective-C action target bridging button clicks to python callbacks."""

    def cancelPressed_(self, sender):
        if getattr(self, "on_cancel", None):
            self.on_cancel()

    def commitPressed_(self, sender):
        if getattr(self, "on_commit", None):
            self.on_commit()


def _circle_button(x: float, title: str, bg: NSColor, fg: NSColor) -> NSButton:
    b = NSButton.alloc().initWithFrame_(NSMakeRect(x, PAD, BTN, BTN))
    b.setBordered_(False)
    b.setWantsLayer_(True)
    b.layer().setCornerRadius_(BTN / 2)
    b.layer().setBackgroundColor_(bg.CGColor())
    at = NSMutableAttributedString.alloc().initWithString_(title)
    at.addAttribute_value_range_(NSForegroundColorAttributeName, fg, (0, len(title)))
    at.addAttribute_value_range_(NSFontAttributeName, NSFont.boldSystemFontOfSize_(14), (0, len(title)))
    b.setAttributedTitle_(at)
    return b


class RecordingPill:
    def __init__(self, on_cancel=None, on_commit=None) -> None:
        screen = NSScreen.mainScreen().frame()
        rect = NSMakeRect((screen.size.width - W) / 2, BOTTOM, W, H)
        self.panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            rect,
            NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
            NSBackingStoreBuffered,
            False,
        )
        self.panel.setLevel_(NSStatusWindowLevel)
        self.panel.setOpaque_(False)
        self.panel.setBackgroundColor_(NSColor.clearColor())
        self.panel.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces)
        self.panel.setBecomesKeyOnlyIfNeeded_(True)

        root = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, W, H))
        root.setWantsLayer_(True)
        root.layer().setCornerRadius_(H / 2)
        root.layer().setBackgroundColor_(
            NSColor.colorWithCalibratedRed_green_blue_alpha_(0.04, 0.04, 0.05, 0.97).CGColor()
        )

        self._target = _PillTarget.alloc().init()
        self._target.on_cancel = on_cancel
        self._target.on_commit = on_commit

        cancel = _circle_button(
            PAD,
            "✕",
            NSColor.colorWithCalibratedRed_green_blue_alpha_(0.28, 0.28, 0.3, 1.0),
            NSColor.whiteColor(),
        )
        cancel.setTarget_(self._target)
        cancel.setAction_("cancelPressed:")

        commit = _circle_button(W - PAD - BTN, "✓", NSColor.whiteColor(), NSColor.blackColor())
        commit.setTarget_(self._target)
        commit.setAction_("commitPressed:")

        self.dots = NSTextField.alloc().initWithFrame_(
            NSMakeRect(PAD + BTN, 0, W - 2 * (PAD + BTN), H)
        )
        self.dots.setEditable_(False)
        self.dots.setBordered_(False)
        self.dots.setDrawsBackground_(False)
        self.dots.setAlignment_(NSTextAlignmentCenter)
        self.dots.setFont_(NSFont.systemFontOfSize_(13))
        self.dots.setTextColor_(NSColor.whiteColor())
        self.dots.setStringValue_("·" * N_DOTS)

        root.addSubview_(cancel)
        root.addSubview_(self.dots)
        root.addSubview_(commit)
        self.panel.setContentView_(root)

        self._levels: deque[float] = deque([0.0] * N_DOTS, maxlen=N_DOTS)

    # ---- threading helper ------------------------------------------------
    @staticmethod
    def _on_main(fn) -> None:
        def block() -> None:  # pyobjc blocks MUST return None (void)
            fn()

        NSOperationQueue.mainQueue().addOperationWithBlock_(block)

    # ---- public API --------------------------------------------------------
    def show(self) -> None:
        self._levels.extend([0.0] * N_DOTS)

        def _do():
            self.dots.setStringValue_("·" * N_DOTS)
            self.panel.orderFrontRegardless()

        self._on_main(_do)

    def set_level(self, rms: float) -> None:
        """Scrolling dot waveform: each dot is one recent level sample."""
        self._levels.append(rms)
        chars = "".join("·" if l < 0.008 else "•" if l < 0.05 else "●" for l in self._levels)
        self._on_main(lambda: self.dots.setStringValue_(chars))

    def busy(self) -> None:
        self._on_main(lambda: self.dots.setStringValue_("· · ·"))

    def hide(self) -> None:
        self._on_main(lambda: self.panel.orderOut_(None))
