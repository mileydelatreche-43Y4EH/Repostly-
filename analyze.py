"""Analyse posts + reposts TikTok avec Claude (sans conseils DM)."""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from typing import Any

import httpx

CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

# Faux positifs fréquents (watermark, outil, bruit TikTok)
_NOISE_INTEREST = re.compile(
    r"capcut|cap\s*cut|édition\s*vid[eé]o|video\s*edit|inshot|"
    r"vn\b|premiere|after\s*effects|tiktok\s*shop|original\s*sound|"
    r"son\s*original|watermark|template",
    re.I,
)

SYSTEM = (
    "Tu analyses le contenu public d'un profil TikTok via métadonnées "
    "(captions, hashtags, sons, créateurs) — posts et/ou reposts. "
    "Tu n'as PAS vu les vidéos. Portrait concret, basé UNIQUEMENT sur des signaux répétés. "
    "Pas de conseils DM. "
    "Si posts absents, n'en parle pas. "
    "RÈGLES STRICTES :\n"
    "- interests : 3 à 5 max, goûts de vie réels (foi, études, voyages, beauté…). "
    "JAMAIS CapCut, édition vidéo, templates, outils TikTok, 'contenu viral' générique.\n"
    "- personality / topics / keywords : 3 à 5 max chacun.\n"
    "- categories : sections optionnelles UNIQUEMENT si signaux forts et répétés "
    "(artiste / thème cité plusieurs fois). Exemple : "
    '{"title":"Goûts musicaux","items":["Jul","rap FR"]}. '
    "Si rien de solide, omets categories ou []. "
    "Ne crée PAS une catégorie pour un artiste mentionné une seule fois.\n"
    "- COMPTEURS : utilise UNIQUEMENT les totaux fournis dans le prompt "
    "(total profil vs nombre analysé). N'invente jamais un chiffre de posts/reposts. "
    "Si tu cites un volume, dis par ex. « X reposts au total, Y analysés ». "
    "Ne confonds jamais le nombre analysé avec le total du profil.\n"
    "JSON uniquement :\n"
    "{\n"
    '  "summary": "3-5 phrases",\n'
    '  "vibe": "ambiance courte",\n'
    '  "personality": ["traits"],\n'
    '  "interests": ["intérêts forts seulement"],\n'
    '  "topics": ["thèmes"],\n'
    '  "content_patterns": ["patterns"],\n'
    '  "creator_affinity": ["créateurs"],\n'
    '  "categories": [{"title":"Goûts musicaux","items":["…"]}],\n'
    '  "tone": "ton",\n'
    '  "keywords": ["mots-clés"],\n'
    '  "own_content_style": "si posts fournis seulement",\n'
    '  "confidence": "low|medium|high"\n'
    "}"
)


def _anthropic_key() -> str:
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY manquante dans .env")
    return key


def _artist_from_music(music: str) -> str:
    m = (music or "").strip()
    if not m:
        return ""
    for sep in (" — ", " – ", " - ", " | "):
        if sep in m:
            parts = [p.strip() for p in m.split(sep) if p.strip()]
            if len(parts) >= 2:
                return parts[-1][:60]
    return ""


def detect_categories(
    items: list[dict[str, Any]],
    *,
    min_hits: int = 3,
) -> list[dict[str, Any]]:
    """
    Catégories déterministes : un artiste / thème n'apparaît que s'il revient
    assez souvent (sons + captions + hashtags).
    """
    if not items:
        return []

    # Seuil adaptatif : petits échantillons → 2, sinon 3
    n = len(items)
    threshold = 2 if n < 40 else min_hits

    artist_hits: Counter[str] = Counter()
    artist_display: dict[str, str] = {}
    theme_hits: Counter[str] = Counter()

    theme_patterns: list[tuple[str, re.Pattern[str]]] = [
        ("Spiritualité / foi", re.compile(r"\b(allah|islam|muslim|musulman|coran|hijab|ramadan|salat|deen)\b", re.I)),
        ("Voyages", re.compile(r"\b(travel|voyage|plage|beach|vacances|italia|italy|paris|dubai)\b", re.I)),
        ("Beauté / maquillage", re.compile(r"\b(makeup|make\s*up|maquillage|beauty|skincare|glow)\b", re.I)),
        ("Études", re.compile(r"\b(étudiant|etudiant|université|university|exam|cours|fac\b|school)\b", re.I)),
        ("Sport / fitness", re.compile(r"\b(gym|fitness|sport|workout|course|running)\b", re.I)),
        ("Mode", re.compile(r"\b(outfit|ootd|fashion|mode|style)\b", re.I)),
    ]

    for it in items:
        music = str(it.get("music") or "")
        caption = str(it.get("caption") or "")
        tags = " ".join(str(t) for t in (it.get("hashtags") or []))
        blob = f"{caption} {tags}"

        artist = _artist_from_music(music)
        if artist and len(artist) > 1:
            key = artist.lower()
            if not re.search(r"original\s*sound|son\s*original|capcut|tiktok", key, re.I):
                artist_hits[key] += 1
                artist_display.setdefault(key, artist)

        for theme_label, pat in theme_patterns:
            if pat.search(f"{blob} {music}"):
                theme_hits[theme_label] += 1

    # Mentions texte (caption / hashtag) des artistes déjà vus dans les sons
    for it in items:
        caption = str(it.get("caption") or "")
        tags = " ".join(str(t) for t in (it.get("hashtags") or []))
        blob = f"{caption} {tags}"
        for key in artist_display:
            if re.search(rf"\b{re.escape(key)}\b", blob, re.I):
                artist_hits[key] += 1

    categories: list[dict[str, Any]] = []

    music_items = [
        f"{artist_display[k]} (×{c})"
        for k, c in artist_hits.most_common(12)
        if c >= threshold
    ]
    if music_items:
        categories.append({"title": "Goûts musicaux", "items": music_items})

    theme_items = [
        f"{label} (×{c})"
        for label, c in theme_hits.most_common(8)
        if c >= threshold
    ]
    if theme_items:
        categories.append({"title": "Thèmes récurrents", "items": theme_items})

    return categories


