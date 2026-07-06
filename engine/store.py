"""SQLite store: dictation history, custom dictionary, insights queries."""
import sqlite3
from pathlib import Path

DB_PATH = Path.home() / ".simo-flow.db"

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
"""


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
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
        if q:
            rows = c.execute(
                "SELECT * FROM history WHERE polished_text LIKE ? ORDER BY id DESC LIMIT ?",
                (f"%{q}%", limit),
            )
        else:
            rows = c.execute("SELECT * FROM history ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]


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


if __name__ == "__main__":
    log_dictation("um test raw", "Test raw.", 500, 2.0, "selfcheck")
    assert history(1)[0]["polished_text"] == "Test raw."
    dictionary_add("Simonian")
    assert any(t["term"] == "Simonian" for t in dictionary_terms())
    assert "Simonian" in dictionary_prompt()
    ins = insights()
    assert ins["total_words"] > 0 and "per_day" in ins
    print(f"store self-check OK — {ins['total_utterances']} utterances, dict={len(dictionary_terms())} terms")
