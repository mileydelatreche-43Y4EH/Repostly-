"""Scrape local TikTok (profil + posts + reposts) via Playwright."""

from __future__ import annotations

import base64
import os
import re
import time
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

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


def _int(v: Any) -> int:
    try:
        s = str(v).strip().lower().replace(",", "").replace(" ", "")
        if s.endswith("k"):
            return int(float(s[:-1]) * 1000)
        if s.endswith("m"):
            return int(float(s[:-1]) * 1_000_000)
        return int(float(s))
    except (TypeError, ValueError):
        return 0


def _normalize_item(item: dict[str, Any], *, kind: str) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None

    raw = item.get("item") if isinstance(item.get("item"), dict) else item

    caption = (
        raw.get("desc")
        or raw.get("description")
        or raw.get("text")
        or raw.get("caption")
        or ""
    )
    caption = str(caption).strip()

    author = ""
    author_obj = raw.get("author") or raw.get("authorMeta") or {}
    if isinstance(author_obj, dict):
        author = (
            author_obj.get("uniqueId")
            or author_obj.get("unique_id")
            or author_obj.get("nickname")
            or author_obj.get("nickName")
            or author_obj.get("name")
            or ""
        )

    music = ""
    music_obj = raw.get("music") or raw.get("musicMeta") or {}
    if isinstance(music_obj, dict):
        title = music_obj.get("title") or music_obj.get("musicName") or music_obj.get("name") or ""
        artist = (
            music_obj.get("authorName")
            or music_obj.get("author")
            or music_obj.get("musicAuthor")
            or ""
        )
        music = f"{title} — {artist}".strip(" —") if artist else str(title)

    hashtags: list[str] = []
    for ch in raw.get("challenges") or raw.get("hashtags") or []:
        if isinstance(ch, dict):
            name = ch.get("title") or ch.get("name") or ""
            if name:
                hashtags.append(str(name).lstrip("#"))
        elif isinstance(ch, str):
            hashtags.append(ch.lstrip("#"))
    if not hashtags and caption:
        hashtags = [h.lstrip("#") for h in re.findall(r"#([\w\u00C0-\u024F]+)", caption)]

    stats = raw.get("stats") or raw.get("statsV2") or {}
    plays = 0
    likes = 0
    if isinstance(stats, dict):
        plays = stats.get("playCount") or stats.get("play_count") or 0
        likes = stats.get("diggCount") or stats.get("digg_count") or stats.get("likeCount") or 0

    cover = ""
    video = raw.get("video") or {}
    if isinstance(video, dict):
        cover = (
            video.get("cover")
            or video.get("originCover")
            or video.get("dynamicCover")
            or ""
        )
        if isinstance(cover, dict):
            cover = cover.get("urlList", [""])[0] if cover.get("urlList") else ""

    video_id = raw.get("id") or raw.get("aweme_id") or ""
    author_id = author or "video"
    url = f"https://www.tiktok.com/@{author_id}/video/{video_id}" if video_id else ""

    create_time = raw.get("createTime") or raw.get("create_time") or raw.get("createTimeISO") or 0
    try:
        create_time = int(create_time)
    except (TypeError, ValueError):
        create_time = 0

    if not caption and not hashtags and not music and not author and not cover:
        return None

    return {
        "kind": kind,
        "caption": caption[:500],
        "author": str(author).strip(),
        "music": str(music).strip()[:200],
        "hashtags": hashtags[:20],
        "url": url,
        "cover": str(cover).strip(),
        "plays": _int(plays),
        "likes": _int(likes),
        "id": str(video_id),
        "create_time": create_time,
    }


def _items_from_payload(payload: Any) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    for key in ("itemList", "item_list", "items", "aweme_list"):
        lst = payload.get(key)
        if isinstance(lst, list):
            return [x for x in lst if isinstance(x, dict)]
    return []


def _avatar_to_data_url(page, avatar_url: str) -> str:
    if not avatar_url:
        return ""
    if avatar_url.startswith("data:"):
        return avatar_url
    try:
        resp = page.request.get(avatar_url, timeout=15000)
        if resp.status != 200:
            return avatar_url
        body = resp.body()
        ctype = resp.headers.get("content-type", "image/jpeg").split(";")[0]
        if "image" not in ctype:
            ctype = "image/jpeg"
        b64 = base64.b64encode(body).decode("ascii")
        return f"data:{ctype};base64,{b64}"
    except Exception:
        return avatar_url


