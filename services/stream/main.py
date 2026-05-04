"""
astra-stream — SSE bridge between the web UI and Astra core.

Endpoints
---------

    GET /health                     liveness probe
    POST /stream   body: {prompt}   returns text/event-stream with the
                                    full Astra reasoning trace

The web UI proxies through `/api/chat` in Next.js so the browser
never needs direct access to this service. That also means CORS is
kept tight: only localhost dev origins are allowed for direct debug.
"""

from __future__ import annotations

import asyncio
import logging
import os

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

# Load .env before anything that depends on env vars
load_dotenv()

# Make astra core importable. Setting this before importing runner so
# the lazy import in run_query() sees astra on sys.path.
#
# Astra's Settings uses env_file=".env" with a *relative* path, so we
# must also chdir into the astra directory for its config to load.
# That's fine: this service is a thin streaming shell over astra; it
# doesn't need its own working directory for anything else.
_astra_path = os.environ.get("STREAM_ASTRA_CORE_PATH")
if _astra_path:
    import sys

    if _astra_path not in sys.path:
        sys.path.insert(0, _astra_path)
    os.chdir(_astra_path)

from stream.events import error as sse_error, heartbeat  # noqa: E402
from stream.runner import run_query  # noqa: E402

_SHARED_SECRET = os.environ.get("STREAM_SHARED_SECRET", "").strip()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("astra-stream")

app = FastAPI(
    title="astra-stream",
    description="SSE bridge between the Astra Agent SDK and the web UI",
    version="0.1.0",
)

# CORS — tight: only the Next.js dev servers. In production the proxy
# handles it and this list can be empty.
_origins = [
    o.strip()
    for o in os.environ.get("STREAM_ALLOWED_ORIGINS", "").split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
    allow_credentials=False,
)


@app.get("/health")
async def health() -> dict[str, object]:
    return {
        "status": "healthy",
        "service": "astra-stream",
        "port": int(os.environ.get("STREAM_PORT", 8700)),
    }


# ── Embedding-model pre-warm ───────────────────────────────────
#
# The sentence-transformers model (all-MiniLM-L6-v2) lazy-loads on
# first call to recall_memories. On a fresh container that means the
# user's first turn pays a 30-60s cold start while ~80MB of weights
# download from HuggingFace. The browser's heartbeat shows "no
# activity for 48s" and the user (reasonably) thinks Astra is hung.
#
# Fix: kick off the model load at startup in a background thread so
# the import + download happens while the server is otherwise idle.
# By the time the first user request arrives, the model is hot.
#
# The thread is daemonized so a stuck download can't keep the process
# alive on shutdown. If the load fails, we log and let the lazy path
# retry on first use — same as before, just no longer dropped silently.

@app.on_event("startup")
async def _prewarm_embedding_model() -> None:
    import threading

    def _load():
        try:
            logger.info("[prewarm] loading sentence-transformers model in background")
            from astra.memory.embeddings import _get_model  # type: ignore[import-not-found]
            model = _get_model()
            # Run a tiny encode to warm the inference path too — the
            # first encode after load also has overhead beyond download.
            model.encode("warmup", normalize_embeddings=True)
            logger.info("[prewarm] embedding model ready")
        except Exception:
            logger.exception("[prewarm] embedding model load failed (will retry lazily)")

    t = threading.Thread(target=_load, name="embedding-prewarm", daemon=True)
    t.start()


# ── Stuck-turn sweeper at startup ──────────────────────────────
#
# Every previous deploy/restart that interrupted an in-flight turn
# left a 'running' row in the turns table. Without cleanup these rows
# accumulate forever — the user has seen turns stuck for 7+ hours.
# This sweeper runs once at startup and marks any 'running' row
# whose started_at is more than 15 minutes old as 'interrupted', so
# the audit + listings stay clean.
#
# 15 min is the watermark because the runner now has a 4-min idle
# timeout (see _SDK_IDLE_TIMEOUT_SEC in runner.py). Any 'running' row
# older than that has been orphaned by something the runner couldn't
# catch — almost always a deploy or container restart.

