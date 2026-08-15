"""SQLite store: dictation history, custom dictionary, insights queries."""
import os
import sqlite3
from pathlib import Path

DB_PATH = Path.home() / ".simo-flow.db"

# Existing DBs were created world-readable (umask 022). Tighten on import so an
# upgrade also fixes the historical file, not just fresh installs.
if DB_PATH.exists():
    try:
        os.chmod(DB_PATH, 0o600)
    except OSError:
        pass

_SCHEMA = """
CREATE TABLE IF NOT EXISTS history (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  app_name TEXT,
  raw_text TEXT NOT NULL,
  polished_text TEXT NOT NULL,
  duration_ms INTEGER,
  word_count INTEGER
);
CREATE TABLE IF NOT EXISTS dictionary (
  id INTEGER PRIMARY KEY,
  term TEXT NOT NULL UNIQUE,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def _conn() -> sqlite3.Connection:
    existed = DB_PATH.exists()
    c = sqlite3.connect(DB_PATH)
    if not existed:
        # DB holds full plaintext of everything dictated — keep it owner-only,
        # not world-readable, on shared/managed machines.
        os.chmod(DB_PATH, 0o600)
    c.executescript(_SCHEMA)
    # migration: audio_sec for real WPM (duration_ms is pipeline latency, not speech time)
    cols = [r[1] for r in c.execute("PRAGMA table_info(history)")]
    if "audio_sec" not in cols:
        c.execute("ALTER TABLE history ADD COLUMN audio_sec REAL DEFAULT 0")
    return c


def log_dictation(raw: str, polished: str, duration_ms: int, audio_sec: float = 0.0, app_name: str = "") -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO history (ts, app_name, raw_text, polished_text, duration_ms, word_count, audio_sec)"
            " VALUES (datetime('now','localtime'), ?, ?, ?, ?, ?, ?)",
            (app_name, raw, polished, duration_ms, len(polished.split()), audio_sec),
        )


def history(limit: int = 100, q: str = "") -> list[dict]:
    with _conn() as c:
        c.row_factory = sqlite3.Row
        # id DESC breaks ties: two dictations in the same second must still order
        # deterministically newest-first, or the feed order flickers
        if q:
            rows = c.execute(
                "SELECT * FROM history WHERE polished_text LIKE ? ORDER BY ts DESC, id DESC LIMIT ?",
                (f"%{q}%", limit),
            )
        else:
            rows = c.execute("SELECT * FROM history ORDER BY ts DESC, id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]


def history_all() -> list[dict]:
    """Every row, oldest first — for export. Unlike history() this is deliberately
    unbounded: an export that silently truncated would be worse than slow."""
    with _conn() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute("SELECT * FROM history ORDER BY ts ASC, id ASC")
        return [dict(r) for r in rows]


def clear_history() -> int:
    """Delete all dictation history. Returns the number of rows removed.
    (The dictionary and settings are left intact.)"""
    with _conn() as c:
        n = c.execute("SELECT COUNT(*) FROM history").fetchone()[0]
        c.execute("DELETE FROM history")
        return n


def history_needing_flatten() -> int:
    """How many stored dictations would actually change if tidied.

    Counted with the same function that does the tidying rather than a SQL
    approximation of it. The approximation looked only for newlines while the
    tidy collapses every run of whitespace, so the dashboard could offer to fix
    one number and then report having fixed a different one.
    """
    from engine.stt import flatten_whitespace

    with _conn() as c:
        c.row_factory = sqlite3.Row
        return sum(
            1
            for row in c.execute("SELECT raw_text, polished_text FROM history")
            if flatten_whitespace(row["raw_text"]) != row["raw_text"]
            or flatten_whitespace(row["polished_text"]) != row["polished_text"]
        )


def flatten_history() -> int:
    """Normalise whitespace in stored dictations. Returns rows changed.

    v2.1.1 flattens whisper's fixed-width line wrapping going forward, but rows
    written before it still contain hard breaks mid-sentence — they render broken
    in the feed and in any export.

    Deliberately reuses `stt.flatten_whitespace`, the same function every new
    dictation goes through, rather than a migration-specific rule. A second
    implementation could drift from the first and would silently leave history
    formatted differently from new entries, which is the whole complaint.

    Backs the database up first. This rewrites text the user spoke; the change is
    whitespace-only and reversible in principle, but "in principle" is not a
    restore path.
    """
    from engine.stt import flatten_whitespace

    # Work out the changes before touching anything, so a run with nothing to do
    # is genuinely free — no backup, no writes. Clicking the button twice must not
    # leave a second copy of your entire dictation history in your home folder.
    # ponytail: reads every row into memory. Fine for a personal history; revisit
    # if anyone ever has enough dictations for that to matter.
    pending: list[tuple[str, str, int]] = []
    with _conn() as c:
        c.row_factory = sqlite3.Row
        for row in c.execute("SELECT id, raw_text, polished_text FROM history"):
            raw = flatten_whitespace(row["raw_text"])
            polished = flatten_whitespace(row["polished_text"])
            if raw != row["raw_text"] or polished != row["polished_text"]:
                pending.append((raw, polished, row["id"]))
    if not pending:
        return 0

    _backup_db()
    with _conn() as c:
        c.executemany(
            "UPDATE history SET raw_text = ?, polished_text = ? WHERE id = ?", pending
        )
    return len(pending)


def _backup_db() -> Path | None:
    """Copy the database next to itself before a destructive migration.

    Uses SQLite's own backup API rather than copying the file. Two threads write
    this database — the dictation pipeline and the dashboard's API server — each
    on its own connection, and a filesystem copy taken while one of them is
    mid-transaction can produce a torn, unopenable file. A backup that might not
    restore is not a restore path, which is the only reason this function exists.

    The destination is created owner-only *before* any data reaches it. Letting
    SQLite create it would apply the process umask, leaving a full plaintext dump
    of every dictation ever recorded world-readable for the length of the copy —
    a window, not a state, and easy to miss precisely because the end result looks
    correct.
    """
    if not DB_PATH.exists():
        return None
    from datetime import datetime

    dest = DB_PATH.with_suffix(f".db.bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    os.close(os.open(dest, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600))
    source = sqlite3.connect(DB_PATH)
    try:
        target = sqlite3.connect(dest)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    print(f"[simo] history backed up to {dest}", flush=True)
    return dest


def insights() -> dict:
    with _conn() as c:
        total_words, total_utt = c.execute(
            "SELECT COALESCE(SUM(word_count),0), COUNT(*) FROM history"
        ).fetchone()
        # wpm over utterances with real audio time
        row = c.execute(
            "SELECT SUM(word_count), SUM(audio_sec) FROM history WHERE audio_sec > 0.5"
        ).fetchone()
        wpm = round(row[0] / (row[1] / 60)) if row and row[1] else 0
        fixes = c.execute(
            "SELECT COUNT(*) FROM history WHERE raw_text != polished_text"
        ).fetchone()[0]
        per_day = c.execute(
            "SELECT date(ts) d, SUM(word_count) FROM history GROUP BY d ORDER BY d DESC LIMIT 105"
        ).fetchall()
        avg_latency = c.execute(
            "SELECT COALESCE(AVG(duration_ms),0) FROM history WHERE duration_ms > 0"
        ).fetchone()[0]
        return {
            "total_words": total_words,
            "total_utterances": total_utt,
            "wpm": wpm,
            "fixes": fixes,
            "avg_latency_ms": round(avg_latency),
            "per_day": [{"date": d, "words": w} for d, w in per_day],
        }


# ---- dictionary -----------------------------------------------------------
def dictionary_terms() -> list[dict]:
    with _conn() as c:
        return [
            {"id": i, "term": t}
            for i, t in c.execute("SELECT id, term FROM dictionary ORDER BY term")
        ]


def dictionary_add(term: str) -> None:
    term = term.strip()
    if term:
        with _conn() as c:
            c.execute("INSERT OR IGNORE INTO dictionary (term) VALUES (?)", (term,))


def dictionary_delete(term_id: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM dictionary WHERE id = ?", (term_id,))


def dictionary_prompt() -> str:
    """Terms joined for whisper's initial_prompt — biases ASR toward your vocab."""
    terms = [d["term"] for d in dictionary_terms()]
    return ("Vocabulary: " + ", ".join(terms) + ".") if terms else ""


# ---- settings (key/value) -------------------------------------------------
def get_setting(key: str, default: str = "") -> str:
    with _conn() as c:
        row = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default


def set_setting(key: str, value: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


if __name__ == "__main__":
    log_dictation("um test raw", "Test raw.", 500, 2.0, "selfcheck")
    assert history(1)[0]["polished_text"] == "Test raw."
    dictionary_add("Simonian")
    assert any(t["term"] == "Simonian" for t in dictionary_terms())
    assert "Simonian" in dictionary_prompt()
    ins = insights()
    assert ins["total_words"] > 0 and "per_day" in ins
    print(f"store self-check OK — {ins['total_utterances']} utterances, dict={len(dictionary_terms())} terms")