def _parse_count_token(raw: str) -> int:
    return _int(raw)


def _extract_profile_from_page(
    page, handle: str, *, encode_avatar: bool = False
) -> dict[str, Any]:
    data = page.evaluate(
        """() => {
          const out = {
            nickname: '', avatar: '', bio: '',
            followers: '', following: '', likes: '',
            videoCount: '', repostCount: ''
          };

          const takeNum = (v) => {
            if (v === null || v === undefined || v === '') return '';
            return String(v);
          };

          try {
            const script = document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__');
            if (script && script.textContent) {
              const json = JSON.parse(script.textContent);
              const scope = json?.__DEFAULT_SCOPE__ || {};
              let user = {};
              let stats = {};
              for (const val of Object.values(scope)) {
                const ui = val?.userInfo;
                if (ui?.user) {
                  user = ui.user;
                  stats = ui.stats || ui.statsV2 || {};
                  break;
                }
              }
              out.nickname = user.nickname || user.nickName || '';
              out.avatar = user.avatarLarger || user.avatarMedium || user.avatarThumb || '';
              out.bio = user.signature || '';
              out.followers = takeNum(stats.followerCount ?? stats.followerCount);
              out.following = takeNum(stats.followingCount);
              out.likes = takeNum(stats.heartCount ?? stats.heart);
              out.videoCount = takeNum(stats.videoCount ?? stats.video_count);
              out.repostCount = takeNum(
                stats.repostCount
                ?? stats.repost_count
                ?? stats.repostVideoCount
                ?? stats.repost_video_count
                ?? user.repostCount
                ?? user.repost_count
              );
            }
          } catch (e) {}

          // SIGI_STATE (ancien format TikTok)
          try {
            const sigi = document.getElementById('SIGI_STATE');
            if (sigi && sigi.textContent) {
              const json = JSON.parse(sigi.textContent);
              const users = json?.UserModule?.users || {};
              const statsMap = json?.UserModule?.stats || {};
              const u = Object.values(users).find((x) => x && x.uniqueId) || {};
              const st = statsMap[u.id] || statsMap[u.uid] || {};
              if (!out.nickname && u.nickname) out.nickname = u.nickname;
              if (!out.avatar && (u.avatarLarger || u.avatarMedium)) {
                out.avatar = u.avatarLarger || u.avatarMedium;
              }
              if (!out.bio && u.signature) out.bio = u.signature;
              if (!out.videoCount && st.videoCount != null) out.videoCount = takeNum(st.videoCount);
              if (!out.repostCount && (st.repostCount != null || st.repostVideoCount != null)) {
                out.repostCount = takeNum(st.repostCount ?? st.repostVideoCount);
              }
            }
          } catch (e) {}

          if (!out.avatar) {
            const imgs = [...document.querySelectorAll('img')];
            const hit = imgs.find((img) =>
              (img.src || '').includes('tiktokcdn') &&
              (img.width >= 80 || img.naturalWidth >= 80 || img.className.toLowerCase().includes('avatar'))
            );
            if (hit?.src) out.avatar = hit.src;
          }
          if (!out.nickname) {
            const h =
              document.querySelector('h1[data-e2e="user-title"]') ||
              document.querySelector('h2[data-e2e="user-subtitle"]');
            if (h?.textContent) out.nickname = h.textContent.trim();
          }
          if (!out.bio) {
            const bio = document.querySelector('[data-e2e="user-bio"]');
            if (bio?.textContent) out.bio = bio.textContent.trim();
          }

          // Compteurs onglets / labels visibles — priorité haute
          const tabNodes = [
            ...document.querySelectorAll('[role="tab"]'),
            ...document.querySelectorAll('[data-e2e*="user-"], [data-e2e*="tab"], [data-e2e*="repost"]'),
          ];
          for (const t of tabNodes) {
            const txt = (t.textContent || '').replace(/\\s+/g, ' ').trim();
            if (txt.length > 48) continue;
            const mVid = txt.match(/(\\d[\\d.,\\s]*[kKmM]?)\\s*(Videos|Vidéos|Posts)/i);
            const mRep = txt.match(/(\\d[\\d.,\\s]*[kKmM]?)\\s*(Reposts|Republiés|Republier|Repost)/i);
            if (mVid) out.videoCount = mVid[1].replace(/\\s/g, '');
            if (mRep) out.repostCount = mRep[1].replace(/\\s/g, '');
          }

          // Balayage large du texte page (ex. "847 Reposts")
          if (!out.repostCount) {
            const body = (document.body && document.body.innerText) || '';
            const m = body.match(/(\\d[\\d.,\\s]*[kKmM]?)\\s*(Reposts|Republiés)/i);
            if (m) out.repostCount = m[1].replace(/\\s/g, '');
          }
          if (!out.videoCount) {
            const body = (document.body && document.body.innerText) || '';
            const m = body.match(/(\\d[\\d.,\\s]*[kKmM]?)\\s*(Videos|Vidéos)/i);
            if (m) out.videoCount = m[1].replace(/\\s/g, '');
          }

          return out;
        }"""
    )
    if not isinstance(data, dict):
        data = {}

    avatar_raw = (data.get("avatar") or "").strip()
    # Pas de base64 par défaut (lent) — le front passe par /api/avatar
    encode = bool(encode_avatar)
    avatar_data = _avatar_to_data_url(page, avatar_raw) if (encode and avatar_raw) else ""

    return {
        "handle": handle,
        "nickname": (data.get("nickname") or handle or "").strip(),
        "avatar": avatar_data or "",
        "avatar_url": avatar_raw if avatar_raw.startswith("http") else "",
        "bio": (data.get("bio") or "").strip()[:300],
        "followers": str(data.get("followers") or "").strip(),
        "following": str(data.get("following") or "").strip(),
        "likes": str(data.get("likes") or "").strip(),
        "video_count": _parse_count_token(str(data.get("videoCount") or "")),
        "repost_count": _parse_count_token(str(data.get("repostCount") or "")),
    }


