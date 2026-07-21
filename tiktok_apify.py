"""Scrape des reposts TikTok via Apify."""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import urlparse

from apify_client import ApifyClient

DEFAULT_ACTOR = "clockworks/tiktok-scraper"
_HANDLE_RE = re.compile(r"@([A-Za-z0-9._]+)")


def extract_handle(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        raise ValueError("Colle un lien TikTok ou un @handle.")

    m = _HANDLE_RE.search(text)
    if m:
        return m.group(1)

    if "tiktok.com" in text.lower():
        path = urlparse(text).path.strip("/")
        parts = path.split("/")
        if parts and parts[0].startswith("@"):
            return parts[0][1:]
        if parts:
            return parts[0].lstrip("@")

    handle = text.lstrip("@").split("/")[0].strip()
    if not handle or not re.match(r"^[A-Za-z0-9._]+$", handle):
        raise ValueError("Handle TikTok invalide — ex. @user ou https://www.tiktok.com/@user")
    return handle


def _apify_token() -> str:
    token = os.getenv("APIFY_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "APIFY_TOKEN manquant — crée un compte Apify et ajoute le token dans .env"
        )
    return token


def _normalize_item(item: dict[str, Any]) -> dict[str, Any] | None:
    """Uniformise les champs selon l'actor Clockworks / autres."""
    if not isinstance(item, dict):
        return None

    caption = (
        item.get("text")
        or item.get("desc")
        or item.get("description")
        or item.get("caption")
        or ""
    )
    caption = str(caption).strip()

    author = ""
    author_obj = item.get("authorMeta") or item.get("author") or {}
    if isinstance(author_obj, dict):
        author = (
            author_obj.get("name")
            or author_obj.get("uniqueId")
            or author_obj.get("nickName")
            or ""
        )
    elif isinstance(author_obj, str):
        author = author_obj

    music = ""
    music_obj = item.get("musicMeta") or item.get("music") or {}
    if isinstance(music_obj, dict):
        music = music_obj.get("musicName") or music_obj.get("title") or music_obj.get("name") or ""
        artist = music_obj.get("musicAuthor") or music_obj.get("authorName") or ""
        if artist:
            music = f"{music} — {artist}".strip(" —")
    elif isinstance(music_obj, str):
        music = music_obj

    hashtags: list[str] = []
    raw_tags = item.get("hashtags") or item.get("challenges") or []
    if isinstance(raw_tags, list):
        for t in raw_tags:
            if isinstance(t, str):
                hashtags.append(t.lstrip("#"))
            elif isinstance(t, dict):
                name = t.get("name") or t.get("title") or ""
                if name:
                    hashtags.append(str(name).lstrip("#"))

    if not hashtags and caption:
        hashtags = [h.lstrip("#") for h in re.findall(r"#([\w\u00C0-\u024F]+)", caption)]

    url = (
        item.get("webVideoUrl")
        or item.get("url")
        or item.get("videoUrl")
        or ""
    )

    stats = item.get("videoMeta") or item.get("stats") or {}
    plays = item.get("playCount") or (stats.get("playCount") if isinstance(stats, dict) else 0) or 0
    likes = item.get("diggCount") or item.get("likes") or (stats.get("diggCount") if isinstance(stats, dict) else 0) or 0

    if not caption and not hashtags and not music and not author:
        return None

    return {
        "caption": caption[:500],
        "author": str(author).strip(),
        "music": str(music).strip()[:200],
        "hashtags": hashtags[:20],
        "url": str(url).strip(),
        "plays": int(plays) if str(plays).isdigit() or isinstance(plays, (int, float)) else 0,
        "likes": int(likes) if str(likes).isdigit() or isinstance(likes, (int, float)) else 0,
    }


def fetch_reposts(profile_url: str, *, max_items: int = 200) -> tuple[str, list[dict[str, Any]]]:
    """
    Lance l'actor Apify et retourne (handle, liste normalisée).
    Analyse = métadonnées (captions, hashtags, sons, auteurs) — pas le flux vidéo brut.
    """
    handle = extract_handle(profile_url)
    max_items = max(10, min(int(max_items), 1000))
    actor_id = os.getenv("APIFY_TIKTOK_ACTOR", DEFAULT_ACTOR).strip() or DEFAULT_ACTOR

    client = ApifyClient(_apify_token())

    # Clockworks TikTok Scraper — section Reposts
    run_input: dict[str, Any] = {
        "profiles": [handle],
        "profileScrapeSections": ["reposts"],
        "resultsPerPage": max_items,
        "shouldDownloadVideos": False,
        "shouldDownloadCovers": False,
        "shouldDownloadSlideshowImages": False,
        "shouldDownloadSubtitles": False,
    }

    run = client.actor(actor_id).call(run_input=run_input, timeout_secs=900)
    if not run:
        raise RuntimeError("Apify n'a renvoyé aucun run.")

    dataset_id = run.get("defaultDatasetId")
    if not dataset_id:
        raise RuntimeError("Pas de dataset Apify — vérifie le crédit / l'actor.")

    items: list[dict[str, Any]] = []
    for raw in client.dataset(dataset_id).iterate_items():
        norm = _normalize_item(raw)
        if norm:
            items.append(norm)
        if len(items) >= max_items:
            break

    if not items:
        raise RuntimeError(
            "Aucun repost trouvé. Le profil est peut‑être privé, sans reposts publics, "
            "ou l'actor Apify a besoin de proxy / crédits."
        )

    return handle, items
