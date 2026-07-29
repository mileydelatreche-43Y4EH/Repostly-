"""Télécharge toutes les vidéos d'un compte + paroles (sous-titres TikTok)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from tiktok_local import extract_handle, fetch_profile_quick, fetch_user_posts

ROOT = Path(__file__).resolve().parent
ARCHIVES = ROOT / "data" / "archives"

ProgressCb = Callable[[str], None]
ProfileCb = Callable[[dict], None]

# 0 = toutes les vidéos du compte
ALLOWED_MAX = (0, 10, 20, 50, 100, 250, 500, 1000, 2000)
KEYWORD = "cheaterbuster"

# Priorité H.264 (avc1) — HEVC/AV1 = écran noir / image figée dans Chrome
YTDLP_FORMAT = (
    "bv*[vcodec^=avc1][ext=mp4]+ba[ext=m4a]/"
    "bv*[vcodec^=avc1]+ba/"
    "b[ext=mp4]/"
    "bv*[ext=mp4]+ba/"
    "b"
)

_CONVERT_LOCKS: dict[str, threading.Lock] = {}
_CONVERT_LOCKS_GUARD = threading.Lock()


def _is_light() -> bool:
    return os.getenv("SCRAPE_LIGHT", "0").strip() not in ("0", "false", "False")


def _safe_handle(handle: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", handle)[:64]


def archive_dir(handle: str) -> Path:
    d = ARCHIVES / _safe_handle(handle)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_manifest(out_dir: Path) -> dict[str, Any] | None:
    path = out_dir / "manifest.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _existing_items_map(manifest: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if not manifest:
        return out
    for it in manifest.get("items") or []:
        if not isinstance(it, dict):
            continue
        vid = str(it.get("id") or "").strip()
        if vid:
            out[vid] = dict(it)
    return out


def _compute_list_target(max_videos: int, existing_count: int) -> int:
    """Cible cumulative : 100 puis 500 → 500 ; 100 puis 100 → 200."""
    if max_videos == 0:
        return 0
    if existing_count <= 0:
        return max_videos
    if max_videos > existing_count:
        return max_videos
    return existing_count + max_videos


def _flush_manifest_light(
    out_dir: Path,
    handle: str,
    profile_data: dict[str, Any],
    items_out: list[dict[str, Any]],
    *,
    max_videos: int,
    list_target: int,
    light: bool,
    resumed: bool = False,
    added: int = 0,
    complete: bool = False,
    skipped: bool = False,
    partial: bool = False,
) -> dict[str, Any]:
    """Écrit manifest.json sans ZIP (rapide, pour sauver le progrès en cours)."""
    sorted_items = sorted(items_out, key=lambda x: (not x.get("has_keyword"),))
    kw_count = sum(1 for x in sorted_items if x.get("has_keyword"))
    manifest = {
        "handle": handle,
        "profile": profile_data,
        "requested": max_videos,
        "list_target": list_target,
        "found": len(sorted_items),
        "downloaded": sum(1 for x in sorted_items if x.get("file")),
        "transcribed": sum(1 for x in sorted_items if x.get("transcript")),
        "keyword": KEYWORD,
        "keyword_hits": kw_count,
        "local_mode": not light,
        "complete": complete,
        "resumed": resumed,
        "added": added,
        "skipped": skipped,
        "partial": partial,
        "items": sorted_items,
        "all_transcripts": "",
        "keyword_file": "",
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return manifest


def _build_item_meta(
    *,
    vid: str,
    url: str,
    post: dict[str, Any],
    handle: str,
    downloaded: Path | None,
    spoken: str,
    err: str,
    out_dir: Path,
) -> dict[str, Any]:
    caption = (post.get("caption") or "").strip()
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
        (out_dir / f"{vid}.txt").write_text(transcript, encoding="utf-8")

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
    (out_dir / f"{vid}.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return meta


def _write_manifest_bundle(
    out_dir: Path,
    handle: str,
    profile_data: dict[str, Any],
    items_out: list[dict[str, Any]],
    *,
    max_videos: int,
    list_target: int,
    light: bool,
    on_progress: ProgressCb | None,
    resumed: bool = False,
    added: int = 0,
    complete: bool = False,
    skipped: bool = False,
    partial: bool = False,
) -> dict[str, Any]:
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
        "list_target": list_target,
        "found": len(items_out),
        "downloaded": sum(1 for x in items_out if x.get("file")),
        "transcribed": sum(1 for x in items_out if x.get("transcript")),
        "keyword": KEYWORD,
        "keyword_hits": kw_count,
        "local_mode": not light,
        "complete": complete,
        "resumed": resumed,
        "added": added,
        "skipped": skipped,
        "partial": partial,
        "items": items_out,
        "all_transcripts": "all_transcripts.txt" if all_text else "",
        "keyword_file": "cheaterbuster_only.txt" if kw_only else "",
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if not skipped:
        _progress(on_progress, f"Création du ZIP… ({kw_count}× {KEYWORD})")
    zip_path = out_dir / f"{_safe_handle(handle)}_archive.zip"
    try:
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for p in out_dir.iterdir():
                if p.name == zip_path.name:
                    continue
                if p.suffix.lower() in (".mp4", ".txt", ".json", ".webm", ".mkv", ".vtt"):
                    if ".browser.mp4" in p.name.lower() or ".h264." in p.name.lower():
                        continue
                    zf.write(p, arcname=p.name)
        manifest["zip"] = zip_path.name
    except Exception:
        # ZIP optionnel : le progrès reste dans manifest.json
        pass

    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if skipped:
        _progress(on_progress, f"Déjà à jour — {len(items_out)} vidéos archivées.")
    elif partial:
        _progress(
            on_progress,
            f"Scan partiel sauvé — {len(items_out)} vidéo(s) disponibles.",
        )
    else:
        _progress(on_progress, "Archive prête")
    return manifest


def _progress(cb: ProgressCb | None, msg: str) -> None:
    if cb:
        try:
            cb(msg)
        except Exception:
            pass


def _ffprobe_vcodec(path: Path) -> str:
    try:
        r = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_name",
                "-of",
                "csv=p=0",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        return (r.stdout or "").strip().lower().split("\n")[0].strip()
    except Exception:
        return ""


def ensure_browser_mp4(path: Path) -> Path:
    """
    Garantit un MP4 H.264 lisible dans Chrome.
    Écrit un fichier voisin `.browser.mp4` (n'écrase pas l'original — souvent verrouillé
    par le navigateur / le serveur pendant la lecture).
    """
    if not path or not path.is_file() or path.suffix.lower() != ".mp4":
        return path
    name = path.name.lower()
    if ".browser.mp4" in name or ".h264." in name or ".hevc.bak" in name:
        return path

    key = str(path.resolve())
    with _CONVERT_LOCKS_GUARD:
        lock = _CONVERT_LOCKS.setdefault(key, threading.Lock())

    with lock:
        playable = path.with_name(f"{path.stem}.browser.mp4")
        if playable.is_file() and playable.stat().st_size > 5_000:
            pc = _ffprobe_vcodec(playable)
            if pc in ("h264", "avc1", "avc"):
                return playable

        codec = _ffprobe_vcodec(path)
        if codec in ("h264", "avc1", "avc"):
            return path
        if not codec:
            return path

        tmp = path.with_name(f"{path.stem}.h264.{os.getpid()}.mp4")
        try:
            proc = subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    str(path),
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-crf",
                    "23",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "128k",
                    "-movflags",
                    "+faststart",
                    "-pix_fmt",
                    "yuv420p",
                    str(tmp),
                ],
                capture_output=True,
                timeout=1200,
                check=False,
            )
            if proc.returncode != 0 or not tmp.is_file() or tmp.stat().st_size < 5_000:
                err = (proc.stderr or b"").decode("utf-8", "ignore")[-500:]
                print(f"[archive] ffmpeg fail {path.name}: {err}", flush=True)
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass
                return path

            try:
                if playable.exists():
                    playable.unlink()
                os.replace(tmp, playable)
                return playable
            except OSError as e:
                print(f"[archive] write browser mp4 fail {path.name}: {e}", flush=True)
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass
                return path
        except Exception as e:
            print(f"[archive] ensure_browser_mp4 {path.name}: {e}", flush=True)
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            return path


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
        "format": YTDLP_FORMAT,
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
        and ".corrupt" not in p.name.lower()
        and ".browser" not in p.name.lower()
        and ".h264." not in p.name.lower()
        and ".hevc.bak" not in p.name.lower()
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

    if downloaded:
        ensure_browser_mp4(downloaded)

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
    out_dir = archive_dir(handle)
    existing_manifest = _load_manifest(out_dir)
    existing_map = _existing_items_map(existing_manifest)
    existing_count = len(existing_map)
    prev_requested = existing_manifest.get("requested") if existing_manifest else None

    if existing_count > 0 and prev_requested == 0:
        items = list(existing_map.values())
        _progress(
            on_progress,
            f"Compte déjà entièrement archivé ({existing_count} vidéos) — ouverture…",
        )
        profile_data = dict(existing_manifest.get("profile") or {})
        profile_data.setdefault("handle", handle)
        return _write_manifest_bundle(
            out_dir,
            handle,
            profile_data,
            items,
            max_videos=max_videos,
            list_target=0,
            light=light,
            on_progress=on_progress,
            resumed=True,
            added=0,
            complete=True,
            skipped=True,
        )

    list_target = _compute_list_target(max_videos, existing_count)
    if existing_count > 0:
        tgt_label = "toutes" if list_target == 0 else str(list_target)
        _progress(
            on_progress,
            f"Reprise @{handle} — {existing_count} déjà archivées, cible {tgt_label}…",
        )
    else:
        _progress(on_progress, f"Profil @{handle}…")
    posts: list[dict[str, Any]] = []
    profile_data: dict[str, Any] = {"handle": handle, "nickname": handle}
    list_err = ""

    # Photo + bio tôt (Playwright rapide) — yt-dlp ne les fournit pas
    try:
        quick = fetch_profile_quick(profile, headless=headless)
        if isinstance(quick, dict):
            for k in (
                "nickname",
                "bio",
                "avatar",
                "avatar_url",
                "followers",
                "following",
                "likes",
                "video_count",
                "repost_count",
            ):
                if quick.get(k):
                    profile_data[k] = quick[k]
            if on_profile:
                try:
                    on_profile(dict(profile_data))
                except Exception:
                    pass
    except Exception as e:
        _progress(on_progress, f"Profil partiel… ({e})")

    list_label = "toutes" if list_target == 0 else str(list_target)
    _progress(on_progress, f"Liste des vidéos @{handle} (cible {list_label})…")

    try:
        posts, listed_profile = list_posts_ytdlp(handle, max_items=list_target)
        # garder bio/photo déjà lus ; compléter le reste
        if isinstance(listed_profile, dict):
            if listed_profile.get("nickname") and not profile_data.get("nickname"):
                profile_data["nickname"] = listed_profile["nickname"]
            if listed_profile.get("video_count"):
                profile_data["video_count"] = listed_profile["video_count"]
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
        fb_max = list_target if list_target > 0 else 100
        if fb_max not in (10, 20, 50, 100, 250, 500):
            fb_max = 100
        try:
            handle, posts, scraped_profile = fetch_user_posts(
                profile,
                max_items=fb_max,
                headless=headless,
                on_profile=on_profile,
                on_progress=on_progress,
            )
            if isinstance(scraped_profile, dict):
                for k, v in scraped_profile.items():
                    if v and not profile_data.get(k):
                        profile_data[k] = v
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

    new_posts = [p for p in posts if str(p.get("id") or "") not in existing_map]
    account_total = len(posts)
    if existing_count > 0 and not new_posts:
        listed_ids = {str(p.get("id") or "") for p in posts}
        items = list(existing_map.values())
        for vid, old in existing_map.items():
            if vid not in listed_ids:
                items.append(old)
        complete = max_videos == 0 or account_total <= existing_count
        _progress(
            on_progress,
            f"Déjà à jour — {existing_count} vidéos, rien de nouveau à ajouter.",
        )
        return _write_manifest_bundle(
            out_dir,
            handle,
            profile_data,
            items,
            max_videos=max_videos,
            list_target=list_target,
            light=light,
            on_progress=on_progress,
            resumed=True,
            added=0,
            complete=complete,
            skipped=True,
        )

    total = len(posts)
    to_fetch = len(new_posts)
    if existing_count > 0:
        _progress(
            on_progress,
            f"{to_fetch} nouvelle(s) vidéo(s) à récupérer ({existing_count} déjà là)…",
        )

    workers = 1 if light else min(3, max(1, to_fetch or 1))
    # Map cumulatif : existant + nouvelles (sauvé au fur et à mesure)
    live_map: dict[str, dict[str, Any]] = dict(existing_map)
    added_n = 0

    def _job(post: dict[str, Any], idx: int) -> tuple[str, Path | None, str, str, dict[str, Any]]:
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
        return vid, downloaded, spoken, err, post

    def _save_partial(force_zip: bool = False) -> dict[str, Any]:
        items = list(live_map.values())
        if force_zip:
            return _write_manifest_bundle(
                out_dir,
                handle,
                profile_data,
                items,
                max_videos=max_videos,
                list_target=list_target,
                light=light,
                on_progress=on_progress,
                resumed=existing_count > 0,
                added=added_n,
                complete=False,
                skipped=False,
                partial=True,
            )
        return _flush_manifest_light(
            out_dir,
            handle,
            profile_data,
            items,
            max_videos=max_videos,
            list_target=list_target,
            light=light,
            resumed=existing_count > 0,
            added=added_n,
            complete=False,
            skipped=False,
            partial=True,
        )

    try:
        if to_fetch > 0:
            _progress(on_progress, f"Téléchargement + paroles {to_fetch} vidéos…")
            # Sauve tout de suite le profil dans les récentes (même avant le 1er MP4)
            _flush_manifest_light(
                out_dir,
                handle,
                profile_data,
                list(live_map.values()),
                max_videos=max_videos,
                list_target=list_target,
                light=light,
                resumed=existing_count > 0,
                added=0,
                complete=False,
                skipped=False,
                partial=True,
            )
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(_job, post, i): (i, post)
                    for i, post in enumerate(new_posts, start=1)
                }
                done_n = 0
                for fut in as_completed(futures):
                    try:
                        vid, downloaded, spoken, err, post = fut.result()
                    except Exception as e:
                        # Une vidéo a planté — on continue avec les autres
                        i, post = futures[fut]
                        vid = str(post.get("id") or f"idx{i}")
                        downloaded, spoken, err = None, "", str(e)
                    url = post.get("url") or f"https://www.tiktok.com/@{handle}/video/{vid}"
                    meta = _build_item_meta(
                        vid=vid,
                        url=url,
                        post=post,
                        handle=handle,
                        downloaded=downloaded,
                        spoken=spoken,
                        err=err,
                        out_dir=out_dir,
                    )
                    live_map[vid] = meta
                    if meta.get("file"):
                        added_n += 1
                    done_n += 1
                    _progress(on_progress, f"Téléchargement {done_n}/{to_fetch}…")
                    # Flush disque régulièrement (toutes les 3 vidéos + à la fin)
                    if done_n == to_fetch or done_n % 3 == 0:
                        _save_partial(force_zip=False)

        items_out: list[dict[str, Any]] = []
        listed_ids: set[str] = set()
        for i, post in enumerate(posts, start=1):
            vid = str(post.get("id") or f"idx{i}")
            listed_ids.add(vid)
            url = post.get("url") or f"https://www.tiktok.com/@{handle}/video/{vid}"
            caption = (post.get("caption") or "").strip()

            if vid in live_map:
                meta = dict(live_map[vid])
                meta["url"] = url
                meta["caption"] = caption or meta.get("caption") or ""
                meta["cover"] = post.get("cover") or meta.get("cover") or ""
                meta["plays"] = post.get("plays") or meta.get("plays") or 0
                meta["likes"] = post.get("likes") or meta.get("likes") or 0
                meta["create_time"] = post.get("create_time") or meta.get("create_time") or 0
                items_out.append(meta)
                continue

            # Non téléchargée (ne devrait pas arriver hors crash)
            items_out.append(
                {
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
                    "file": "",
                    "file_size": 0,
                    "transcript": caption,
                    "transcript_source": "description" if caption else "",
                    "has_keyword": bool(re.search(re.escape(KEYWORD), caption, re.I)),
                    "error": "manquant",
                }
            )

        for vid, old in existing_map.items():
            if vid not in listed_ids and vid not in {str(x.get("id")) for x in items_out}:
                items_out.append(old)

        complete = max_videos == 0 or (
            len([x for x in items_out if x.get("file")]) >= list_target
            if list_target > 0
            else account_total <= len(items_out)
        )
        return _write_manifest_bundle(
            out_dir,
            handle,
            profile_data,
            items_out,
            max_videos=max_videos,
            list_target=list_target,
            light=light,
            on_progress=on_progress,
            resumed=existing_count > 0,
            added=added_n,
            complete=complete,
            skipped=False,
            partial=False,
        )
    except Exception as e:
        # Crash / interruption : on conserve tout ce qui est déjà sur disque
        n = len(live_map)
        if n > 0:
            _progress(
                on_progress,
                f"Interrompu après {n} vidéo(s) — sauvegarde du progrès… ({e})",
            )
            return _save_partial(force_zip=True)
        raise


def list_archive_recents(limit: int = 50) -> list[dict[str, Any]]:
    """Liste les archives locales (manifest.json) pour reconstruire les recherches récentes."""
    if not ARCHIVES.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for d in ARCHIVES.iterdir():
        if not d.is_dir():
            continue
        path = d / "manifest.json"
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        handle = str(data.get("handle") or d.name).replace("@", "").lower()
        profile = data.get("profile") if isinstance(data.get("profile"), dict) else {}
        items = data.get("items") if isinstance(data.get("items"), list) else []
        try:
            mtime = int(path.stat().st_mtime * 1000)
        except Exception:
            mtime = 0
        avatar_url = str(profile.get("avatar_url") or "")
        raw_avatar = str(profile.get("avatar") or "")
        if not avatar_url and raw_avatar.startswith("http"):
            avatar_url = raw_avatar
        rows.append(
            {
                "id": f"archive:{handle}",
                "mode": "archive",
                "handle": handle,
                "nickname": str(profile.get("nickname") or handle),
                "avatar_url": avatar_url,
                "savedAt": mtime,
                "hasSnapshot": True,
                "downloaded": int(data.get("downloaded") or len(items) or 0),
                "keyword_hits": int(data.get("keyword_hits") or 0),
                "found": int(data.get("found") or len(items) or 0),
            }
        )
    rows.sort(key=lambda x: x.get("savedAt") or 0, reverse=True)
    return rows[: max(1, min(limit, 100))]


def get_archive_file(handle: str, filename: str) -> Path | None:
    safe = _safe_handle(extract_handle(handle) if "@" in handle or "/" in handle else handle)
    name = Path(filename).name
    if not name or name in (".", "..") or ".h264.tmp" in name:
        return None
    path = ARCHIVES / safe / name
    if path.is_file() and path.resolve().is_relative_to((ARCHIVES / safe).resolve()):
        return path
    return None