def _dismiss_cookies(page) -> None:
    for sel in (
        'button:has-text("Accept all")',
        'button:has-text("Allow all")',
        'button:has-text("Accept Cookies")',
        'button:has-text("Tout accepter")',
        'button:has-text("Accepter")',
        'button:has-text("Accept")',
        '[data-e2e="cookie-banner-accept"]',
    ):
        try:
            btn = page.locator(sel).first
            if btn.count() and btn.is_visible():
                btn.click(timeout=1500)
                page.wait_for_timeout(300)
                break
        except Exception:
            pass
    # Cookiebot / OneTrust souvent dans un iframe ou shadow
    try:
        page.evaluate(
            """() => {
              const roots = [document];
              const walk = (node) => {
                if (!node) return false;
                const btns = node.querySelectorAll
                  ? node.querySelectorAll('button, [role="button"]')
                  : [];
                for (const b of btns) {
                  const t = (b.textContent || '').toLowerCase();
                  if (
                    t.includes('accept all') ||
                    t.includes('allow all') ||
                    t.includes('tout accepter') ||
                    t.includes('accepter tout')
                  ) {
                    b.click();
                    return true;
                  }
                }
                return false;
              };
              if (walk(document)) return;
              document.querySelectorAll('*').forEach((el) => {
                if (el.shadowRoot) walk(el.shadowRoot);
              });
            }"""
        )
    except Exception:
        pass


def _page_looks_blocked(page) -> bool:
    try:
        return bool(
            page.evaluate(
                """() => {
                  const t = ((document.body && document.body.innerText) || '').toLowerCase();
                  const title = (document.title || '').toLowerCase();
                  if (title.includes('captcha') || t.includes('verify to continue')) return true;
                  if (t.includes('are you a human') || t.includes('unusual traffic')) return true;
                  if (document.querySelector('#captcha-verify-container, .captcha-verify-container')) return true;
                  return false;
                }"""
            )
        )
    except Exception:
        return False


