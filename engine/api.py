"""Local dashboard API — binds 127.0.0.1 only, zero external calls."""
import threading
from pathlib import Path

import uvicorn
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
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
# the app runs (browsers send "simple" POSTs with no preflight). Reject anything
# whose Origin — or, for a same-origin GET, whose Referer — isn't our own
# localhost. Note fetch() omits Origin on same-origin GETs, which is exactly why
# _reject_cross_origin needs the Referer fallback: the dashboard's own export
# request arrives with Referer and no Origin.
_ALLOWED_ORIGINS = {
    f"http://127.0.0.1:{PORT}",
    f"http://localhost:{PORT}",
}


def _reject_cross_origin(request: Request) -> None:
    """Refuse a request that did not come from our own dashboard.

    Fails **closed**. The earlier version only rejected an Origin that was
    present and unlisted, so a client omitting the header entirely inherited full
    access — and for these endpoints this check is the only protection there is
    (no auth, no CSRF token). A missing Origin therefore falls back to Referer,
    and a request with neither is refused.

    This stays invisible to real use: fetch() sends Origin on every request whose
    method isn't GET/HEAD, including same-origin ones, so the dashboard's own
    writes always carry it. An opaque origin (sandboxed iframe, file:// page)
    sends the literal string "null", which is present and correctly unlisted.
    """
    origin = request.headers.get("origin")
    if origin is not None:
        if origin not in _ALLOWED_ORIGINS:
            raise HTTPException(status_code=403, detail="cross-origin request refused")
        return
    referer = request.headers.get("referer")
    if referer is not None:
        parts = urlsplit(referer)
        if f"{parts.scheme}://{parts.netloc}" not in _ALLOWED_ORIGINS:
            raise HTTPException(status_code=403, detail="cross-origin request refused")
        return
    raise HTTPException(
        status_code=403, detail="refused: request carried neither Origin nor Referer"
    )


@app.get("/")
def index():
    # no-store so an updated dashboard is never masked by a stale browser cache
    return FileResponse(STATIC / "dashboard.html", headers={"Cache-Control": "no-store"})


@app.get("/api/history")
def api_history(q: str = "", limit: int = 100):
    return store.history(limit=min(limit, 500), q=q)


@app.get("/api/history/export")
def api_history_export(request: Request):
    """Everything, including the raw transcripts — your data, takeable elsewhere.
    Paired with DELETE below: a store of everything you have ever said needs both
    an exit and an off switch, or the privacy promise is only a claim.

    Origin-guarded despite being a read: the response is unreadable cross-origin
    (no CORS headers are ever sent), but an unbounded full-table scan of every
    transcript ever recorded is worth denying as a *trigger* too — fired in a loop
    from a background tab it competes with the dictation pipeline for the GIL.
    Nothing legitimate calls this cross-origin."""
    _reject_cross_origin(request)
    return store.history_all()


@app.delete("/api/history")
def api_history_clear(request: Request):
    _reject_cross_origin(request)
    return {"deleted": store.clear_history()}


@app.get("/api/history/needs-flatten")
def api_history_needs_flatten():
    """How many stored dictations still carry the speech engine's line wrapping.
    A count of rows, revealing none of their content, so it needs no origin guard."""
    return {"count": store.history_needing_flatten()}


@app.post("/api/history/flatten")
def api_history_flatten(request: Request):
    _reject_cross_origin(request)
    return {"updated": store.flatten_history()}


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


@app.get("/api/snippets")
def api_snippets():
    return store.snippets()


class Snippet(BaseModel):
    phrase: str
    expansion: str


@app.post("/api/snippets")
def api_snippet_add(s: Snippet, request: Request):
    _reject_cross_origin(request)
    if not store.snippet_add(s.phrase, s.expansion):
        raise HTTPException(
            status_code=400,
            detail="a phrase must be words you can say — letters, numbers, spaces, "
            "apostrophes or hyphens — and the replacement cannot be empty",
        )
    return store.snippets()


@app.delete("/api/snippets/{snippet_id}")
def api_snippet_delete(snippet_id: int, request: Request):
    _reject_cross_origin(request)
    store.snippet_delete(snippet_id)
    return store.snippets()


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