@app.on_event("startup")
async def _sweep_stuck_turns() -> None:
    try:
        from sqlalchemy import text  # type: ignore[import-not-found]
        from astra.db.engine import async_session  # type: ignore[import-not-found]
    except Exception:
        return
    try:
        async with async_session() as s:
            r = await s.execute(
                text(
                    """
                    UPDATE turns
                    SET status = 'interrupted',
                        ended_at = now(),
                        error_message = COALESCE(
                          error_message,
                          'swept at startup — orphaned by previous restart'
                        )
                    WHERE status = 'running'
                      AND started_at < now() - INTERVAL '15 minutes'
                    """
                )
            )
            count = r.rowcount or 0
            await s.commit()
            if count > 0:
                logger.info(
                    "[startup-sweep] marked %d stuck 'running' turns as interrupted",
                    count,
                )
    except Exception:
        # Migration might not have run yet — table missing is expected
        # the first time this code lands. Don't crash startup.
        logger.exception("[startup-sweep] failed (table may not exist yet)")


@app.get("/memory/search")
async def memory_search(
    request: Request,
    q: str,
    top_k: int = 10,
    memory_type: str | None = None,
) -> dict[str, object]:
    """Semantic memory search.

    Exposes astra.memory.retrieval.search_memories over HTTP so the web
    UI can offer a ⌘K-style semantic query UX. We keep it on this service
    (not in astra-web) because the embedding model is loaded into this
    Python process already — a Node-side round-trip would require its
    own model or a separate embedding microservice.
    """
    _check_secret(request)
    q = (q or "").strip()
    if not q:
        raise HTTPException(400, "q is required")

    # Lazy imports so we don't pay the embedding-model load at boot if
    # nobody ever searches.
    from astra.db.engine import async_session  # type: ignore[import-not-found]
    from astra.memory.models import MemoryType  # type: ignore[import-not-found]
    from astra.memory.retrieval import search_memories  # type: ignore[import-not-found]

    mt = None
    if memory_type:
        try:
            mt = MemoryType(memory_type)
        except ValueError:
            raise HTTPException(400, f"unknown memory_type: {memory_type}")

    try:
        async with async_session() as session:
            results = await search_memories(
                session,
                query=q,
                memory_type=mt,
                top_k=max(1, min(50, int(top_k))),
            )
    except Exception as e:
        logger.exception("memory search failed")
        raise HTTPException(500, f"search failed: {e}")

    return {"query": q, "count": len(results), "results": results}


class StreamRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=20_000)
    session_id: str | None = Field(
        default=None,
        description="Resume a prior SDK session for multi-turn context. "
        "Omit to start fresh.",
    )


def _check_secret(request: Request) -> None:
    """Reject any call that doesn't carry the shared secret.

    When STREAM_SHARED_SECRET is empty the check is skipped — that's
    the dev-only mode. In production (Cloudflare-tunneled), astra-web's
    /api/chat proxy adds the header on every request.
    """
    if not _SHARED_SECRET:
        return
    provided = request.headers.get("x-astra-secret", "").strip()
    if provided != _SHARED_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")


