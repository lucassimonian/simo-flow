"""Local dashboard API — binds 127.0.0.1 only, zero external calls."""
import threading
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel

from engine import store

PORT = 7331
STATIC = Path(__file__).parent / "static"

app = FastAPI(title="Simo Flow", docs_url=None, redoc_url=None)


@app.get("/")
def index():
    return FileResponse(STATIC / "dashboard.html")


@app.get("/api/history")
def api_history(q: str = "", limit: int = 100):
    return store.history(limit=min(limit, 500), q=q)


@app.get("/api/insights")
def api_insights():
    return store.insights()


@app.get("/api/dictionary")
def api_dictionary():
    return store.dictionary_terms()


class Term(BaseModel):
    term: str


@app.post("/api/dictionary")
def api_dictionary_add(t: Term):
    store.dictionary_add(t.term)
    return store.dictionary_terms()


@app.delete("/api/dictionary/{term_id}")
def api_dictionary_delete(term_id: int):
    store.dictionary_delete(term_id)
    return store.dictionary_terms()


def start_in_background() -> None:
    threading.Thread(
        target=lambda: uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning"),
        daemon=True,
    ).start()
