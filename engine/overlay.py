"""Recording pill: a floating capsule shown while you dictate.

Built from genuine macOS materials, not a flat fill. On macOS 26 the capsule is
real Liquid Glass (`NSGlassEffectView`), which refracts and specularly highlights
what is behind it; older systems fall back to `NSVisualEffectView` vibrancy. A
native window shadow gives depth, and a live waveform of rounded bars tracks your
voice level in real time. The label states — Transcribing, Polishing — replace the
bars while the pipeline runs, so a slow run is distinguishable from a hung one.
✕ cancels, ✓ commits.

The panel is non-activating, so clicking it never steals focus from the app you
are dictating into. All public methods are safe to call from any thread.
"""
from collections import deque

import AppKit  # module-level access so Tahoe-only classes can be probed at runtime
import objc
from Foundation import NSObject, NSOperationQueue, NSMakeRect

from AppKit import (
    NSBackingStoreBuffered,
    NSBezierPath,
    NSButton,
    NSColor,
    NSFont,
    NSPanel,
    NSScreen,
    NSStatusWindowLevel,
    NSView,
    NSVisualEffectView,
    NSVisualEffectBlendingModeBehindWindow,
    NSVisualEffectStateActive,
    NSVisualEffectMaterialHUDWindow,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
    NSMutableAttributedString,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
)

W, H, BOTTOM = 196, 54, 92
BTN = 38
PAD = (H - BTN) / 2
N_BARS = 18
BAR_W = 3.0
BAR_GAP = 3.0
MIN_BAR = 3.0
LEVEL_FULL = 0.12  # rms that maps to a full-height bar

# The waveform and glyphs are white, so the substrate has to stay dark or they
# vanish over a light window. Measured against pure white with
# tools/pill_preview.py: NSGlassEffectView's own tintColor is a subtle hue wash,
# NOT an opacity overlay — at any alpha it left the pill and its white content
# almost invisible on a white background. So contrast is guaranteed by an
# explicit scrim behind the content instead, and the glass is left to do what
# only it can: refract and specularly highlight the edges.
#
# Calibration knob: lower to show more of what's behind through the pill, raise
# if white content ever looks washed out. Re-check both extremes with
#   ./.venv/bin/python tools/pill_preview.py --tint <n>
SCRIM_ALPHA = 0.42
GLASS_TINT_ALPHA = 0.18  # a whisper of depth; contrast comes from the scrim


class _WaveformView(NSView):
    """Custom-drawn scrolling waveform: each bar is one recent level sample,
    grown symmetrically from the vertical centre like a real audio meter."""

    def initWithFrame_(self, frame):
        self = objc.super(_WaveformView, self).initWithFrame_(frame)
        if self is not None:
            self._levels = deque([0.0] * N_BARS, maxlen=N_BARS)
            self._msg = None  # when set, draw text instead of bars
        return self

    def isFlipped(self):
        return False

    def push_(self, level):
        self._msg = None
        self._levels.append(float(level))
        self.setNeedsDisplay_(True)

    def setLevels_(self, values):
        self._msg = None
        self._levels = deque(values, maxlen=N_BARS)
        self.setNeedsDisplay_(True)

    def showMessage_(self, text):
        self._msg = text
        self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        b = self.bounds()
        if self._msg is not None:
            self._draw_message(b)
            return
        n = len(self._levels)
        span = n * BAR_W + (n - 1) * BAR_GAP
        x0 = (b.size.width - span) / 2.0
        cy = b.size.height / 2.0
        max_h = b.size.height * 0.68
        for i, lvl in enumerate(self._levels):
            h = MIN_BAR + min(1.0, lvl / LEVEL_FULL) * (max_h - MIN_BAR)
            x = x0 + i * (BAR_W + BAR_GAP)
            y = cy - h / 2.0
            # trailing (most recent) bars a touch brighter for a sense of motion
            alpha = 0.55 + 0.4 * (i / max(1, n - 1))
            NSColor.colorWithCalibratedWhite_alpha_(1.0, alpha).set()
            path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(x, y, BAR_W, h), BAR_W / 2.0, BAR_W / 2.0
            )
            path.fill()

    def _draw_message(self, b):
        attrs = {
            NSFontAttributeName: NSFont.systemFontOfSize_(12.5),
            NSForegroundColorAttributeName: NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.92),
        }
        s = NSMutableAttributedString.alloc().initWithString_attributes_(self._msg, attrs)
        size = s.size()
        s.drawAtPoint_(((b.size.width - size.width) / 2.0, (b.size.height - size.height) / 2.0))


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
    at.addAttribute_value_range_(NSFontAttributeName, NSFont.boldSystemFontOfSize_(15), (0, len(title)))
    b.setAttributedTitle_(at)
    return b


