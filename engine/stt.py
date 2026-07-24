"""STT: manage a persistent whisper-server (Metal, model resident) and transcribe wavs."""
import io
import os
import shutil
import signal
import subprocess
import time
import wave
from pathlib import Path

import numpy as np
import requests

PORT = 7332
URL = f"http://127.0.0.1:{PORT}/inference"
_MODELS_DIR = Path(__file__).resolve().parent.parent / "models"

# Two tiers. "accurate" (large-v3-turbo) is the default: it faithfully
# transcribes what was said — including disfluencies the LLM polish pass then
# removes. "fast" (base.en) is ~7x quicker but silently drops words, so it's
# only for latency-critical use. Measured warm: base.en ~85ms, turbo ~520ms.
MODELS = {
    "accurate": _MODELS_DIR / "ggml-large-v3-turbo-q5_0.bin",
    "fast": _MODELS_DIR / "ggml-base.en.bin",
}
DEFAULT_TIER = "accurate"
MODEL_PATH = MODELS[DEFAULT_TIER]  # back-compat for the self-check

_current_tier = DEFAULT_TIER

# launchd (login start) does NOT inherit the shell PATH, so a bare
# "whisper-server" isn't found. Resolve the absolute path, checking PATH first
# then the usual Homebrew locations, so it works from any launch context.
def _whisper_bin() -> str:
    found = shutil.which("whisper-server")
    if found:
        return found
    for p in ("/opt/homebrew/bin/whisper-server", "/usr/local/bin/whisper-server"):
        if Path(p).exists():
            return p
    raise FileNotFoundError(
        "whisper-server not found — install with: brew install whisper-cpp"
    )


_server: subprocess.Popen | None = None


def _kill_port(port: int) -> None:
    """Kill whatever process is listening on `port`. Used to reap an orphaned
    whisper-server left by a previous instance that was SIGTERM'd (launchctl
    stop/restart, logout) before it could clean up — otherwise the port stays
    held and our fresh server can't bind it."""
    try:
        out = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}"], capture_output=True, text=True, timeout=3
        ).stdout
    except Exception:
        return
    for pid in out.split():
        try:
            os.kill(int(pid), signal.SIGKILL)
        except (ProcessLookupError, ValueError, PermissionError):
            pass


def start_server(tier: str = DEFAULT_TIER) -> None:
    """Start whisper-server for the given model tier; blocks until it answers.

    If a server for a *different* tier is already up, it is replaced. An orphan
    server from a previous process (ours died without cleanup) is reaped first.
    """
    global _server, _current_tier
    model_path = MODELS.get(tier, MODELS[DEFAULT_TIER])
    if not model_path.exists():  # missing turbo download → fall back to base.en
        print(f"[simo] model for '{tier}' missing at {model_path}; using fast tier", flush=True)
        tier, model_path = "fast", MODELS["fast"]
    if is_up() and tier == _current_tier and _server is not None:
        return  # our server, right tier — reuse it
    stop_server()
    if is_up():
        # a server is still answering but it isn't ours (orphan from a prior
        # process, or the wrong tier we couldn't stop) — reap it and wait for
        # the port to free
        _kill_port(PORT)
        deadline = time.time() + 3
        while time.time() < deadline and is_up():
            time.sleep(0.1)
    _current_tier = tier
    _server = subprocess.Popen(
        [_whisper_bin(), "-m", str(model_path), "--port", str(PORT), "--host", "127.0.0.1"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 30
    while time.time() < deadline:
        if is_up():
            return
        time.sleep(0.2)
    raise RuntimeError("whisper-server failed to start within 30s")


def set_tier(tier: str) -> None:
    """Switch the active model tier, restarting the server. Returns when ready."""
    start_server(tier)


def current_tier() -> str:
    return _current_tier


def is_up() -> bool:
    try:
        requests.get(f"http://127.0.0.1:{PORT}/", timeout=0.5)
        return True
    except requests.RequestException:
        return False


def stop_server() -> None:
    """Terminate the server and wait for it to release the port, so an
    immediate restart (tier swap) doesn't race the old process off :7332."""
    global _server
    if _server:
        _server.terminate()
        try:
            _server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _server.kill()
        _server = None
    # wait for the port to actually close (terminate() returns before the OS
    # tears down the listening socket)
    deadline = time.time() + 3
    while time.time() < deadline and is_up():
        time.sleep(0.1)


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
    """Transcribe float32 mono samples via the resident whisper-server.

    Restarts the server if it has died. Before this, one crashed subprocess
    meant every subsequent dictation silently pasted nothing until relaunch.
    """
    if not is_up():
        print("[simo] whisper-server not responding — restarting", flush=True)
        start_server(_current_tier)  # keep the user's tier, don't silently reset to default
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
