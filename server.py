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
from archive import ensure_browser_mp4, get_archive_file, run_archive
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


class ArchiveRequest(BaseModel):
    profile: str = Field(..., min_length=2, max_length=300)
    # 0 = toutes les vidéos du compte
    max_videos: int = Field(100, ge=0, le=2000)


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
    # Forcer UTF-8 sur les fichiers texte du front (évite TÃ©lÃ©charger)
    ct = response.headers.get("content-type", "")
    if request.url.path.startswith("/static/") and ct and "charset=" not in ct.lower():
        base = ct.split(";")[0].strip().lower()
        if base in (
            "text/css",
            "text/html",
            "text/javascript",
            "application/javascript",
            "application/json",
        ) or base.startswith("text/"):
            response.headers["content-type"] = f"{base}; charset=utf-8"
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
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/health")
async def health():
    light = os.getenv("SCRAPE_LIGHT", "0").strip() not in ("0", "false", "False")
    return {
        "ok": True,
        "service": "repostly",
        "local_mode": not light,
        "archive_max": 20 if light else 0,
    }


@app.get("/api/capabilities")
async def capabilities():
    light = os.getenv("SCRAPE_LIGHT", "0").strip() not in ("0", "false", "False")
    return {
        "local_mode": not light,
        "archive_limits": [10, 20] if light else [100, 500, 0],
        "default_archive": 20 if light else 100,
    }


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
            "repost_total_uncertain": bool(profile.get("repost_total_uncertain")),
            "repost_incomplete": bool(profile.get("repost_incomplete")),
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


@app.post("/api/archive")
async def api_archive(req: ArchiveRequest):
    """Télécharge les vidéos postées + paroles (sous-titres) (SSE)."""
    allowed = {0, 10, 20, 50, 100, 250, 500, 1000, 2000}
    max_videos = req.max_videos if req.max_videos in allowed else 100
    light = os.getenv("SCRAPE_LIGHT", "0").strip() not in ("0", "false", "False")
    if light and (max_videos == 0 or max_videos > 20):
        max_videos = 20
    headless = os.getenv("SCRAPE_HEADLESS", "1").strip() not in ("0", "false", "False")
    handle = extract_handle(req.profile)
    log.info(
        "archive stream start @%s max=%s",
        handle,
        max_videos,
    )

    async def event_stream():
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        box: dict = {}

        def emit(obj: dict) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, obj)

        def on_profile(profile: dict) -> None:
            emit({"type": "profile", "data": profile})

        def on_progress(message: str) -> None:
            log.info("archive progress @%s: %s", handle, message)
            emit({"type": "progress", "message": message})

        def work() -> None:
            try:
                emit({"type": "progress", "message": "Lancement…"})
                manifest = run_archive(
                    req.profile,
                    max_videos=max_videos,
                    headless=headless,
                    on_profile=on_profile,
                    on_progress=on_progress,
                )
                box["ok"] = manifest
            except Exception as e:
                box["err"] = e
                log.exception("archive thread fail @%s", handle)
            finally:
                emit({"type": "_done"})

        worker = asyncio.create_task(asyncio.to_thread(work))

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
            yield _sse({"type": "error", "detail": str(box["err"])})
            return
        if "ok" not in box:
            yield _sse({"type": "error", "detail": "Archive interrompue."})
            return

        manifest = box["ok"]
        # Ne pas renvoyer de blobs — juste métadonnées + textes
        light_items = []
        for it in manifest.get("items") or []:
            light_items.append(
                {
                    "id": it.get("id"),
                    "url": it.get("url"),
                    "caption": it.get("caption"),
                    "music": it.get("music"),
                    "hashtags": it.get("hashtags") or [],
                    "cover": it.get("cover"),
                    "file": it.get("file"),
                    "file_size": it.get("file_size") or 0,
                    "transcript": it.get("transcript"),
                    "transcript_source": it.get("transcript_source"),
                    "has_keyword": bool(it.get("has_keyword")),
                    "error": it.get("error"),
                    "plays": it.get("plays"),
                    "likes": it.get("likes"),
                    "create_time": it.get("create_time"),
                }
            )
        payload = {
            "mode": "archive",
            "handle": manifest.get("handle"),
            "profile": manifest.get("profile") or {},
            "requested": manifest.get("requested"),
            "found": manifest.get("found"),
            "downloaded": manifest.get("downloaded"),
            "transcribed": manifest.get("transcribed"),
            "keyword": manifest.get("keyword") or "cheaterbuster",
            "keyword_hits": manifest.get("keyword_hits") or 0,
            "local_mode": manifest.get("local_mode"),
            "complete": manifest.get("complete"),
            "resumed": manifest.get("resumed"),
            "added": manifest.get("added"),
            "skipped": manifest.get("skipped"),
            "zip": manifest.get("zip"),
            "all_transcripts": manifest.get("all_transcripts"),
            "keyword_file": manifest.get("keyword_file"),
            "items": light_items,
        }
        yield _sse({"type": "result", "data": payload})
        log.info(
            "archive complete @%s dl=%s tx=%s",
            handle,
            payload["downloaded"],
            payload["transcribed"],
        )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/archive/{handle}/snapshot")
async def api_archive_snapshot(handle: str):
    """Résultat figé sur disque — rouvre la page sans re-télécharger."""
    from archive import ARCHIVES, _safe_handle

    safe = _safe_handle(extract_handle(handle) if "@" in handle or "/" in handle else handle)
    path = ARCHIVES / safe / "manifest.json"
    if not path.is_file():
        raise HTTPException(404, "Snapshot introuvable")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(500, f"Snapshot illisible: {e}") from e
    if not isinstance(data, dict):
        raise HTTPException(500, "Snapshot invalide")
    data["mode"] = "archive"
    data.pop("out_dir", None)
    return data


@app.get("/api/archive/{handle}/file/{filename}")
async def api_archive_file(handle: str, filename: str):
    path = get_archive_file(handle, filename)
    if not path:
        raise HTTPException(404, "Fichier introuvable")
    media = "application/octet-stream"
    disposition = "attachment"
    if filename.endswith(".mp4"):
        # Convertit HEVC → H.264 à la volée (sinon image figée dans Chrome)
        path = await asyncio.to_thread(ensure_browser_mp4, path)
        media = "video/mp4"
        disposition = "inline"
    elif filename.endswith(".txt"):
        media = "text/plain; charset=utf-8"
    elif filename.endswith(".zip"):
        media = "application/zip"
    elif filename.endswith(".json"):
        media = "application/json"
    return FileResponse(
        path,
        media_type=media,
        filename=filename,
        content_disposition_type=disposition,
        headers={
            "Cache-Control": "private, max-age=3600",
            "Accept-Ranges": "bytes",
        },
    )


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
