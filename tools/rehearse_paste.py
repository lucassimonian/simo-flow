#!/usr/bin/env python3
"""Paste rehearsal: make the wrong-window bug reproducible on demand.

The bug this exists for only appears when focus moves *during* transcription, so
it can't be triggered deliberately in normal use and can't be caught by a unit
test — the failure lives in real macOS activation behaviour, not in our logic.

This rehearses the exact sequence the pipeline performs, with a deliberate pause
in the middle so you can click away on purpose:

  1. snapshot the focused app (what fn-down does)
  2. wait, so you can click into a different window
  3. paste a sentinel through the real paste path
  4. report where it landed and whether your clipboard survived

Usage:
    ./simo rehearse            # 5s pause, default sentinel
    ./simo rehearse 8          # 8s pause
    ./simo rehearse 8 "hello"  # custom text

Expected result: the sentinel appears in the window you were in at step 1, even
though you clicked elsewhere at step 2 — and your clipboard is unchanged.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import inject  # noqa: E402

SENTINEL = "simo-flow rehearsal ✓"


def _name(snapshot: dict | None) -> str:
    if not snapshot:
        return "unknown"
    return f"{snapshot.get('bundle_id') or '?'} (pid {snapshot.get('pid')})"


def main() -> int:
    pause = float(sys.argv[1]) if len(sys.argv) > 1 else 5.0
    text = sys.argv[2] if len(sys.argv) > 2 else SENTINEL

    if not inject._is_trusted():
        print("✗ Accessibility permission is not granted — the real pipeline would")
        print("  refuse to paste too. Grant it in System Settings > Privacy &")
        print("  Security > Accessibility, then restart Simo Flow and retry.")
        return 1

    # Seed a known string so the restore path is exercised deterministically.
    # Reading whatever happened to be there is unreliable: a clipboard mid-sync
    # over Universal Clipboard reads as None and then lands *after* the paste,
    # which looks exactly like a clobber and isn't one.
    original = inject._get_clipboard()
    clipboard_before = f"simo-flow rehearsal clipboard {int(time.time())}"
    inject._set_clipboard(clipboard_before)

    start = inject.capture_focus()
    print(f"1. focus snapshot   : {_name(start)}")
    print(f"2. click into ANOTHER window now — pasting in {pause:.0f}s…")
    for remaining in range(int(pause), 0, -1):
        print(f"   {remaining}…", end="\r", flush=True)
        time.sleep(1)
    time.sleep(pause - int(pause))

    moved_to = inject.capture_focus()
    print(f"\n3. focus at paste   : {_name(moved_to)}")
    if moved_to and start and moved_to.get("pid") == start.get("pid"):
        print("   (you didn't switch away — the bug can't show itself; try again)")

    t0 = time.time()
    delivered = inject.paste_text(text, focus=start)
    elapsed = (time.time() - t0) * 1000

    landed = inject.capture_focus()
    clipboard_after = inject._get_clipboard()
    clipboard_ok = clipboard_after == clipboard_before

    print(f"4. delivered        : {delivered}  ({elapsed:.0f}ms)")
    print(f"   landed in        : {_name(landed)}")
    print(f"   clipboard restored: {clipboard_ok}"
          f"{'' if clipboard_ok else f' (seeded {clipboard_before!r}, now {clipboard_after!r})'}")

    focus_ok = bool(delivered and landed and start and landed.get("pid") == start.get("pid"))
    if original is not None:  # hand the machine back the way we found it
        inject._set_clipboard(original)

    print()
    if focus_ok and clipboard_ok:
        print(f"✓ PASS — '{text}' should be sitting in {_name(start)}, and the clipboard was restored.")
        return 0
    if not delivered:
        print("✗ FAIL — paste was refused. The reason is printed above and in ~/.simo-flow.log.")
    elif not focus_ok:
        print(f"✗ FAIL — landed in {_name(landed)} but should have landed in {_name(start)}.")
    else:
        print("✗ FAIL — paste landed correctly but the clipboard was not restored.")
        print("  If another Apple device was syncing its clipboard (Universal Clipboard),")
        print("  re-run once it has settled — that write is genuinely not ours.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
