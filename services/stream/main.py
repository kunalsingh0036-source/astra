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
from fastapi import Response
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

from stream.events import (  # noqa: E402
    error as sse_error,
    done as done_event,
    heartbeat,
)

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


@app.get("/health/deep")
async def health_deep() -> dict[str, object]:
    """Deep system health for the stream service. Probed by
    astra-web's /api/health/deep (which doesn't have visibility
    into Railway-side env vars or the migrations/ directory).

    Each check is non-sensitive — keys are reported as set/missing,
    not their values. The migration head is whatever's on disk
    (the LATEST file in astra/db/migrations/versions/) — this is
    the source of truth for "what should the DB be at" since the
    stream container has the migration files copied in.
    """
    import glob

    checks: list[dict[str, object]] = []

    # ── Anthropic key ──
    key_set = bool((os.environ.get("ANTHROPIC_API_KEY") or "").strip())
    checks.append({
        "name": "anthropic_key",
        "status": "ok" if key_set else "down",
        "detail": "env var set" if key_set else "ANTHROPIC_API_KEY missing",
    })

    # ── Database URL ──
    db_url_set = bool((os.environ.get("DATABASE_URL") or "").strip())
    checks.append({
        "name": "database_url",
        "status": "ok" if db_url_set else "down",
        "detail": "set" if db_url_set else "DATABASE_URL missing",
    })

    # ── Latest migration on disk vs head in DB ──
    # The stream container has migration files at this path. We
    # compare what's on disk (the truth) against what the DB
    # reports as its current head.
    try:
        files = sorted(
            glob.glob("/app/astra/db/migrations/versions/*.py"),
            reverse=False,
        )
        # Filenames look like: <revision>_<slug>.py
        # Latest = highest sort order (revision strings are
        # alphabetic in alembic's auto-generated form). Take the
        # last one.
        on_disk = ""
        if files:
            for f in reversed(files):
                base = os.path.basename(f)
                if base.startswith("__"):
                    continue
                # revision is everything before the first underscore
                on_disk = base.split("_", 1)[0]
                if on_disk:
                    break

        # DB head
        db_head = ""
        try:
            from sqlalchemy import text  # type: ignore
            from astra.db.engine import async_session  # type: ignore
            async with async_session() as s:
                r = await s.execute(text("SELECT version_num FROM alembic_version"))
                row = r.first()
                if row:
                    db_head = str(row[0])
        except Exception:
            pass

        if not on_disk or not db_head:
            checks.append({
                "name": "migration_head",
                "status": "degraded",
                "detail": (
                    f"could not resolve heads (disk={on_disk or 'unknown'}, "
                    f"db={db_head or 'unknown'})"
                ),
            })
        elif on_disk == db_head:
            checks.append({
                "name": "migration_head",
                "status": "ok",
                "detail": db_head,
            })
        else:
            checks.append({
                "name": "migration_head",
                "status": "degraded",
                "detail": (
                    f"db={db_head} disk={on_disk} — run alembic upgrade head"
                ),
            })
    except Exception as e:
        checks.append({
            "name": "migration_head",
            "status": "degraded",
            "detail": f"check failed: {type(e).__name__}",
        })

    any_down = any(c["status"] == "down" for c in checks)
    any_degraded = any(c["status"] == "degraded" for c in checks)
    overall = "down" if any_down else ("degraded" if any_degraded else "ok")

    return {
        "status": overall,
        "service": "astra-stream",
        "checks": checks,
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
    attachments: list[str] | None = Field(
        default=None,
        description="UUIDs returned by POST /uploads. Each gets fetched "
        "and embedded as an image content block on the user message.",
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


@app.get("/bridge/poll")
async def bridge_poll(request: Request) -> dict[str, object]:
    """Long-poll endpoint the local bridge daemon hits.

    The daemon presents its token via Authorization: Bearer <token>.
    We auth, claim the next pending call for that token, and return
    it. If no pending call exists we wait up to 25s before returning
    an empty body so the daemon can reconnect (most network
    middleboxes drop idle HTTP at 30s).
    """
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    token = auth.split(None, 1)[1].strip()

    from astra.runtime.bridge import (  # type: ignore[import-not-found]
        validate_bridge_token,
        claim_pending_call,
    )

    bt = await validate_bridge_token(token)
    if bt is None:
        raise HTTPException(401, "invalid or revoked bridge token")

    # Try claiming immediately; if nothing's there, poll briefly.
    deadline = asyncio.get_event_loop().time() + 25
    while True:
        call = await claim_pending_call(bt.id)
        if call is not None:
            return {
                "call": {
                    "id": call.id,
                    "tool": call.tool_name,
                    "args": call.args,
                }
            }
        if asyncio.get_event_loop().time() >= deadline:
            return {"call": None}
        await asyncio.sleep(0.5)


class BridgeResultBody(BaseModel):
    call_id: int
    ok: bool
    result: str = ""
    error_message: str | None = None


@app.post("/bridge/result")
async def bridge_result(body: BridgeResultBody, request: Request) -> dict[str, object]:
    """Daemon posts a tool execution result here. Status flips to
    'complete' or 'failed' and the waiting Astra tool unblocks."""
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    token = auth.split(None, 1)[1].strip()

    from astra.runtime.bridge import (  # type: ignore[import-not-found]
        validate_bridge_token,
        finalize_call,
    )

    bt = await validate_bridge_token(token)
    if bt is None:
        raise HTTPException(401, "invalid or revoked bridge token")

    await finalize_call(
        body.call_id,
        ok=body.ok,
        result=body.result,
        error_message=body.error_message,
    )
    return {"ok": True}


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


@app.get("/previews/{preview_id}")
async def previews_get(preview_id: str) -> Response:
    """Serve a stored preview by id. Returns the body with the
    preview's stored Content-Type so an iframe (or new tab) can
    render it natively. 404 if missing or expired.

    Headers:
      - X-Frame-Options: SAMEORIGIN — allows iframe embed from our
        own origin (the chat pane), blocks third-party embedding.
      - Content-Security-Policy: tightly scoped — the stored body
        can run inline scripts/styles (the agent generated the
        content; preventing inline would break most useful HTML)
        but can't load resources from arbitrary origins.
      - Cache-Control: short TTL because the body is immutable for
        the row's lifetime; once expired the route 404s anyway.

    Public — no shared-secret check. The preview_id is a UUID; the
    only way to know it is to have received the artifact in a turn
    (which itself is auth-gated upstream). This is the same
    pattern as our other artifact endpoints.
    """
    try:
        from astra.runtime.preview_store import get_preview  # type: ignore[import-not-found]
    except Exception as e:
        raise HTTPException(500, f"preview module load failed: {e}")
    row = await get_preview(preview_id)
    if not row:
        raise HTTPException(404, "preview not found or expired")
    headers = {
        "x-frame-options": "SAMEORIGIN",
        "content-security-policy": (
            "default-src 'self' data: blob:; "
            "img-src 'self' data: blob: https:; "
            "style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; "
            "font-src 'self' data: https:; "
            "frame-ancestors 'self';"
        ),
        "cache-control": "private, max-age=300",
        "x-content-type-options": "nosniff",
    }
    # Binary types (images, PDFs) are stored base64-encoded in the
    # TEXT body column. Decode here so the browser receives raw
    # bytes with the right mimetype. Text types (HTML, plain text,
    # JSON) are stored verbatim and served as-is.
    body = row["body"]
    media_type = row["content_type"]
    if _is_binary_media(media_type):
        import base64
        try:
            body_bytes = base64.b64decode(body)
        except Exception:
            raise HTTPException(500, "preview body is not valid base64")
        return Response(
            content=body_bytes,
            media_type=media_type,
            headers=headers,
        )
    return Response(
        content=body,
        media_type=media_type,
        headers=headers,
    )


def _is_binary_media(content_type: str) -> bool:
    """Whether this content-type was stored base64-encoded in the
    TEXT body and needs decoding on serve. Images and PDFs hit this;
    HTML/plain-text/JSON do not."""
    ct = (content_type or "").lower().split(";", 1)[0].strip()
    return (
        ct.startswith("image/")
        or ct == "application/pdf"
        or ct == "application/octet-stream"
    )


# Accept user-uploaded files (screenshots, photos, attachments).
# Same storage as previews but the workflow is inverted: user
# uploads → returns id → id flows into a chat turn's attachments
# → agent_loop fetches the bytes when building the Anthropic
# user-message content blocks.
_MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB
_ALLOWED_UPLOAD_TYPES = {
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
}


@app.post("/uploads")
async def uploads_create(request: Request) -> dict[str, object]:
    """Accept a multipart-uploaded image, persist it, return its id.

    The browser POSTs via /api/uploads in astra-web which proxies
    here (so the public DNS still goes through Vercel's edge for
    consistency). Body is base64-encoded in the previews row so
    the existing storage layer + TTL apply unchanged.

    Auth: same shared-secret check as the agent endpoints —
    uploads are a foot-gun if exposed unauthed (storage cost,
    moderation surface).
    """
    _check_secret(request)
    form = await request.form()
    file = form.get("file")
    if file is None or not hasattr(file, "filename"):
        raise HTTPException(400, "missing 'file' field")
    media_type = (getattr(file, "content_type", None) or "").lower()
    if media_type not in _ALLOWED_UPLOAD_TYPES:
        raise HTTPException(
            415,
            f"unsupported media type {media_type!r}; allowed: "
            f"{sorted(_ALLOWED_UPLOAD_TYPES)}",
        )
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "empty file")
    if len(raw) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            413,
            f"file exceeds {_MAX_UPLOAD_BYTES} bytes ({len(raw)} given)",
        )

    import base64
    encoded = base64.b64encode(raw).decode("ascii")
    title = getattr(file, "filename", None) or "upload"

    try:
        from astra.runtime.preview_store import create_preview  # type: ignore[import-not-found]
    except Exception as e:
        raise HTTPException(500, f"preview module load failed: {e}")
    try:
        preview_id = await create_preview(
            title=str(title),
            body=encoded,
            content_type=media_type,
        )
    except ValueError as e:
        raise HTTPException(413, f"upload rejected: {e}")
    return {
        "id": preview_id,
        "content_type": media_type,
        "byte_count": len(raw),
    }