def _click_tab(page, labels: tuple[str, ...]) -> bool:
    # data-e2e TikTok d'abord
    for e2e in (
        'a[data-e2e="user-repost"]',
        'p[data-e2e="user-repost"]',
        'div[data-e2e="user-repost"]',
        '[data-e2e="repost-tab"]',
        'a[data-e2e="user-post"]',
        'p[data-e2e="user-post"]',
    ):
        label_hint = labels[0].lower() if labels else ""
        if "repost" in e2e and "repost" not in label_hint and "repub" not in label_hint:
            continue
        if "post" in e2e and "repost" not in e2e:
            if not any(x.lower() in ("videos", "vidéos", "posts") for x in labels):
                continue
        try:
            tab = page.locator(e2e).first
            if tab.count():
                tab.click(timeout=2500)
                return True
        except Exception:
            continue

    for label in labels:
        for sel in (
            f'div[role="tab"]:has-text("{label}")',
            f'a[role="tab"]:has-text("{label}")',
            f'p[role="tab"]:has-text("{label}")',
            f'[data-e2e*="tab"]:has-text("{label}")',
            f'p:has-text("{label}")',
            f'span:has-text("{label}")',
            f'a:has-text("{label}")',
        ):
            try:
                tab = page.locator(sel).first
                if tab.count():
                    tab.click(timeout=2500)
                    return True
            except Exception:
                continue
    # Clic JS par texte exact/partiel sur les onglets
    try:
        hit = page.evaluate(
            """(labels) => {
              const nodes = [
                ...document.querySelectorAll('[role="tab"]'),
                ...document.querySelectorAll('[data-e2e*="tab"], [data-e2e*="repost"], [data-e2e*="post"]'),
                ...document.querySelectorAll('p, span, a, div'),
              ];
              const lower = labels.map((l) => l.toLowerCase());
              for (const n of nodes) {
                const txt = (n.textContent || '').replace(/\\s+/g, ' ').trim();
                if (txt.length > 40) continue;
                const tl = txt.toLowerCase();
                if (lower.some((l) => tl === l || tl.includes(l))) {
                  n.click();
                  return true;
                }
              }
              return false;
            }""",
            list(labels),
        )
        return bool(hit)
    except Exception:
        return False


def _api_url_matches(url: str, api_substr: str) -> bool:
    u = (url or "").lower()
    key = (api_substr or "").lower()
    if key in u:
        return True
    # Variantes TikTok
    if "repost" in key and ("repost/item_list" in u or "repost_item_list" in u):
        return True
    if "post/item_list" in key and "post/item_list" in u and "repost" not in u:
        return True
    return False


def _extract_embedded_items(page, *, kind: str) -> list[dict[str, Any]]:
    """Items déjà présents dans __UNIVERSAL_DATA__ / SIGI (sans XHR)."""
    raw_items = page.evaluate(
        """() => {
          const out = [];
          const pushList = (lst) => {
            if (!Array.isArray(lst)) return;
            for (const x of lst) if (x && typeof x === 'object') out.push(x);
          };
          try {
            const script = document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__');
            if (script && script.textContent) {
              const json = JSON.parse(script.textContent);
              const scope = json?.__DEFAULT_SCOPE__ || {};
              for (const val of Object.values(scope)) {
                pushList(val?.itemList);
                pushList(val?.items);
                pushList(val?.repostList);
                if (val?.userDetail?.itemList) pushList(val.userDetail.itemList);
                if (Array.isArray(val)) {
                  for (const inner of val) {
                    pushList(inner?.itemList);
                    pushList(inner?.items);
                  }
                }
              }
            }
          } catch (e) {}
          try {
            const sigi = document.getElementById('SIGI_STATE');
            if (sigi && sigi.textContent) {
              const json = JSON.parse(sigi.textContent);
              const im = json?.ItemModule;
              if (im && typeof im === 'object') {
                for (const v of Object.values(im)) if (v && typeof v === 'object') out.push(v);
              }
            }
          } catch (e) {}
          return out;
        }"""
    )
    collected: list[dict[str, Any]] = []
    seen: set[str] = set()
    if not isinstance(raw_items, list):
        return collected
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        norm = _normalize_item(raw, kind=kind)
        if not norm:
            continue
        vid = norm.get("id") or ""
        if vid and vid in seen:
            continue
        if vid:
            seen.add(vid)
        collected.append(norm)
    return collected


