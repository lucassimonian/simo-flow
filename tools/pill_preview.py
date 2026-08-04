#!/usr/bin/env python3
"""Render the recording pill over controlled backgrounds and screenshot it.

The pill floats over whatever the user happens to be looking at, so "does it
look good" is not answerable against a single screenshot of one wallpaper. The
white and black cases are the real tests: the waveform and glyphs are white, so
a light substrate is where legibility fails, and that failure is invisible if
you only ever check it against a mid-tone desktop.

Renders each background as an opaque panel directly behind the pill, so results
are deterministic and comparable run to run — unlike capturing over whatever
window is frontmost.

Usage:
    ./.venv/bin/python tools/pill_preview.py                 # white + black + mid
    ./.venv/bin/python tools/pill_preview.py --tint 0.4      # try another tint
    ./.venv/bin/python tools/pill_preview.py --stage Polishing
    ./.venv/bin/python tools/pill_preview.py --out docs      # write to docs/
"""
import argparse
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import AppKit  # noqa: E402
from AppKit import (  # noqa: E402
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSBackingStoreBuffered,
    NSColor,
    NSPanel,
    NSScreen,
    NSView,
    NSWindowStyleMaskBorderless,
)
from engine import overlay  # noqa: E402

# A realistic sweep of speech levels — not a flat line, so bar height and the
# brightness ramp across the trailing bars are both visible.
LEVELS = [0.03, 0.06, 0.10, 0.12, 0.08, 0.11, 0.05, 0.09,
          0.12, 0.07, 0.04, 0.10, 0.12, 0.06, 0.09, 0.03, 0.08, 0.11]

BACKDROPS = {
    "white": 1.0,  # worst case for white content
    "mid": 0.5,
    "black": 0.0,  # worst case for the dark tint
}


def _backdrop(level: float) -> NSPanel:
    """An opaque panel filling the screen, one level below the pill.

    Must be built on the main thread — AppKit window creation off-main hangs
    rather than raising, which is a slow way to learn the rule. All three are
    created up front and then only ordered in and out from the worker.
    """
    frame = NSScreen.mainScreen().frame()
    panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        frame, NSWindowStyleMaskBorderless, NSBackingStoreBuffered, False
    )
    panel.setBackgroundColor_(NSColor.colorWithCalibratedWhite_alpha_(level, 1.0))
    panel.setLevel_(overlay.NSStatusWindowLevel - 1)
    panel.setOpaque_(True)
    # A bare NSPanel with no content view draws nothing, so backgroundColor alone
    # leaves the desktop showing through and the test silently measures the
    # wallpaper instead of the intended backdrop.
    fill = NSView.alloc().initWithFrame_(frame)
    fill.setWantsLayer_(True)
    fill.layer().setBackgroundColor_(
        NSColor.colorWithCalibratedWhite_alpha_(level, 1.0).CGColor()
    )
    panel.setContentView_(fill)
    return panel


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tint", type=float, default=None, help="override GLASS_TINT_ALPHA")
    ap.add_argument("--stage", default=None, help="render a stage label instead of bars")
    ap.add_argument("--out", default="/tmp", help="output directory")
    args = ap.parse_args()

    if args.tint is not None:
        overlay.GLASS_TINT_ALPHA = args.tint
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    glass = getattr(AppKit, "NSGlassEffectView", None) is not None
    kind = "glass" if glass else "vibrancy"

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    pill = overlay.RecordingPill()
    pill.show()
    backdrops = {name: _backdrop(level) for name, level in BACKDROPS.items()}

    screen = NSScreen.mainScreen().frame().size
    W, H, BOTTOM = overlay.W, overlay.H, overlay.BOTTOM
    pad = 44
    region = (f"{int((screen.width - W) / 2) - pad},"
              f"{int(screen.height - BOTTOM - H) - pad},{W + 2 * pad},{H + 2 * pad}")
    written: list[Path] = []

    def run() -> None:
        time.sleep(1.0)  # let show()'s queued reset land before we set content
        for name, back in backdrops.items():
            # Wrapped in a lambda: a bound ObjC method passed straight to
            # addOperationWithBlock_ is silently dropped, so the backdrop never
            # appeared and the test measured the wallpaper instead.
            pill._on_main(lambda b=back: b.orderFrontRegardless())
            if args.stage:
                pill._on_main(lambda: pill.wave.showMessage_(args.stage))
            else:
                pill._on_main(lambda: pill.wave.setLevels_(LEVELS))
            pill._on_main(lambda: pill.panel.orderFrontRegardless())
            time.sleep(0.8)
            suffix = f"-{args.stage.lower()}" if args.stage else ""
            path = outdir / f"pill-{kind}{suffix}-{name}.png"
            subprocess.run(["screencapture", "-x", "-R" + region, str(path)], capture_output=True)
            written.append(path)
            pill._on_main(lambda b=back: b.orderOut_(None))
            time.sleep(0.2)
        sys.stderr.write(
            f"{kind} pill, tint={overlay.GLASS_TINT_ALPHA}\n"
            + "".join(f"  {p}\n" for p in written)
        )
        sys.stderr.flush()
        os._exit(0)

    threading.Thread(target=run, daemon=True).start()
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