@app.post("/stream-lean")
async def stream_lean(req: StreamRequest, request: Request) -> StreamingResponse:
    """Run a turn against the LEAN runtime — direct anthropic.AsyncAnthropic,
    no SDK, no bundled CLI subprocess.

    Phase 2 of the runtime migration: text-only (no tools yet — Phase 3).
    Lives alongside /stream so we can test the lean path against real
    Anthropic API in production without affecting the SDK path. Once
    Phase 5 lands, /stream itself will route here.
    """
    _check_secret(request)
    if not req.prompt.strip():
        raise HTTPException(status_code=400, detail="prompt is empty")

    # Lazy import so the service still boots if astra core fails to import.
    # The astra.runtime.tools side-effect import registers every lean-runtime
    # tool with REGISTRY before the agent loop reads it.
    try:
        import astra.runtime.tools  # type: ignore[import-not-found]  # noqa: F401
        from astra.runtime.agent_loop import run_lean_turn  # type: ignore[import-not-found]
        from astra.core.system_prompt import get_system_prompt  # type: ignore[import-not-found]
    except Exception as e:
        from stream.events import error as sse_error, done

        async def _err_gen():
            yield sse_error(f"failed to load lean runtime: {e}")
            yield done(duration_ms=0)

        return StreamingResponse(
            _err_gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform"},
        )

    # Create the durable turn row up-front so a mid-stream crash still
    # leaves a record. Same store as /stream uses.
    from astra.runtime.turn_store import create_turn_record  # type: ignore[import-not-found]

    turn_id = await create_turn_record(
        session_id=req.session_id,
        prompt=req.prompt,
    )

    async def generate():
        runner_iter = run_lean_turn(
            req.prompt,
            session_id=req.session_id,
            system_prompt=get_system_prompt(),
            turn_id=turn_id,
            load_history=True,
        ).__aiter__()
        while True:
            try:
                frame = await asyncio.wait_for(
                    runner_iter.__anext__(),
                    timeout=15,
                )
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                yield heartbeat()
                continue
            except Exception as e:
                from stream.events import error as sse_error
                logger.exception("lean stream raised")
                yield sse_error(f"lean runtime crashed: {e}")
                return
            else:
                yield frame

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── In-flight task registry ───────────────────────────────
#
# When the browser cancels a turn (Esc, dismiss, navigate away),
# we want to actually stop the agent run on the server — not let
# it burn API tokens for 4 more minutes until the runner watchdog
# fires. This dict maps turn_id → asyncio.Task so /turns/<id>/cancel
# can call task.cancel().
#
# Sweeper runs every 5 minutes to drop done/cancelled tasks so the
# dict doesn't grow unbounded across container lifetime.
_running_turns: dict[int, asyncio.Task[Any]] = {}