def aggregate_signals(items: list[dict[str, Any]]) -> dict[str, Any]:
    tags: Counter[str] = Counter()
    authors: Counter[str] = Counter()
    musics: Counter[str] = Counter()
    captions: list[str] = []
    total_likes = 0
    total_plays = 0

    for it in items:
        for t in it.get("hashtags") or []:
            if t:
                tags[str(t).lower()] += 1
        a = (it.get("author") or "").strip()
        if a:
            authors[a.lower()] += 1
        m = (it.get("music") or "").strip()
        if m:
            musics[m.lower()[:80]] += 1
        cap = (it.get("caption") or "").strip()
        if cap:
            captions.append(cap[:300])
        total_likes += int(it.get("likes") or 0)
        total_plays += int(it.get("plays") or 0)

    n = len(items) or 1
    return {
        "count": len(items),
        "top_hashtags": tags.most_common(30),
        "top_authors": authors.most_common(20),
        "top_music": musics.most_common(15),
        "sample_captions": captions[:100],
        "avg_likes": int(total_likes / n) if items else 0,
        "avg_plays": int(total_plays / n) if items else 0,
    }


def _section(title: str, items: list[dict[str, Any]]) -> list[str]:
    if not items:
        return []
    sig = aggregate_signals(items)
    lines = [title, f"Nombre : {sig['count']}"]
    lines.append("Top hashtags :")
    for tag, n in sig["top_hashtags"][:20]:
        lines.append(f"  #{tag} ({n})")
    lines.append("Créateurs / auteurs :")
    for author, n in sig["top_authors"][:15]:
        lines.append(f"  @{author} ({n})")
    lines.append("Sons :")
    for music, n in sig["top_music"][:10]:
        lines.append(f"  {music} ({n})")
    lines.append("Captions :")
    for i, cap in enumerate(sig["sample_captions"][:50], 1):
        lines.append(f"  {i}. {cap}")
    return lines


def _clean_list(items: Any, *, limit: int = 5) -> list[str]:
    if not isinstance(items, list):
        return []
    out: list[str] = []
    for x in items:
        s = str(x).strip()
        if not s or _NOISE_INTEREST.search(s):
            continue
        out.append(s)
        if len(out) >= limit:
            break
    return out


def _merge_categories(
    computed: list[dict[str, Any]],
    from_llm: Any,
) -> list[dict[str, Any]]:
    """Priorité aux catégories calculées (seuils durs) ; LLM peut en ajouter d'autres."""
    by_title: dict[str, dict[str, Any]] = {}
    for cat in computed:
        title = str(cat.get("title") or "").strip()
        items = [str(x).strip() for x in (cat.get("items") or []) if str(x).strip()]
        if title and items:
            by_title[title.lower()] = {"title": title, "items": items}

    if isinstance(from_llm, list):
        for cat in from_llm:
            if not isinstance(cat, dict):
                continue
            title = str(cat.get("title") or "").strip()
            raw_items = cat.get("items") or []
            items = [
                str(x).strip()
                for x in raw_items
                if str(x).strip() and not _NOISE_INTEREST.search(str(x))
            ][:8]
            if not title or not items:
                continue
            key = title.lower()
            # Ne pas écraser Goûts musicaux / Thèmes calculés
            if key in by_title:
                continue
            # Catégories LLM : au moins 2 items pour éviter le bruit
            if len(items) < 2 and "music" not in key and "goût" not in key and "gout" not in key:
                continue
            by_title[key] = {"title": title, "items": items}

    # Ordre préféré
    preferred = ["goûts musicaux", "gouts musicaux", "thèmes récurrents", "themes recurrents"]
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for p in preferred:
        if p in by_title:
            ordered.append(by_title[p])
            seen.add(p)
    for k, v in by_title.items():
        if k not in seen:
            ordered.append(v)
    return ordered


