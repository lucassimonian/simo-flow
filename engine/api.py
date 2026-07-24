"""Local dashboard API — binds 127.0.0.1 only, zero external calls."""
import threading
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from engine import store

PORT = 7331
STATIC = Path(__file__).parent / "static"

app = FastAPI(title="Simo Flow", docs_url=None, redoc_url=None)

# Allowed Host header values. Anything else means the request reached us via a
# hostname that isn't ours — the classic DNS-rebinding vector, where a malicious
# page rebinds its own domain to 127.0.0.1 and becomes same-origin, then reads
# /api/history. For an app whose whole promise is "your words never leave the
# machine", that read path must be closed, not just the writes.
_ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}


@app.middleware("http")
async def _host_guard(request: Request, call_next):
    host = request.headers.get("host", "")
    if host not in _ALLOWED_HOSTS:
        return JSONResponse({"detail": "bad host"}, status_code=403)
    return await call_next(request)

# The dashboard has no auth (localhost-only, by design), but a state-changing
# request can still be forged cross-origin by any website the user visits while
# the app runs (browsers send "simple" POSTs with no preflight). Reject writes
# whose Origin isn't our own localhost. Same-origin fetches from the dashboard
# send a matching Origin (or none, for direct navigation), so this is invisible
# to legitimate use.
_ALLOWED_ORIGINS = {
    f"http://127.0.0.1:{PORT}",
    f"http://localhost:{PORT}",
}


def _reject_cross_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin is not None and origin not in _ALLOWED_ORIGINS:
        from fastapi import HTTPException

        raise HTTPException(status_code=403, detail="cross-origin request refused")


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
def api_dictionary_add(t: Term, request: Request):
    _reject_cross_origin(request)
    store.dictionary_add(t.term)
    return store.dictionary_terms()


@app.delete("/api/dictionary/{term_id}")
def api_dictionary_delete(term_id: int, request: Request):
    _reject_cross_origin(request)
    store.dictionary_delete(term_id)
    return store.dictionary_terms()


def start_in_background() -> None:
    threading.Thread(
        target=lambda: uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning"),
        daemon=True,
    ).start()
