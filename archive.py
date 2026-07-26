"""Télécharge toutes les vidéos d'un compte + paroles (sous-titres TikTok)."""

from __future__ import annotations

import json
import os
import re
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from tiktok_local import extract_handle, fetch_user_posts

ROOT = Path(__file__).resolve().parent
ARCHIVES = ROOT / "data" / "archives"

ProgressCb = Callable[[str], None]
ProfileCb = Callable[[dict], None]

# 0 = toutes les vidéos du compte
ALLOWED_MAX = (0, 10, 20, 50, 100, 250, 500, 1000, 2000)
KEYWORD = "cheaterbuster"


def _is_light() -> bool:
    return os.getenv("SCRAPE_LIGHT", "0").strip() not in ("0", "false", "False")


def _safe_handle(handle: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", handle)[:64]


def archive_dir(handle: str) -> Path:
    d = ARCHIVES / _safe_handle(handle)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _progress(cb: ProgressCb | None, msg: str) -> None:
    if cb:
        try:
            cb(msg)
        except Exception:
            pass


def _hashtags(text: str) -> list[str]:
    return [h.lstrip("#") for h in re.findall(r"#([\w\u00C0-\u024F]+)", text or "")][:20]


def _thumb(entry: dict[str, Any]) -> str:
    thumbs = entry.get("thumbnails") or []
    if isinstance(thumbs, list) and thumbs:
        best = max(
            (t for t in thumbs if isinstance(t, dict) and t.get("url")),
            key=lambda t: int(t.get("preference") or 0),
            default=None,
        )
        if best:
            return str(best.get("url") or "")
        return str(thumbs[0].get("url") or "")
    return str(entry.get("thumbnail") or "")


def _vtt_to_text(path: Path) -> str:
    """Convertit un fichier WEBVTT en texte des paroles."""
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""
    lines: list[str] = []
    seen_tail: list[str] = []
    for line in raw.splitlines():
        t = line.strip()
        if not t or t.startswith("WEBVTT") or t.startswith("NOTE") or "-->" in t:
            continue
        if re.fullmatch(r"\d+", t):
            continue
        t = re.sub(r"<[^>]+>", "", t).strip()
        if not t:
            continue
        # dédup des lignes répétées (TikTok double souvent)
        if seen_tail and t == seen_tail[-1]:
            continue
        seen_tail.append(t)
        if len(seen_tail) > 5:
            seen_tail.pop(0)
        lines.append(t)
    return "\n".join(lines).strip()


def _find_vtt_text(out_dir: Path, vid: str) -> str:
    candidates = sorted(out_dir.glob(f"{vid}*.vtt"))
    best = ""
    for p in candidates:
        text = _vtt_to_text(p)
        if len(text) > len(best):
            best = text
    return best


def list_posts_ytdlp(
    handle: str,
    *,
    max_items: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Liste les vidéos du profil via yt-dlp. max_items=0 → toutes."""
    try:
        import yt_dlp
    except ImportError as e:
        raise RuntimeError("yt-dlp manquant — pip install yt-dlp") from e

    url = f"https://www.tiktok.com/@{handle}"
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
        "ignoreerrors": True,
    }
    if max_items > 0:
        opts["playlistend"] = max_items

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if not isinstance(info, dict):
        return [], {"handle": handle, "nickname": handle}

    entries = [e for e in (info.get("entries") or []) if isinstance(e, dict)]
    posts: list[dict[str, Any]] = []
    nickname = (
        info.get("channel")
        or info.get("uploader")
        or (entries[0].get("channel") if entries else "")
        or handle
    )
    limit = len(entries) if max_items <= 0 else max_items
    for e in entries[:limit]:
        vid = str(e.get("id") or "").strip()
        if not vid:
            continue
        title = str(e.get("title") or e.get("description") or "").strip()
        posts.append(
            {
                "kind": "post",
                "id": vid,
                "caption": title[:500],
                "author": str(e.get("uploader") or handle),
                "music": "",
                "hashtags": _hashtags(title),
                "url": e.get("url")
                or e.get("webpage_url")
                or f"https://www.tiktok.com/@{handle}/video/{vid}",
                "cover": _thumb(e),
                "plays": int(e.get("view_count") or 0),
                "likes": int(e.get("like_count") or 0),
                "create_time": int(e.get("timestamp") or 0),
            }
        )

    profile = {
        "handle": handle,
        "nickname": str(nickname).strip() or handle,
        "avatar": "",
        "avatar_url": "",
        "bio": "",
        "video_count": len(posts),
        "repost_count": 0,
    }
    return posts, profile


def _download_one(url: str, out_path: Path) -> tuple[Path | None, str]:
    """
    Télécharge MP4 + sous-titres (paroles).
    Retourne (chemin_mp4, texte_paroles_vtt).
    """
    try:
        import yt_dlp
    except ImportError as e:
        raise RuntimeError("yt-dlp manquant — pip install yt-dlp") from e

    out_path.parent.mkdir(parents=True, exist_ok=True)
    stem = out_path.with_suffix("")
    tmpl = str(stem) + ".%(ext)s"
    opts = {
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "outtmpl": tmpl,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 4,
        "fragment_retries": 4,
        "socket_timeout": 45,
        "concurrent_fragment_downloads": 3,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["all"],
        "subtitlesformat": "vtt",
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.tiktok.com/",
        },
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])

    vid = out_path.stem
    spoken = _find_vtt_text(out_path.parent, vid)

    candidates = list(out_path.parent.glob(out_path.stem + ".*"))
    candidates = [
        p
        for p in candidates
        if p.suffix.lower() in (".mp4", ".webm", ".mkv", ".mov")
        and p.stat().st_size > 5_000
    ]
    downloaded: Path | None = None
    if candidates:
        best = max(candidates, key=lambda p: p.stat().st_size)
        if best.suffix.lower() != ".mp4":
            target = out_path.with_suffix(".mp4")
            try:
                best.replace(target)
                downloaded = target
            except Exception:
                downloaded = best
        else:
            downloaded = best
    elif out_path.with_suffix(".mp4").exists():
        downloaded = out_path.with_suffix(".mp4")

    return downloaded, spoken


def _ensure_subs_only(url: str, out_dir: Path, vid: str) -> str:
    """Si MP4 déjà là : récupère seulement les sous-titres."""
    existing = _find_vtt_text(out_dir, vid)
    if existing:
        return existing
    try:
        import yt_dlp
    except ImportError:
        return ""
    tmpl = str(out_dir / f"{vid}.%(ext)s")
    opts = {
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["all"],
        "subtitlesformat": "vtt",
        "outtmpl": tmpl,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception:
        return ""
    return _find_vtt_text(out_dir, vid)


def run_archive(
    profile: str,
    *,
    max_videos: int = 100,
    headless: bool = True,
    on_profile: ProfileCb | None = None,
    on_progress: ProgressCb | None = None,
) -> dict[str, Any]:
    """
    1) Liste toutes / N vidéos (yt-dlp)
    2) Télécharge MP4 + sous-titres (= paroles)
    3) Texte prioritaire = paroles VTT, sinon caption
    """
    handle = extract_handle(profile)
    light = _is_light()
    if max_videos not in ALLOWED_MAX:
        max_videos = 100
    if light and (max_videos == 0 or max_videos > 20):
        max_videos = 20

    label = "toutes" if max_videos == 0 else str(max_videos)
    _progress(on_progress, f"Liste des vidéos @{handle} (cible {label})…")
    posts: list[dict[str, Any]] = []
    profile_data: dict[str, Any] = {"handle": handle, "nickname": handle}
    list_err = ""

    try:
        posts, profile_data = list_posts_ytdlp(handle, max_items=max_videos)
        _progress(on_progress, f"{len(posts)} vidéos listées…")
    except Exception as e:
        list_err = str(e)
        _progress(on_progress, f"yt-dlp échoué — fallback navigateur… ({e})")

    if on_profile:
        try:
            on_profile(dict(profile_data))
        except Exception:
            pass

    if not posts:
        fb_max = 100 if max_videos == 0 else max_videos
        if fb_max not in (10, 20, 50, 100, 250, 500):
            fb_max = 100
        try:
            handle, posts, profile_data = fetch_user_posts(
                profile,
                max_items=fb_max,
                headless=headless,
                on_profile=on_profile,
                on_progress=on_progress,
            )
        except Exception as e:
            raise RuntimeError(
                "Aucune vidéo trouvée. "
                + (f"yt-dlp: {list_err}. " if list_err else "")
                + f"Navigateur: {e}"
            ) from e

    if not posts:
        raise RuntimeError(
            "Aucune vidéo postée trouvée pour ce compte "
            "(TikTok a peut‑être limité la requête)."
        )

    out_dir = archive_dir(handle)
    total = len(posts)
    workers = 1 if light else min(3, max(1, total))
    download_map: dict[str, tuple[Path | None, str, str]] = {}

    def _job(post: dict[str, Any], idx: int) -> tuple[str, Path | None, str, str]:
        vid = str(post.get("id") or f"idx{idx}")
        url = post.get("url") or f"https://www.tiktok.com/@{handle}/video/{vid}"
        video_path = out_dir / f"{vid}.mp4"
        err = ""
        downloaded: Path | None = None
        spoken = ""
        try:
            if video_path.exists() and video_path.stat().st_size > 10_000:
                downloaded = video_path
                spoken = _ensure_subs_only(url, out_dir, vid)
            else:
                downloaded, spoken = _download_one(url, video_path)
                if not downloaded:
                    err = "téléchargement vide"
        except Exception as e:
            err = str(e)
            downloaded = None
        return vid, downloaded, spoken, err

    _progress(on_progress, f"Téléchargement + paroles {total} vidéos…")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_job, post, i): (i, post)
            for i, post in enumerate(posts, start=1)
        }
        done_n = 0
        for fut in as_completed(futures):
            i, post = futures[fut]
            vid, downloaded, spoken, err = fut.result()
            download_map[vid] = (downloaded, spoken, err)
            done_n += 1
            _progress(on_progress, f"Téléchargement {done_n}/{total}…")

    items_out: list[dict[str, Any]] = []
    for i, post in enumerate(posts, start=1):
        vid = str(post.get("id") or f"idx{i}")
        url = post.get("url") or f"https://www.tiktok.com/@{handle}/video/{vid}"
        caption = (post.get("caption") or "").strip()
        downloaded, spoken, err = download_map.get(vid, (None, "", "manquant"))
        transcript_path = out_dir / f"{vid}.txt"
        meta_path = out_dir / f"{vid}.json"

        # Paroles (sous-titres) en priorité, sinon caption
        if spoken:
            transcript = spoken
            source = "subtitles"
        elif caption:
            transcript = caption
            source = "description"
        else:
            transcript = ""
            source = ""

        if transcript:
            transcript_path.write_text(transcript, encoding="utf-8")

        file_name = downloaded.name if downloaded else ""
        size_bytes = downloaded.stat().st_size if downloaded and downloaded.exists() else 0
        has_kw = bool(re.search(re.escape(KEYWORD), transcript, re.I)) or bool(
            re.search(re.escape(KEYWORD), caption, re.I)
        )
        meta = {
            "id": vid,
            "url": url,
            "caption": caption,
            "author": post.get("author") or handle,
            "music": post.get("music") or "",
            "hashtags": post.get("hashtags") or [],
            "cover": post.get("cover") or "",
            "plays": post.get("plays") or 0,
            "likes": post.get("likes") or 0,
            "create_time": post.get("create_time") or 0,
            "file": file_name,
            "file_size": size_bytes,
            "transcript": transcript,
            "transcript_source": source,
            "has_keyword": has_kw,
            "error": err,
        }
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        items_out.append(meta)

    # Hits keyword d'abord
    items_out.sort(key=lambda x: (not x.get("has_keyword"),))

    all_text = "\n\n———\n\n".join(
        f"[{i}] {it.get('caption') or it.get('id')}\n{it.get('transcript') or ''}".strip()
        for i, it in enumerate(items_out, start=1)
        if it.get("transcript")
    )
    if all_text:
        (out_dir / "all_transcripts.txt").write_text(all_text, encoding="utf-8")

    kw_only = "\n\n———\n\n".join(
        f"[{it.get('id')}]\n{it.get('transcript') or ''}".strip()
        for it in items_out
        if it.get("has_keyword")
    )
    if kw_only:
        (out_dir / "cheaterbuster_only.txt").write_text(kw_only, encoding="utf-8")

    kw_count = sum(1 for x in items_out if x.get("has_keyword"))
    manifest = {
        "handle": handle,
        "profile": profile_data,
        "requested": max_videos,
        "found": total,
        "downloaded": sum(1 for x in items_out if x.get("file")),
        "transcribed": sum(1 for x in items_out if x.get("transcript")),
        "keyword": KEYWORD,
        "keyword_hits": kw_count,
        "local_mode": not light,
        "out_dir": str(out_dir),
        "items": items_out,
        "all_transcripts": "all_transcripts.txt" if all_text else "",
        "keyword_file": "cheaterbuster_only.txt" if kw_only else "",
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    _progress(on_progress, f"Création du ZIP… ({kw_count}× {KEYWORD})")
    zip_path = out_dir / f"{_safe_handle(handle)}_archive.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in out_dir.iterdir():
            if p.name == zip_path.name:
                continue
            if p.suffix.lower() in (".mp4", ".txt", ".json", ".webm", ".mkv", ".vtt"):
                zf.write(p, arcname=p.name)

    manifest["zip"] = zip_path.name
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _progress(on_progress, "Archive prête")
    return manifest


def get_archive_file(handle: str, filename: str) -> Path | None:
    safe = _safe_handle(extract_handle(handle) if "@" in handle or "/" in handle else handle)
    name = Path(filename).name
    if not name or name in (".", ".."):
        return None
    path = ARCHIVES / safe / name
    if path.is_file() and path.resolve().is_relative_to((ARCHIVES / safe).resolve()):
        return path
    return None