async def analyze_profile(
    handle: str,
    posts: list[dict[str, Any]],
    reposts: list[dict[str, Any]],
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    all_items = reposts + posts
    computed_cats = detect_categories(all_items, min_hits=3)

    lines = [f"Profil TikTok : @{handle}"]
    if profile:
        if profile.get("nickname"):
            lines.append(f"Nom : {profile['nickname']}")
        if profile.get("bio"):
            lines.append(f"Bio : {profile['bio']}")
        if profile.get("followers"):
            lines.append(f"Followers : {profile['followers']}")
        if profile.get("repost_count"):
            lines.append(f"Total reposts sur le profil TikTok : {profile['repost_count']}")
        if profile.get("reposts_scraped"):
            lines.append(
                f"Reposts réellement analysés ici : {profile['reposts_scraped']} "
                f"(les plus récents, quota demandé {profile.get('reposts_requested') or '?'})"
            )
        if profile.get("video_count"):
            lines.append(f"Total vidéos/posts sur le profil : {profile['video_count']}")
        if profile.get("posts_scraped"):
            lines.append(f"Posts réellement analysés ici : {profile['posts_scraped']}")
        if profile.get("repost_total_unknown"):
            lines.append(
                "Total reposts du profil INCONNU — ne cite aucun total inventé, "
                "parle seulement du nombre analysé."
            )

    if computed_cats:
        lines.append("\n=== SIGNAUX RÉCURRENTS (déjà comptés — respecte ces seuils) ===")
        for cat in computed_cats:
            lines.append(f"{cat['title']} :")
            for item in cat["items"]:
                lines.append(f"  - {item}")
        lines.append(
            "Utilise ces signaux pour categories. "
            "N'invente pas d'artiste absent de cette liste."
        )

    if posts:
        lines += [""] + _section("=== POSTS PERSONNELS ===", posts)
    if reposts:
        lines += [""] + _section("=== REPOSTS ===", reposts)

    lines.append(
        "\nJSON demandé. Intérêts courts (3-5). Pas CapCut. "
        "Categories seulement si preuves répétées."
    )
    user = "\n".join(lines)

    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": 1600,
        "temperature": 0.25,
        "system": SYSTEM,
        "messages": [{"role": "user", "content": user}],
    }
    headers = {
        "x-api-key": _anthropic_key(),
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=90.0) as client:
        r = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers,
            json=payload,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"Erreur Claude ({r.status_code}) : {r.text[:300]}")
        text = ""
        for block in r.json().get("content", []):
            if block.get("type") == "text":
                text = block.get("text", "")
                break

    analysis = _parse_json(text)
    analysis.pop("hooks_for_dm", None)
    analysis.pop("avoid", None)
    analysis.pop("dm_tips", None)
    analysis.pop("music_taste", None)  # remplacé par categories
    if not posts:
        analysis.pop("own_content_style", None)

    analysis["interests"] = _clean_list(analysis.get("interests"), limit=5)
    analysis["personality"] = _clean_list(analysis.get("personality"), limit=5)
    analysis["topics"] = _clean_list(analysis.get("topics"), limit=5)
    analysis["keywords"] = _clean_list(analysis.get("keywords"), limit=6)
    analysis["content_patterns"] = _clean_list(analysis.get("content_patterns"), limit=5)
    analysis["creator_affinity"] = _clean_list(analysis.get("creator_affinity"), limit=6)
    analysis["categories"] = _merge_categories(computed_cats, analysis.get("categories"))

    combined = aggregate_signals(all_items)
    post_sig = aggregate_signals(posts) if posts else None

    analysis["_meta"] = {
        "reposts_analyzed": len(reposts),
        "posts_analyzed": len(posts),
        "top_hashtags": [
            {"tag": t, "count": n} for t, n in combined["top_hashtags"][:15]
        ],
        "top_authors": [
            {"author": a, "count": n} for a, n in combined["top_authors"][:12]
        ],
        "top_music": [
            {"music": m, "count": n} for m, n in combined["top_music"][:10]
        ],
        "categories_computed": computed_cats,
    }
    if post_sig:
        analysis["_meta"]["posts_top_hashtags"] = [
            {"tag": t, "count": n} for t, n in post_sig["top_hashtags"][:10]
        ]
    return analysis


def _parse_json(raw: str) -> dict[str, Any]:
    text = raw.strip()
    start = text.find("{")
    end = text.rfind("}") + 1
    if start < 0 or end <= start:
        raise RuntimeError("Claude n'a pas renvoyé de JSON exploitable.")
    try:
        data = json.loads(text[start:end])
    except json.JSONDecodeError as e:
        cleaned = re.sub(r",\s*}", "}", text[start:end])
        cleaned = re.sub(r",\s*]", "]", cleaned)
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as e2:
            raise RuntimeError(f"JSON Claude invalide : {e2}") from e
    if not isinstance(data, dict):
        raise RuntimeError("Réponse Claude invalide.")
    return data
