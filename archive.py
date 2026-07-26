"""Télécharge les vidéos postées par un compte TikTok + transcription texte."""

from __future__ import annotations

import json
import os
import re
import zipfile
from pathlib import Path
from typing import Any, Callable

from tiktok_local import extract_handle, fetch_user_posts

ROOT = Path(__file__).resolve().parent
ARCHIVES = ROOT / "data" / "archives"


ProgressCb = Callable[[str], None]
ProfileCb = Callable[[dict], None]


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


def _extract_tiktok_captions(raw: dict[str, Any]) -> str:
    """Sous-titres auto TikTok si présents (gratuit)."""
    chunks: list[str] = []

    def walk(obj: Any, depth: int = 0) -> None:
        if depth > 8 or obj is None:
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                kl = str(k).lower()
                if kl in ("text", "utterance", "content", "subtitle") and isinstance(v, str):
                    t = v.strip()
                    if 2 < len(t) < 500 and not t.startswith("http"):
                        chunks.append(t)
                elif kl in ("captioninfos", "captions", "cla", "subtitleinfos"):
                    walk(v, depth + 1)
                else:
                    walk(v, depth + 1)
        elif isinstance(obj, list):
            for x in obj[:80]:
                walk(x, depth + 1)

    # Chemins fréquents TikTok
    video = raw.get("video") if isinstance(raw.get("video"), dict) else {}
    for key in ("cla", "subtitleInfos", "subtitle_infos", "captionInfos"):
        if key in video:
            walk(video.get(key))
        if key in raw:
            walk(raw.get(key))
    walk(raw.get("stickersOnItem"))

    # Déduplique en gardant l'ordre
    seen: set[str] = set()
    out: list[str] = []
    for c in chunks:
        key = c.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return "\n".join(out).strip()


def _download_one(url: str, out_path: Path) -> Path | None:
    """Télécharge une vidéo TikTok en meilleure qualité via yt-dlp."""
    try:
        import yt_dlp
    except ImportError as e:
        raise RuntimeError("yt-dlp manquant — pip install yt-dlp") from e

    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Sans extension : yt-dlp ajoute .mp4
    tmpl = str(out_path.with_suffix("")) + ".%(ext)s"
    opts = {
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "outtmpl": tmpl,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 3,
        "socket_timeout": 30,
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

    # Trouver le fichier produit
    candidates = list(out_path.parent.glob(out_path.stem + ".*"))
    candidates = [p for p in candidates if p.suffix.lower() in (".mp4", ".webm", ".mkv", ".mov")]
    if candidates:
        # Renommer en .mp4 si possible
        best = max(candidates, key=lambda p: p.stat().st_size)
        if best.suffix.lower() != ".mp4":
            target = out_path.with_suffix(".mp4")
            try:
                best.replace(target)
                return target
            except Exception:
                return best
        return best
    if out_path.with_suffix(".mp4").exists():
        return out_path.with_suffix(".mp4")
    return None


def _whisper_transcribe(video_path: Path) -> str:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return ""
    try:
        from openai import OpenAI
    except ImportError as e:
        raise RuntimeError("openai manquant — pip install openai") from e

    client = OpenAI(api_key=api_key)
    # Whisper accepte mp4 ; limite ~25 Mo — compresser audio si trop gros
    path = video_path
    size_mb = path.stat().st_size / (1024 * 1024)
    audio_tmp: Path | None = None
    if size_mb > 24:
        audio_tmp = path.with_suffix(".whisper.mp3")
        import subprocess

        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(path),
                "-vn",
                "-acodec",
                "libmp3lame",
                "-q:a",
                "5",
                str(audio_tmp),
            ],
            check=True,
            capture_output=True,
        )
        path = audio_tmp

    try:
        with path.open("rb") as f:
            result = client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                # Auto-detect langue (souvent FR)
                response_format="text",
            )
        text = result if isinstance(result, str) else getattr(result, "text", "") or str(result)
        return str(text).strip()
    finally:
        if audio_tmp and audio_tmp.exists():
            try:
                audio_tmp.unlink()
            except Exception:
                pass