def _extract_dom_items(page, *, kind: str) -> list[dict[str, Any]]:
    """Fallback : liens vidéo visibles dans la grille."""
    rows = page.evaluate(
        """() => {
          const out = [];
          const seen = new Set();
          const anchors = [...document.querySelectorAll('a[href*="/video/"]')];
          for (const a of anchors) {
            const href = a.href || a.getAttribute('href') || '';
            const m = href.match(/@([^/]+)\\/video\\/(\\d+)/);
            if (!m) continue;
            const id = m[2];
            if (seen.has(id)) continue;
            seen.add(id);
            const img = a.querySelector('img');
            const caption =
              (a.getAttribute('aria-label') || '') ||
              (img && (img.getAttribute('alt') || '')) ||
              '';
            out.push({
              id,
              author: m[1],
              desc: caption.slice(0, 400),
              video: { cover: (img && img.src) || '' },
            });
          }
          return out;
        }"""
    )
    collected: list[dict[str, Any]] = []
    if not isinstance(rows, list):
        return collected
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        norm = _normalize_item(raw, kind=kind)
        if norm:
            collected.append(norm)
    return collected


def _with_count(url: str, count: int = 35) -> str:
    if "count=" in url:
        return re.sub(r"count=\d+", f"count={count}", url)
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}count={count}"


def _with_cursor(url: str, cursor: int | str) -> str:
    cursor = str(cursor)
    if "cursor=" in url:
        return re.sub(r"cursor=[^&]*", f"cursor={cursor}", url)
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}cursor={cursor}"