@app.post("/api/share")
async def share_receive(request: Request) -> dict[str, object]:
    """Accept a payload from the iOS Share Sheet extension.

    Auth: the extension sends `Authorization: Bearer <share_token>`;
    each paired phone has its own token (see `share_tokens`).
    """
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    token = auth.split(None, 1)[1].strip()

    from astra.shares import file_share_payload, validate_token  # type: ignore[import-not-found]

    token_id = await validate_token(token)
    if token_id is None:
        raise HTTPException(401, "invalid or revoked token")

    content_type = (request.headers.get("content-type") or "").lower()

    kind = "text"
    source_app = ""
    source_url = ""
    title = ""
    text_body = ""
    note = ""
    file_bytes: bytes | None = None
    file_ext = ""
    mime = ""
    client_ts_raw = ""

    if "multipart/form-data" in content_type:
        form = await request.form()
        source_app = str(form.get("source_app", "") or "")
        source_url = str(form.get("source_url", "") or "")
        title = str(form.get("title", "") or "")
        text_body = str(form.get("text", "") or "")
        note = str(form.get("note", "") or "")
        kind = str(form.get("kind", "") or "file") or "file"
        client_ts_raw = str(form.get("client_ts", "") or "")
        upload = form.get("file")
        if upload is not None and hasattr(upload, "read"):
            file_bytes = await upload.read()
            filename = getattr(upload, "filename", "") or ""
            file_ext = filename.rsplit(".", 1)[-1] if "." in filename else ""
            mime = getattr(upload, "content_type", "") or ""
    else:
        body = await request.json()
        source_app = str(body.get("source_app", "") or "")
        source_url = str(body.get("source_url", "") or "")
        title = str(body.get("title", "") or "")
        text_body = str(body.get("text", "") or "")
        note = str(body.get("note", "") or "")
        kind = str(body.get("kind", "") or ("url" if source_url else "text"))
        client_ts_raw = str(body.get("client_ts", "") or "")

    # Parse the optional client timestamp. Bad values are silently
    # ignored — the row still lands, just without the device moment.
    client_ts = None
    if client_ts_raw:
        try:
            from datetime import datetime  # local — light import
            client_ts = datetime.fromisoformat(client_ts_raw.replace("Z", "+00:00"))
        except Exception:
            client_ts = None

    result = await file_share_payload(
        token_id=token_id,
        kind=kind,
        source_app=source_app,
        source_url=source_url,
        title=title,
        text=text_body,
        note=note,
        file_bytes=file_bytes,
        file_ext=file_ext,
        mime_type=mime,
        client_ts=client_ts,
    )
    logger.info(
        "[share] received id=%s kind=%s from=%r",
        result["id"], kind, source_app,
    )
    return {"ok": True, "id": result["id"]}


@app.post("/api/push/test")
async def push_test(request: Request) -> dict[str, object]:
    """Send a test notification to every active subscription.

    Called by astra-web's /api/push/test route. astra-web holds the
    auth session; this endpoint is protected by the shared secret so
    random internet traffic can't spam notifications.
    """
    _check_secret(request)

    # Lazy import so the sender's pywebpush load happens only when
    # someone actually tries to send.
    from astra.push import broadcast  # type: ignore[import-not-found]

    r = await broadcast(
        title="astra · test",
        body="web push is live",
        url="/",
        tag="astra-test",
    )
    return {
        "attempted": r.attempted,
        "delivered": r.delivered,
        "pruned": r.pruned,
        "failed": r.failed,
    }


@app.post("/stream")
async def stream(req: StreamRequest, request: Request) -> StreamingResponse:
    """Run an Astra query and stream the result as SSE."""
    _check_secret(request)
    if not req.prompt.strip():
        raise HTTPException(status_code=400, detail="prompt is empty")

    async def generate():
        # Kick off the runner and interleave heartbeats so proxies
        # don't drop slow streams.
        runner_iter = run_query(
            req.prompt, resume_session_id=req.session_id
        ).__aiter__()
        last_sent = asyncio.get_event_loop().time()

        while True:
            try:
                # Race the next event against a 15s heartbeat timer.
                frame = await asyncio.wait_for(runner_iter.__anext__(), timeout=15)
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                # Still computing — keep the connection warm.
                yield heartbeat()
                last_sent = asyncio.get_event_loop().time()
                continue
            except Exception as e:
                logger.exception("stream runner raised")
                yield sse_error(f"runner crashed: {e}")
                return
            else:
                yield frame
                last_sent = asyncio.get_event_loop().time()

        # Suppress unused-variable warnings; keeping the timestamp in
        # case we add duration logging later.
        _ = last_sent

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # disables nginx buffering if any
        },
    )


def main() -> None:
    """Entry point for `python -m stream.main`."""
    import uvicorn

    host = os.environ.get("STREAM_HOST", "0.0.0.0")
    port = int(os.environ.get("STREAM_PORT", 8700))
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