def run_archive(
    profile: str,
    *,
    max_videos: int = 20,
    transcribe: bool = True,
    headless: bool = True,
    on_profile: ProfileCb | None = None,
    on_progress: ProgressCb | None = None,
) -> dict[str, Any]:
    """
    1) Liste les posts du compte
    2) Télécharge chaque vidéo (meilleure qualité dispo)
    3) Extrait le texte (captions TikTok gratuits, sinon Whisper ~0,006 $/min)
    """
    handle = extract_handle(profile)
    light = os.getenv("SCRAPE_LIGHT", "1").strip() not in ("0", "false", "False")
    allowed = {10, 20, 50, 100}
    if max_videos not in allowed:
        max_videos = 20
    if light and max_videos > 20:
        max_videos = 20

    _progress(on_progress, f"Collecte des posts (cible {max_videos})…")
    handle, posts, profile_data = fetch_user_posts(
        profile,
        max_items=max_videos,
        headless=headless,
        on_profile=on_profile,
        on_progress=on_progress,
    )

    out_dir = archive_dir(handle)
    items_out: list[dict[str, Any]] = []
    whisper_ok = bool(os.getenv("OPENAI_API_KEY", "").strip())
    if transcribe and not whisper_ok:
        _progress(
            on_progress,
            "Pas de OPENAI_API_KEY — captions TikTok seulement (Whisper désactivé).",
        )

    total = len(posts)
    for i, post in enumerate(posts, start=1):
        vid = str(post.get("id") or f"idx{i}")
        url = post.get("url") or f"https://www.tiktok.com/@{handle}/video/{vid}"
        caption = (post.get("caption") or "").strip()
        _progress(on_progress, f"Téléchargement {i}/{total}…")

        video_path = out_dir / f"{vid}.mp4"
        transcript_path = out_dir / f"{vid}.txt"
        meta_path = out_dir / f"{vid}.json"

        downloaded: Path | None = None
        err = ""
        try:
            if video_path.exists() and video_path.stat().st_size > 10_000:
                downloaded = video_path
            else:
                downloaded = _download_one(url, video_path)
        except Exception as e:
            err = str(e)
            downloaded = None

        transcript = ""
        source = ""
        free_caps = (post.get("spoken_hints") or "").strip()

        # Whisper = parole réelle (idéal). Sinon captions TikTok gratuites, sinon description.
        if transcribe and whisper_ok and downloaded:
            try:
                _progress(on_progress, f"Transcription IA {i}/{total}…")
                transcript = _whisper_transcribe(downloaded)
                source = "whisper" if transcript else ""
            except Exception as e:
                if not err:
                    err = f"whisper: {e}"

        if not transcript and free_caps:
            transcript = free_caps
            source = "tiktok_captions"
        if not transcript and caption:
            transcript = caption
            source = source or "description"

        if transcript:
            transcript_path.write_text(transcript, encoding="utf-8")

        file_name = downloaded.name if downloaded else ""
        meta = {
            "id": vid,
            "url": url,
            "caption": caption,
            "author": post.get("author") or handle,
            "music": post.get("music") or "",
            "cover": post.get("cover") or "",
            "plays": post.get("plays") or 0,
            "likes": post.get("likes") or 0,
            "create_time": post.get("create_time") or 0,
            "file": file_name,
            "transcript": transcript,
            "transcript_source": source,
            "error": err,
        }
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        items_out.append(meta)

    # Manifest
    manifest = {
        "handle": handle,
        "profile": profile_data,
        "requested": max_videos,
        "downloaded": sum(1 for x in items_out if x.get("file")),
        "transcribed": sum(1 for x in items_out if x.get("transcript")),
        "whisper_enabled": whisper_ok and transcribe,
        "items": items_out,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # ZIP des mp4 + txt
    zip_path = out_dir / f"{_safe_handle(handle)}_archive.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in out_dir.iterdir():
            if p.suffix.lower() in (".mp4", ".txt", ".json", ".webm", ".mkv") and p.name != zip_path.name:
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
    # filename only — pas de path traversal
    name = Path(filename).name
    if not name or name in (".", ".."):
        return None
    path = ARCHIVES / safe / name
    if path.is_file() and path.resolve().is_relative_to((ARCHIVES / safe).resolve()):
        return path
    return None
