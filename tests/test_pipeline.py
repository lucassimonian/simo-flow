"""End-to-end headless check: wav -> stt -> polish -> paste into a real app.

Opens TextEdit, pastes the pipeline output at its cursor, reads the document
back via AppleScript, and asserts the text landed and the clipboard survived.
Run:  .venv/bin/python tests/test_pipeline.py
"""
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import inject, polish, stt  # noqa: E402

WAV = Path(__file__).parent / "utterance.wav"


def osa(script: str) -> str:
    return subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True, check=True
    ).stdout.strip()


def main() -> None:
    # 1. transcribe + polish
    stt.start_server()
    t0 = time.time()
    raw = stt.transcribe_file(WAV)
    cleaned = polish.polish(raw)
    pipeline_ms = (time.time() - t0) * 1000
    print(f"raw:     {raw!r}")
    print(f"cleaned: {cleaned!r}")
    print(f"stt+polish: {pipeline_ms:.0f}ms")
    assert "quarterly report" in cleaned.lower(), "content lost in pipeline"

    # 2. paste into a real app and read it back
    sentinel = "clipboard-sentinel-simo"
    inject._set_clipboard(sentinel)

    osa('tell application "TextEdit" to activate')
    time.sleep(1.5)  # cold launch can be slow
    osa('tell application "TextEdit" to make new document')
    time.sleep(1.0)  # let the window take focus

    inject.paste_text(cleaned)
    time.sleep(0.6)

    doc_text = osa('tell application "TextEdit" to get text of document 1')
    osa('tell application "TextEdit" to close document 1 saving no')

    assert cleaned.split()[0] in doc_text and "quarterly report" in doc_text.lower(), (
        f"paste did not land: doc={doc_text!r}"
    )
    assert inject._get_clipboard() == sentinel, "clipboard was not restored"
    print("PIPELINE E2E OK — pasted into TextEdit, clipboard restored")


if __name__ == "__main__":
    main()