def _glass_capsule(content: NSView) -> NSView:
    """Wrap `content` in the best capsule material this macOS offers.

    macOS 26 (Tahoe) ships Liquid Glass as a real API — `NSGlassEffectView`
    genuinely refracts and specularly highlights what is behind it, where
    `NSVisualEffectView` only blurs and tints. Detected at runtime rather than by
    version number, so this keeps working on older systems and in CI.

    Contrast does not come from the material. Both paths get the same explicit
    scrim behind the content, because neither material darkens itself reliably:
    Liquid Glass adopts the luminance of whatever is behind it, and the HUD
    vibrancy material washes out over a light window just as badly. Measured over
    pure white, pure black and a mid-tone desktop with `tools/pill_preview.py` —
    re-run it after touching anything here.
    """
    # Scrim: sits behind the content, inside the glass, clipped to the capsule.
    # This is what makes white content legible on any background — deterministic,
    # unlike relying on the material to darken itself.
    content.setWantsLayer_(True)
    content.layer().setCornerRadius_(H / 2)
    content.layer().setMasksToBounds_(True)
    content.layer().setBackgroundColor_(
        NSColor.colorWithCalibratedWhite_alpha_(0.0, SCRIM_ALPHA).CGColor()
    )

    glass_cls = getattr(AppKit, "NSGlassEffectView", None)
    if glass_cls is not None:
        glass = glass_cls.alloc().initWithFrame_(NSMakeRect(0, 0, W, H))
        glass.setContentView_(content)
        glass.setCornerRadius_(H / 2)  # capsule
        style_regular = getattr(AppKit, "NSGlassEffectViewStyleRegular", None)
        if style_regular is not None:
            glass.setStyle_(style_regular)
        glass.setTintColor_(NSColor.colorWithCalibratedWhite_alpha_(0.0, GLASS_TINT_ALPHA))
        return glass

    root = NSVisualEffectView.alloc().initWithFrame_(NSMakeRect(0, 0, W, H))
    root.setMaterial_(NSVisualEffectMaterialHUDWindow)
    root.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
    root.setState_(NSVisualEffectStateActive)
    root.setWantsLayer_(True)
    root.layer().setCornerRadius_(H / 2)
    root.layer().setMasksToBounds_(True)
    root.layer().setBorderWidth_(0.6)
    root.layer().setBorderColor_(NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.12).CGColor())
    root.addSubview_(content)
    return root


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
        self.panel.setHasShadow_(True)  # native depth
        self.panel.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces)
        self.panel.setBecomesKeyOnlyIfNeeded_(True)

        self._target = _PillTarget.alloc().init()
        self._target.on_cancel = on_cancel
        self._target.on_commit = on_commit

        cancel = _circle_button(
            PAD, "✕",
            NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.16),
            NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.92),
        )
        cancel.setTarget_(self._target)
        cancel.setAction_("cancelPressed:")

        commit = _circle_button(W - PAD - BTN, "✓", NSColor.whiteColor(), NSColor.blackColor())
        commit.setTarget_(self._target)
        commit.setAction_("commitPressed:")

        self.wave = _WaveformView.alloc().initWithFrame_(
            NSMakeRect(PAD + BTN, 0, W - 2 * (PAD + BTN), H)
        )

        content = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, W, H))
        content.addSubview_(cancel)
        content.addSubview_(self.wave)
        content.addSubview_(commit)
        self.panel.setContentView_(_glass_capsule(content))

    # ---- threading helper ------------------------------------------------
    @staticmethod
    def _on_main(fn) -> None:
        def block() -> None:  # pyobjc blocks MUST return None (void)
            fn()

        NSOperationQueue.mainQueue().addOperationWithBlock_(block)

    # ---- public API ------------------------------------------------------
    def show(self) -> None:
        def _do():
            self.wave.setLevels_([0.0] * N_BARS)
            self.panel.orderFrontRegardless()

        self._on_main(_do)

    def set_level(self, rms: float) -> None:
        self._on_main(lambda: self.wave.push_(rms))

    def busy(self, stage: str = "") -> None:
        """Show what the pipeline is actually doing, not just that it's alive.

        The stages have visibly different durations — transcription is ~500ms on
        the accurate tier, polish another ~450ms — so a single anonymous spinner
        left the user unable to tell a slow run from a hung one.
        """
        if stage:
            self._on_main(lambda: self.wave.showMessage_(stage))
        else:
            self._on_main(lambda: self.wave.setLevels_([0.02] * N_BARS))

    def flash(self, msg: str, hold_sec: float = 1.6) -> None:
        """Show a short message in the pill, then hide it. For visible failures
        that used to vanish into a terminal nobody was watching."""
        import threading

        def _do():
            self.wave.showMessage_(msg)
            self.panel.orderFrontRegardless()

        self._on_main(_do)
        t = threading.Timer(hold_sec, self.hide)
        t.daemon = True
        t.start()

    def hide(self) -> None:
        self._on_main(lambda: self.panel.orderOut_(None))
