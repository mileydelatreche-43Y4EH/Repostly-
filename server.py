"""Repostly — scrape local + analyse Claude."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from analyze import analyze_profile
from tiktok_local import extract_handle, fetch_profile_content, fetch_profile_quick

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

app = FastAPI(title="Repostly", docs_url=None, redoc_url=None)


class ProfileRequest(BaseModel):
    profile: str = Field(..., min_length=2, max_length=300)


class AnalyzeRequest(BaseModel):
    profile: str = Field(..., min_length=2, max_length=300)
    max_reposts: int = Field(100, ge=100, le=1000)


@app.get("/")
async def index():
    return FileResponse(
        ROOT / "static" / "index.html",
        headers={"Cache-Control": "no-store"},
    )


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
    """Photo + bio + compteurs — rapide, pour l'animation."""
    headless = os.getenv("SCRAPE_HEADLESS", "1").strip() not in ("0", "false", "False")
    try:
        extract_handle(req.profile)
        profile = await asyncio.to_thread(
            fetch_profile_quick, req.profile, headless=headless
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(502, f"Profil inaccessible : {e}") from e
    return profile


@app.post("/api/analyze")
async def api_analyze(req: AnalyzeRequest):
    if not os.getenv("ANTHROPIC_API_KEY", "").strip():
        raise HTTPException(400, "ANTHROPIC_API_KEY manquante dans .env")

    # Le choix UI prime toujours (100 / 500 / 1000) — pas d'override .env
    allowed = {100, 500, 1000}
    max_items = req.max_reposts if req.max_reposts in allowed else 100
    headless = os.getenv("SCRAPE_HEADLESS", "1").strip() not in ("0", "false", "False")

    try:
        extract_handle(req.profile)
        handle, posts, reposts, profile = await asyncio.to_thread(
            fetch_profile_content,
            req.profile,
            max_items=max_items,
            headless=headless,
        )
        analysis = await analyze_profile(handle, posts, reposts, profile)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except RuntimeError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(502, f"Erreur scrape/analyse : {e}") from e

    return {
        "handle": handle,
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


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
