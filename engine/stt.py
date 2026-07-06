"""STT: manage a persistent whisper-server (Metal, model resident) and transcribe wavs."""
import io
import subprocess
import time
import wave
from pathlib import Path

import numpy as np
import requests

PORT = 7332
URL = f"http://127.0.0.1:{PORT}/inference"
MODEL_PATH = Path(__file__).resolve().parent.parent / "models" / "ggml-base.en.bin"

_server: subprocess.Popen | None = None


def start_server(model_path: Path = MODEL_PATH) -> None:
    """Start whisper-server if not already running; blocks until it answers."""
    global _server
    if is_up():
        return
    _server = subprocess.Popen(
        ["whisper-server", "-m", str(model_path), "--port", str(PORT), "--host", "127.0.0.1"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 30
    while time.time() < deadline:
        if is_up():
            return
        time.sleep(0.2)
    raise RuntimeError("whisper-server failed to start within 30s")


def is_up() -> bool:
    try:
        requests.get(f"http://127.0.0.1:{PORT}/", timeout=0.5)
        return True
    except requests.RequestException:
        return False


def stop_server() -> None:
    global _server
    if _server:
        _server.terminate()
        _server = None


def _to_wav_bytes(samples: np.ndarray, rate: int = 16000) -> bytes:
    """float32 [-1,1] mono -> 16-bit PCM wav bytes."""
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def transcribe(samples: np.ndarray, initial_prompt: str = "", rate: int = 16000) -> str:
    """Transcribe float32 mono samples via the resident whisper-server."""
    data = {"response_format": "json", "temperature": "0.0"}
    if initial_prompt:
        data["prompt"] = initial_prompt
    r = requests.post(
        URL,
        files={"file": ("u.wav", _to_wav_bytes(samples, rate), "audio/wav")},
        data=data,
        timeout=60,
    )
    r.raise_for_status()
    return dedupe_sentences(r.json().get("text", "").strip())


def dedupe_sentences(text: str) -> str:
    """Collapse consecutive exact-duplicate sentences (rare whisper repetition
    artifact on noisy tails). ponytail: exact match only — fuzzy matching
    fragments on abbreviations like 'p.m.' and risks eating real speech."""
    import re

    parts = re.split(r"(?<=[.!?])\s+", text)
    out: list[str] = []
    for p in parts:
        if out and p.strip().lower() == out[-1].strip().lower():
            continue
        out.append(p)
    return " ".join(out)


def transcribe_file(path: str | Path, initial_prompt: str = "") -> str:
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1, "need 16kHz mono wav"
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return transcribe(pcm.astype(np.float32) / 32768.0, initial_prompt)


if __name__ == "__main__":
    import sys

    wav = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).parent.parent / "tests" / "utterance.wav")
    start_server()
    t0 = time.time()
    text = transcribe_file(wav)
    dt = (time.time() - t0) * 1000
    print(f"TEXT:    {text}")
    print(f"latency: {dt:.0f}ms (server warm, model resident)")
    assert "quarterly report" in text.lower(), "transcription content check failed"
    print("stt self-check OK")