def _collect_from_api(
    page,
    *,
    api_substr: str,
    kind: str,
    max_items: int,
    seed_items: list[dict[str, Any]] | None = None,
    seed_url: str = "",
) -> list[dict[str, Any]]:
    """
    Collecte les max_items plus récents (ordre API + tri create_time).
    Continue jusqu'à atteindre le quota, ou jusqu'à épuisement (hasMore=false).
    """
    collected: list[dict[str, Any]] = []
    seen: set[str] = set()
    first_url = seed_url or ""
    last_cursor: int | str | None = None
    last_has_more = True

    if seed_items:
        for item in seed_items:
            if not isinstance(item, dict):
                continue
            vid = str(item.get("id") or "")
            if vid and vid in seen:
                continue
            if vid:
                seen.add(vid)
            collected.append(item)

    def ingest(payload: Any) -> int:
        nonlocal last_cursor, last_has_more
        if isinstance(payload, dict):
            if "cursor" in payload or "maxCursor" in payload:
                last_cursor = payload.get("cursor", payload.get("maxCursor"))
            if "hasMore" in payload or "has_more" in payload:
                last_has_more = bool(payload.get("hasMore") or payload.get("has_more"))
        added = 0
        for raw in _items_from_payload(payload):
            vid = str(raw.get("id") or raw.get("aweme_id") or "")
            if vid and vid in seen:
                continue
            if vid:
                seen.add(vid)
            norm = _normalize_item(raw, kind=kind)
            if norm:
                collected.append(norm)
                added += 1
        return added

    def on_response(response) -> None:
        nonlocal first_url
        url = response.url
        if not _api_url_matches(url, api_substr):
            return
        try:
            if response.status != 200:
                return
            data = response.json()
        except Exception:
            return
        if not first_url:
            first_url = url
        ingest(data)

    page.on("response", on_response)

    # Phase 1 — scroll (ne pas abandonner trop tôt)
    scroll_rounds = max(40, min(160, max_items + 30))
    stagnant = 0
    last_n = len(collected)
    for _ in range(scroll_rounds):
        if len(collected) >= max_items:
            break
        page.mouse.wheel(0, 4200)
        page.wait_for_timeout(550)
        if len(collected) == last_n:
            stagnant += 1
        else:
            stagnant = 0
            last_n = len(collected)
        # 12 scrolls sans nouveau = on passe à la pagination API
        if stagnant >= 12:
            break

    # Phase 2 — pagination cursor jusqu'au quota demandé
    if first_url and len(collected) < max_items:
        cursor = last_cursor if last_cursor is not None else 0
        page_loops = max(60, max_items // 5 + 40)
        empty_streak = 0
        for i in range(page_loops):
            if len(collected) >= max_items:
                break
            next_url = _with_count(_with_cursor(first_url, cursor), 35)
            result = page.evaluate(
                """async (url) => {
                    try {
                      const r = await fetch(url, { credentials: 'include' });
                      if (!r.ok) return { ok: false, status: r.status };
                      return { ok: true, data: await r.json() };
                    } catch (e) {
                      return { ok: false, error: String(e) };
                    }
                }""",
                next_url,
            )
            if not result or not result.get("ok"):
                empty_streak += 1
                if empty_streak >= 5:
                    break
                page.mouse.wheel(0, 2400)
                page.wait_for_timeout(700)
                continue

            data = result.get("data") or {}
            before = len(collected)
            added = ingest(data)
            # Toujours prendre le cursor renvoyé par TikTok
            if isinstance(data, dict):
                if data.get("cursor") is not None:
                    last_cursor = data.get("cursor")
                elif data.get("maxCursor") is not None:
                    last_cursor = data.get("maxCursor")
            cursor = last_cursor if last_cursor is not None else cursor
            try:
                cursor = int(cursor) if cursor is not None else 0
            except (TypeError, ValueError):
                pass

            if added == 0:
                empty_streak += 1
            else:
                empty_streak = 0

            # hasMore=false : on arrête seulement si vraiment plus rien de nouveau
            has_more = bool(data.get("hasMore") or data.get("has_more"))
            if last_has_more is False or has_more is False:
                if added == 0:
                    break
            if empty_streak >= 5:
                break

            # Garder la session TikTok active
            if i % 2 == 0:
                page.mouse.wheel(0, 1800)
                page.wait_for_timeout(350)
            else:
                time.sleep(0.15)

            # Sécurité : si on n'avance plus du tout
            if len(collected) == before and empty_streak >= 3:
                page.wait_for_timeout(800)

    try:
        page.remove_listener("response", on_response)
    except Exception:
        pass

    collected.sort(key=lambda x: int(x.get("create_time") or 0), reverse=True)
    return collected[:max_items]


def _browser_context(headless: bool):
    light = os.getenv("SCRAPE_LIGHT", "1").strip() not in ("0", "false", "False")
    args = [
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--no-sandbox",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-default-apps",
        "--disable-sync",
        "--no-first-run",
        "--mute-audio",
        "--hide-scrollbars",
        "--disable-software-rasterizer",
        "--disable-features=IsolateOrigins,site-per-process",
    ]
    # Pas de --single-process : ça freeze souvent Chromium sur Render

    p = sync_playwright().start()
    browser = p.chromium.launch(headless=headless, args=args)
    context = browser.new_context(
        viewport={"width": 900, "height": 720} if light else {"width": 1280, "height": 860},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        locale="fr-FR",
        timezone_id="Europe/Paris",
        java_script_enabled=True,
        color_scheme="dark",
        extra_http_headers={
            "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
            "Sec-Ch-Ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
        },
    )
    # Masquer navigator.webdriver (sinon TikTok coupe souvent les XHR)
    context.add_init_script(
        """
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        window.chrome = window.chrome || { runtime: {} };
        Object.defineProperty(navigator, 'languages', { get: () => ['fr-FR', 'fr', 'en-US', 'en'] });
        Object.defineProperty(navigator, 'plugins', {
          get: () => [1, 2, 3, 4, 5],
        });
        const originalQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) =>
          parameters.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : originalQuery(parameters);
        """
    )
    return p, browser, context


def fetch_profile_quick(
    profile_url: str,
    *,
    headless: bool = True,
) -> dict[str, Any]:
    """Charge uniquement le profil (photo via URL CDN + compteurs)."""
    handle = extract_handle(profile_url)
    url = f"https://www.tiktok.com/@{handle}"
    p, browser, context = _browser_context(headless)
    try:
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(1200)
        _dismiss_cookies(page)
        page.wait_for_timeout(400)
        return _extract_profile_from_page(page, handle, encode_avatar=False)
    finally:
        context.close()
        browser.close()
        p.stop()


def fetch_profile_content(
    profile_url: str,
    *,
    max_items: int = 100,
    headless: bool = True,
    on_profile=None,
    on_progress=None,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """
    Profil + reposts (+ posts limités).
    on_profile(profile) appelé dès que la bio/photo est lue (même session navigateur).
    """
    handle = extract_handle(profile_url)
    if max_items not in (100, 500, 1000):
        max_items = 100

    light = os.getenv("SCRAPE_LIGHT", "1").strip() not in ("0", "false", "False")
    # Sur Free Render, plafonner pour éviter OOM
    if light and max_items > 100:
        max_items = 100

    base = f"https://www.tiktok.com/@{handle}"
    max_posts = 0 if light else min(40, max_items)

    def progress(msg: str) -> None:
        if on_progress:
            try:
                on_progress(msg)
            except Exception:
                pass

    p, browser, context = _browser_context(headless)
    try:
        page = context.new_page()
        progress("Ouverture du profil…")
        page.goto(base, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(1500)
        _dismiss_cookies(page)
        page.wait_for_timeout(500)

        if _page_looks_blocked(page):
            progress("Challenge TikTok détecté — nouvel essai…")
            page.wait_for_timeout(2000)
            page.reload(wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(1500)
            _dismiss_cookies(page)

        profile = _extract_profile_from_page(page, handle, encode_avatar=True)
        if on_profile:
            try:
                on_profile(dict(profile))
            except Exception:
                pass

        api_total = {"repost": 0, "post": 0}
        early_reposts: list[dict[str, Any]] = []
        early_seen: set[str] = set()
        early_repost_url = ""

        def _capture_early(response) -> None:
            nonlocal early_repost_url
            url = response.url
            try:
                if response.status != 200:
                    return
                if _api_url_matches(url, "/api/repost/item_list"):
                    if not early_repost_url:
                        early_repost_url = url
                    data = response.json()
                    tot = data.get("total") or data.get("totalCount") or data.get("total_count")
                    if tot:
                        api_total["repost"] = max(api_total["repost"], _int(tot))
                    for raw in _items_from_payload(data):
                        norm = _normalize_item(raw, kind="repost")
                        if not norm:
                            continue
                        vid = norm.get("id") or ""
                        if vid and vid in early_seen:
                            continue
                        if vid:
                            early_seen.add(vid)
                        early_reposts.append(norm)
                if _api_url_matches(url, "/api/post/item_list"):
                    data = response.json()
                    tot = data.get("total") or data.get("totalCount") or data.get("total_count")
                    if tot:
                        api_total["post"] = max(api_total["post"], _int(tot))
            except Exception:
                pass

        # Écouter AVANT le clic onglet (sinon on rate le 1er batch XHR)
        page.on("response", _capture_early)

        progress("Ouverture de l'onglet Reposts…")
        opened = _click_tab(page, ("Reposts", "Republiés", "Republier", "Repost"))
        if not opened:
            page.goto(f"{base}?tab=repost", wait_until="domcontentloaded", timeout=45000)
        else:
            # Forcer aussi l'URL tab=repost (TikTok charge parfois mieux)
            try:
                if "tab=repost" not in (page.url or ""):
                    page.goto(f"{base}?tab=repost", wait_until="domcontentloaded", timeout=45000)
            except Exception:
                pass
        page.wait_for_timeout(2000)
        _dismiss_cookies(page)

        # Attendre un XHR repost si possible
        try:
            page.wait_for_response(
                lambda r: _api_url_matches(r.url, "/api/repost/item_list") and r.status == 200,
                timeout=8000,
            )
        except Exception:
            pass
        page.wait_for_timeout(800)

        profile_on_reposts = _extract_profile_from_page(page, handle, encode_avatar=False)
        for key in ("repost_count", "video_count", "nickname", "bio", "avatar", "avatar_url"):
            new_v = profile_on_reposts.get(key)
            old_v = profile.get(key)
            if key in ("repost_count", "video_count"):
                if int(new_v or 0) > int(old_v or 0):
                    profile[key] = int(new_v)
            elif key == "avatar":
                if new_v and not old_v:
                    profile[key] = new_v
            elif new_v and not old_v:
                profile[key] = new_v
        if on_profile and (profile.get("avatar") or profile.get("avatar_url")):
            try:
                on_profile(dict(profile))
            except Exception:
                pass

        progress(f"Collecte des reposts (cible {max_items})…")
        reposts = _collect_from_api(
            page,
            api_substr="/api/repost/item_list",
            kind="repost",
            max_items=max_items,
            seed_items=list(early_reposts),
            seed_url=early_repost_url,
        )

        # Fallbacks si XHR vide (blocage partiel / onglet raté)
        if len(reposts) < 5:
            progress("Peu de XHR — fallback données page…")
            for src in (
                _extract_embedded_items(page, kind="repost"),
                _extract_dom_items(page, kind="repost"),
            ):
                seen_ids = {str(x.get("id") or "") for x in reposts if x.get("id")}
                for item in src:
                    vid = str(item.get("id") or "")
                    if vid and vid in seen_ids:
                        continue
                    if vid:
                        seen_ids.add(vid)
                    reposts.append(item)
                if len(reposts) >= max_items:
                    break
            reposts = reposts[:max_items]

        # 2e tentative navigateur si toujours vide
        if not reposts and not _page_looks_blocked(page):
            progress("Aucun repost — nouvel essai…")
            try:
                page.goto(f"{base}?tab=repost", wait_until="networkidle", timeout=35000)
            except Exception:
                page.goto(f"{base}?tab=repost", wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2500)
            _click_tab(page, ("Reposts", "Republiés", "Republier", "Repost"))
            page.wait_for_timeout(1500)
            more = _collect_from_api(
                page,
                api_substr="/api/repost/item_list",
                kind="repost",
                max_items=max_items,
            )
            if more:
                reposts = more
            if not reposts:
                reposts = _extract_dom_items(page, kind="repost")[:max_items]

        progress(f"{len(reposts)} reposts récupérés…")

        # Posts ensuite (échantillon) — sauté en mode léger (RAM)
        posts: list[dict[str, Any]] = []
        if max_posts > 0:
            progress("Lecture des posts…")
            _click_tab(page, ("Videos", "Vidéos", "Posts"))
            page.wait_for_timeout(1000)
            posts = _collect_from_api(
                page,
                api_substr="/api/post/item_list",
                kind="post",
                max_items=max_posts,
            )
        try:
            page.remove_listener("response", _capture_early)
        except Exception:
            pass

        again = _extract_profile_from_page(page, handle, encode_avatar=False)
        if int(again.get("repost_count") or 0) > int(profile.get("repost_count") or 0):
            profile["repost_count"] = int(again["repost_count"])
        if api_total["repost"] > int(profile.get("repost_count") or 0):
            profile["repost_count"] = api_total["repost"]
        if api_total["post"] > int(profile.get("video_count") or 0):
            profile["video_count"] = api_total["post"]

        if not posts and not reposts:
            blocked = _page_looks_blocked(page)
            raise RuntimeError(
                "TikTok a bloqué le scrape (captcha / bot detection)."
                if blocked
                else "Aucun contenu trouvé (posts / reposts). Profil privé, onglets masqués, "
                "ou TikTok a bloqué le navigateur."
            )

        scraped_r = len(reposts)
        scraped_p = len(posts)
        profile["reposts_requested"] = max_items
        profile["reposts_scraped"] = scraped_r
        profile["posts_scraped"] = scraped_p
        profile["repost_incomplete"] = scraped_r < max_items

        known_total = int(profile.get("repost_count") or 0)
        # Ne jamais faire croire que le total = le scrapé si on n'a pas atteint la cible
        if known_total == 0:
            profile["repost_total_unknown"] = True
            # garde 0 côté total ; l'UI affichera "X analysés / cible Y"
        elif known_total < scraped_r:
            profile["repost_count"] = scraped_r
            profile["repost_total_uncertain"] = True

        if scraped_r < max_items:
            profile["repost_total_uncertain"] = True

        known_videos = int(profile.get("video_count") or 0)
        if known_videos < scraped_p:
            profile["video_count"] = scraped_p
        elif known_videos == 0 and scraped_p:
            profile["video_count"] = scraped_p

        progress("Analyse IA…")
        return handle, posts, reposts, profile
    finally:
        context.close()
        browser.close()
        p.stop()


# Compat ancienne API
def fetch_reposts(
    profile_url: str,
    *,
    max_items: int = 100,
    headless: bool = True,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    handle, posts, reposts, profile = fetch_profile_content(
        profile_url, max_items=max_items, headless=headless
    )
    items = reposts or posts
    return handle, items, profile