@app.on_event("startup")
async def _sweep_running_turns_dict() -> None:
    """Background loop that prunes finished tasks from _running_turns."""
    async def _loop() -> None:
        while True:
            try:
                await asyncio.sleep(300)
                stale = [
                    tid for tid, t in _running_turns.items()
                    if t.done() or t.cancelled()
                ]
                for tid in stale:
                    _running_turns.pop(tid, None)
                if stale:
                    logger.info(
                        "[turns] sweeper pruned %d finished task(s)", len(stale)
                    )
            except Exception:
                logger.exception("[turns] sweeper loop iteration failed")
    asyncio.create_task(_loop())


@app.post("/turns/start")
async def turns_start(req: StreamRequest, request: Request) -> dict[str, object]:
    """Start an agent turn in the background. Returns immediately
    with the turn_id; events flow into turn_events durably as the
    agent runs.

    Replaces the SSE-streaming model with poll: the browser hits
    this endpoint to enqueue work + get an id, then polls
    /api/turns/<id>/events for progress + completion. No
    streaming-duration cap matters — the request returns in <100ms.

    The agent run is an asyncio.create_task. It survives this
    request returning, but is bound to the uvicorn worker's event
    loop. If the worker dies (deploy, crash, OOM) the task dies
    too — the turn row stays at status='running' until the
    startup-sweeper marks it interrupted.
    """
    _check_secret(request)
    if not req.prompt.strip():
        raise HTTPException(status_code=400, detail="prompt is empty")

    try:
        import astra.runtime.tools  # type: ignore[import-not-found]  # noqa: F401
        from astra.runtime.agent_loop import run_lean_turn  # type: ignore[import-not-found]
        from astra.runtime.turn_store import create_turn_record  # type: ignore[import-not-found]
        from astra.core.system_prompt import get_system_prompt  # type: ignore[import-not-found]
    except Exception as e:
        raise HTTPException(500, f"runtime load failed: {e}")

    turn_id = await create_turn_record(
        session_id=req.session_id,
        prompt=req.prompt,
    )
    if turn_id is None:
        raise HTTPException(500, "failed to create turn row")

    # Drive the agent loop to exhaustion. Each yielded frame is
    # already written to turn_events by the loop's _emit helper —
    # we just need to consume the generator so it runs. No SSE
    # client; the browser polls turn_events.
    async def _drive() -> None:
        try:
            agen = run_lean_turn(
                req.prompt,
                session_id=req.session_id,
                system_prompt=get_system_prompt(),
                turn_id=turn_id,
                load_history=True,
                attachments=req.attachments,
            ).__aiter__()
            while True:
                try:
                    await agen.__anext__()
                except StopAsyncIteration:
                    break
        except asyncio.CancelledError:
            logger.info(
                "[turns] task for turn=%s cancelled by client", turn_id
            )
            raise
        except Exception:
            logger.exception("[turns] task for turn=%s raised", turn_id)
        finally:
            _running_turns.pop(turn_id, None)

    task = asyncio.create_task(_drive(), name=f"turn-{turn_id}")
    _running_turns[turn_id] = task

    logger.info(
        "[turns] started turn=%s session=%s len(prompt)=%d",
        turn_id,
        req.session_id or "(new)",
        len(req.prompt),
    )

    return {
        "turn_id": turn_id,
        "session_id": req.session_id,
        "status": "running",
    }


