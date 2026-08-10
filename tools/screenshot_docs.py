#!/usr/bin/env python3
"""Regenerate the README dashboard screenshots — from placeholder data, never yours.

The screenshots in `docs/` had gone stale (no Privacy page, old pill), but
regenerating them naively meant publishing real dictated speech to a public repo.
So this seeds a throwaway database with invented content, serves the dashboard
against *that*, screenshots it, and never opens `~/.simo-flow.db` at all.

Dark mode is captured by pre-setting `data-theme` on a temp copy of the dashboard.
The page's own theme code only overrides that when the user has saved a preference,
so the attribute survives — no change to the shipped dashboard for a tool's benefit.

Usage:
    ./.venv/bin/python tools/screenshot_docs.py
    ./.venv/bin/python tools/screenshot_docs.py --out /tmp/preview   # review first
"""
import argparse
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# Invented dictations. Deliberately generic engineering chatter: plausible enough
# that the feed looks real, with nothing personal in it.
SPEECH = [
    ("um so the retry logic needs a backoff or we'll hammer the endpoint",
     "So the retry logic needs a backoff or we'll hammer the endpoint."),
    ("can you review the migration before I run it against staging",
     "Can you review the migration before I run it against staging?"),
    ("I think the cache invalidation is the actual bug here, not the query",
     "I think the cache invalidation is the actual bug here, not the query."),
    ("uh let's ship the read path first and measure before optimising the write",
     "Let's ship the read path first and measure before optimising the write."),
    ("the flaky test is a race in the fixture, not in the code under test",
     "The flaky test is a race in the fixture, not in the code under test."),
    ("remind me to add an index on that foreign key before we launch",
     "Remind me to add an index on that foreign key before we launch."),
    ("basically the p99 got worse after the change so I'm reverting it",
     "Basically the p99 got worse after the change so I'm reverting it."),
    ("draft a note to the team explaining why we're delaying the rollout",
     "Draft a note to the team explaining why we're delaying the rollout."),
    ("you know the error message doesn't say which field failed validation",
     "The error message doesn't say which field failed validation."),
    ("let's pair on the deploy tomorrow morning before standup",
     "Let's pair on the deploy tomorrow morning before standup."),
]
TERMS = ["Simonian", "pyobjc", "Ollama", "whisper.cpp", "PortAudio", "launchd"]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def seed(db_path: Path) -> None:
    """Fill a fresh database with invented history spread over ten weeks."""
    import engine.store as store

    store.DB_PATH = db_path
    now = datetime.now()
    # Weight recent days more heavily so the heatmap and the "words per day" chart
    # look like real use rather than a uniform block.
    for week in range(10):
        for day in range(7):
            when = now - timedelta(days=week * 7 + day)
            if (week + day) % 3 == 0 and week > 1:
                continue  # some days off, so the heatmap has texture
            for i in range((3 if week < 2 else 1) + (day % 2)):
                raw, polished = SPEECH[(week * 7 + day + i) % len(SPEECH)]
                # Vary the clock: every row sharing one timestamp reads as seeded
                # data the moment anyone looks at the feed.
                stamp = when.replace(
                    hour=9 + ((week * 3 + day * 2 + i * 5) % 10),
                    minute=(week * 17 + day * 11 + i * 23) % 60,
                    second=(day * 7 + i * 13) % 60,
                )
                with store._conn() as c:
                    c.execute(
                        "INSERT INTO history (ts, app_name, raw_text, polished_text,"
                        " duration_ms, word_count, audio_sec) VALUES (?,?,?,?,?,?,?)",
                        (stamp.strftime("%Y-%m-%d %H:%M:%S"), "", raw, polished,
                         430 + (i * 210), len(polished.split()),
                         round(len(polished.split()) / 2.7, 1)),
                    )
    for t in TERMS:
        store.dictionary_add(t)


def serve(static_dir: Path, port: int) -> None:
    import uvicorn

    from engine import api

    api.STATIC = static_dir
    api._ALLOWED_HOSTS.add(f"127.0.0.1:{port}")
    threading.Thread(
        target=lambda: uvicorn.run(api.app, host="127.0.0.1", port=port, log_level="error"),
        daemon=True,
    ).start()
    for _ in range(100):  # wait for the port to answer
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("dashboard did not start")


def shoot(url: str, out: Path, size: str = "1440,1000") -> None:
    subprocess.run(
        [CHROME, "--headless=new", f"--screenshot={out}", f"--window-size={size}",
         "--hide-scrollbars", "--force-device-scale-factor=2",
         "--virtual-time-budget=4000", url],
        capture_output=True, check=False,
    )
    if not out.exists():
        raise RuntimeError(f"chrome produced no screenshot for {url}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs"))
    args = ap.parse_args()
    if not Path(CHROME).exists():
        print("Google Chrome not found — needed for headless screenshots.")
        return 2

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        seed(tmpdir / "docs.db")

        # Light and dark copies of the dashboard; the dark one has the theme
        # attribute pre-set so headless Chrome doesn't have to click anything.
        src = (ROOT / "engine" / "static" / "dashboard.html").read_text()
        light_dir, dark_dir = tmpdir / "light", tmpdir / "dark"
        for d in (light_dir, dark_dir):
            d.mkdir()
        (light_dir / "dashboard.html").write_text(src)
        (dark_dir / "dashboard.html").write_text(
            src.replace('<html lang="en">', '<html lang="en" data-theme="dark">', 1)
        )

        shots = [
            (light_dir, "", "dashboard-home.png"),
            (light_dir, "#insights", "dashboard-insights.png"),
            (light_dir, "#privacy", "dashboard-privacy.png"),
            (dark_dir, "", "dashboard-home-dark.png"),
        ]
        port = free_port()
        current: Path | None = None
        for static_dir, frag, name in shots:
            if static_dir != current:
                # A new static dir needs its own server; ports are cheap.
                port = free_port()
                serve(static_dir, port)
                current = static_dir
            target = outdir / name
            shoot(f"http://127.0.0.1:{port}/{frag}", target)
            print(f"  {target.relative_to(ROOT) if outdir.is_relative_to(ROOT) else target}"
                  f"  ({target.stat().st_size // 1024}kb)")

    print("\nSeeded from invented content in tools/screenshot_docs.py —")
    print("your own dictation history was never opened.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
