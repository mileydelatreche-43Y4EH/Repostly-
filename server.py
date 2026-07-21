"""Repostly — scrape local + analyse Claude."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from analyze import analyze_profile
from tiktok_local import extract_handle, fetch_profile_content, fetch_profile_quick

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("repostly")

app = FastAPI(title="Repostly", docs_url=None, redoc_url=None)


class ProfileRequest(BaseModel):
    profile: str = Field(..., min_length=2, max_length=300)


class AnalyzeRequest(BaseModel):
    profile: str = Field(..., min_length=2, max_length=300)
    max_reposts: int = Field(100, ge=100, le=1000)


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.middleware("http")
async def log_requests(request: Request, call_next):
    if request.url.path.startswith("/api/"):
        log.info("→ %s %s", request.method, request.url.path)
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        log.exception("✗ %s %s crash", request.method, request.url.path)
        raise
    ms = (time.perf_counter() - started) * 1000
    # Les streams SSE se terminent "tout de suite" côté middleware — ne pas mentir
    is_stream = getattr(response, "media_type", None) == "text/event-stream"
    if request.url.path.startswith("/api/") and not is_stream:
        log.info(
            "← %s %s → %s (%.0f ms)",
            request.method,
            request.url.path,
            response.status_code,
            ms,
        )
    elif is_stream:
        log.info("← %s %s → stream ouvert", request.method, request.url.path)
    return response


@app.api_route("/", methods=["GET", "HEAD"])
async def index():
    return FileResponse(
        ROOT / "static" / "index.html",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/health")
async def health():
    return {"ok": True, "service": "repostly"}


@app.get("/api/avatar")
async def api_avatar(u: str = Query(..., min_length=8, max_length=2000)):
    """Proxy photo TikTok (CDN bloque souvent le hotlink direct)."""
    if not u.startswith("https://"):
        raise HTTPException(400, "URL avatar invalide")
    host_ok = any(
        x in u.lower()
        for x in ("tiktok", "byteoversea", "bytedance", "ibyteimg", "tiktokcdn")
    )
    if not host_ok:
        raise HTTPException(400, "URL avatar invalide")
    try:
        import httpx

        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            r = await client.get(
                u,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/122.0.0.0 Safari/537.36"
                    ),
                    "Referer": "https://www.tiktok.com/",
                },
            )
        if r.status_code >= 400 or not r.content:
            raise HTTPException(502, "Avatar inaccessible")
        ctype = r.headers.get("content-type", "image/jpeg").split(";")[0]
        if "image" not in ctype:
            ctype = "image/jpeg"
        return Response(
            content=r.content,
            media_type=ctype,
            headers={"Cache-Control": "public, max-age=86400"},
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Avatar : {e}") from e


@app.post("/api/profile")
async def api_profile(req: ProfileRequest):
    """Photo + bio + compteurs — rapide (optionnel, le stream analyse suffit)."""
    headless = os.getenv("SCRAPE_HEADLESS", "1").strip() not in ("0", "false", "False")
    handle = extract_handle(req.profile)
    log.info("profile quick start @%s (headless=%s)", handle, headless)
    try:
        profile = await asyncio.to_thread(
            fetch_profile_quick, req.profile, headless=headless
        )
        log.info(
            "profile quick ok @%s photo=%s",
            handle,
            bool(profile.get("avatar") or profile.get("avatar_url")),
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        log.exception("profile quick fail @%s", handle)
        raise HTTPException(502, f"Profil inaccessible : {e}") from e
    return profile


@app.post("/api/analyze")
async def api_analyze(req: AnalyzeRequest):
    """Analyse en stream SSE : photo tôt, puis résultat final (1 seul navigateur)."""
    if not os.getenv("ANTHROPIC_API_KEY", "").strip():
        raise HTTPException(
            400,
            "ANTHROPIC_API_KEY manquante dans les variables d'environnement Render",
        )

    allowed = {100, 500, 1000}
    max_items = req.max_reposts if req.max_reposts in allowed else 100
    headless = os.getenv("SCRAPE_HEADLESS", "1").strip() not in ("0", "false", "False")
    handle = extract_handle(req.profile)
    log.info("analyze stream start @%s max=%s", handle, max_items)

    async def event_stream():
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        box: dict = {}

        def emit(obj: dict) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, obj)

        def on_profile(profile: dict) -> None:
            log.info(
                "stream profile @%s photo=%s url=%s",
                handle,
                bool(profile.get("avatar") or profile.get("avatar_url")),
                (profile.get("avatar_url") or "")[:60],
            )
            emit({"type": "profile", "data": profile})

        def on_progress(message: str) -> None:
            log.info("stream progress @%s: %s", handle, message)
            emit({"type": "progress", "message": message})

        def work() -> None:
            try:
                emit({"type": "progress", "message": "Lancement navigateur…"})
                h, posts, reposts, profile = fetch_profile_content(
                    req.profile,
                    max_items=max_items,
                    headless=headless,
                    on_profile=on_profile,
                    on_progress=on_progress,
                )
                box["ok"] = (h, posts, reposts, profile)
            except Exception as e:
                box["err"] = e
                log.exception("scrape thread fail @%s", handle)
            finally:
                emit({"type": "_done"})

        worker = asyncio.create_task(asyncio.to_thread(work))

        # Keepalive SSE (évite buffers / timeouts proxy) + lecture events
        while True:
            try:
                ev = await asyncio.wait_for(queue.get(), timeout=8.0)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                if worker.done():
                    break
                continue
            if ev.get("type") == "_done":
                break
            yield _sse(ev)

        await worker

        if "err" in box:
            err = box["err"]
            msg = str(err)
            yield _sse({"type": "error", "detail": msg})
            return

        if "ok" not in box:
            yield _sse({"type": "error", "detail": "Analyse interrompue."})
            return

        h, posts, reposts, profile = box["ok"]
        log.info(
            "scrape ok @%s posts=%s reposts=%s",
            h,
            len(posts),
            len(reposts),
        )
        yield _sse({"type": "progress", "message": "Analyse IA…"})
        try:
            analysis = await analyze_profile(h, posts, reposts, profile)
        except Exception as e:
            log.exception("claude fail @%s", h)
            yield _sse({"type": "error", "detail": f"Erreur Claude : {e}"})
            return

        log.info("claude ok @%s", h)
        payload = {
            "handle": h,
            "reposts_count": len(reposts),
            "posts_count": len(posts),
            "reposts_requested": max_items,
            "repost_total": int(profile.get("repost_count") or 0),
            "video_total": int(profile.get("video_count") or 0),
            "repost_total_unknown": bool(profile.get("repost_total_unknown")),
            "posts": posts[:24],
            "reposts": reposts[:24],
            "profile": profile,
            "analysis": analysis,
            "source": "local",
        }
        yield _sse({"type": "result", "data": payload})
        log.info("stream complete @%s", h)
    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