@app.post("/turns/{turn_id}/cancel")
async def turns_cancel(turn_id: int, request: Request) -> dict[str, object]:
    """Cancel an in-flight turn. Returns whether a task was actually
    cancelled (False if it had already completed)."""
    _check_secret(request)
    task = _running_turns.get(turn_id)
    if task is None:
        return {"cancelled": False, "reason": "not running"}
    if task.done():
        return {"cancelled": False, "reason": "already finished"}
    task.cancel()
    return {"cancelled": True}


@app.post("/stream")
async def stream(req: StreamRequest, request: Request) -> StreamingResponse:
    """Run an Astra query and stream the result as SSE.

    PHASE 6: this endpoint is now ONLY served by the lean runtime
    (astra.runtime.agent_loop). The legacy SDK path was removed
    after Phase 5 ran traffic without regressions. Rollback is no
    longer a fallback toggle — it's a `git revert`.
    """
    _check_secret(request)
    if not req.prompt.strip():
        raise HTTPException(status_code=400, detail="prompt is empty")

    try:
        import astra.runtime.tools  # type: ignore[import-not-found]  # noqa: F401
        from astra.runtime.agent_loop import run_lean_turn  # type: ignore[import-not-found]
        from astra.runtime.turn_store import create_turn_record  # type: ignore[import-not-found]
        from astra.core.system_prompt import get_system_prompt  # type: ignore[import-not-found]
    except Exception as e:
        async def _err_gen():
            yield sse_error(f"failed to load lean runtime: {e}")
            yield done_event(duration_ms=0)

        return StreamingResponse(
            _err_gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform"},
        )

    turn_id = await create_turn_record(
        session_id=req.session_id,
        prompt=req.prompt,
    )

    async def generate():
        runner_iter = run_lean_turn(
            req.prompt,
            session_id=req.session_id,
            system_prompt=get_system_prompt(),
            turn_id=turn_id,
            load_history=True,
        ).__aiter__()
        while True:
            try:
                frame = await asyncio.wait_for(
                    runner_iter.__anext__(), timeout=15
                )
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                yield heartbeat()
                continue
            except Exception as e:
                logger.exception("[lean-runtime] /stream raised")
                yield sse_error(f"lean runtime crashed: {e}")
                return
            else:
                yield frame

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
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
