(() => {
  const RECENT_KEY = "repostly_recent";
  const RECENT_MAX = 8;
  const THEME_KEY = "repostly_theme";

  const form = document.getElementById("form");
  const profile = document.getElementById("profile");
  const maxEl = document.getElementById("max");
  const maxVideosEl = document.getElementById("max-videos");
  const go = document.getElementById("go");
  const goArchive = document.getElementById("go-archive");
  const status = document.getElementById("status");
  const themeToggle = document.getElementById("theme-toggle");
  const optsReposts = document.getElementById("opts-reposts");
  const optsArchive = document.getElementById("opts-archive");

  const viewHome = document.getElementById("view-home");
  const viewScan = document.getElementById("view-scan");
  const viewResults = document.getElementById("view-results");
  const viewArchive = document.getElementById("view-archive");

  let currentMode = "archive";

  const scanAvatar = document.getElementById("scan-avatar");
  const scanFallback = document.getElementById("scan-fallback");
  const scanUser = document.getElementById("scan-user");
  const scanName = document.getElementById("scan-name");
  const scanStep = document.getElementById("scan-step");

  const recentWrap = document.getElementById("recent-wrap");
  const recentList = document.getElementById("recent-list");

  function syncThemeLabel() {
    const dark = document.documentElement.classList.contains("dark");
    themeToggle.setAttribute(
      "aria-label",
      dark ? "Passer en mode clair" : "Passer en mode sombre",
    );
  }

  themeToggle.addEventListener("click", () => {
    const dark = document.documentElement.classList.toggle("dark");
    try {
      localStorage.setItem(THEME_KEY, dark ? "dark" : "light");
    } catch (_) {}
    syncThemeLabel();
  });
  syncThemeLabel();

  const steps = [
    "Connexion…",
    "Lecture du profil…",
    "Collecte des reposts…",
    "Analyse des goûts…",
    "Portrait en cours…",
  ];
  let stepTimer = null;
  let liveSteps = false;

  function showView(name) {
    viewHome.classList.toggle("hidden", name !== "home");
    viewScan.classList.toggle("hidden", name !== "scan");
    viewResults.classList.toggle("hidden", name !== "results");
    viewArchive.classList.toggle("hidden", name !== "archive");
    if (name === "home") void renderRecent();
  }

  function setMode(mode) {
    currentMode = mode === "archive" ? "archive" : "reposts";
    document.querySelectorAll(".mode-btn").forEach((btn) => {
      const on = btn.dataset.mode === currentMode;
      btn.classList.toggle("active", on);
      btn.setAttribute("aria-selected", on ? "true" : "false");
    });
    optsReposts.classList.toggle("hidden", currentMode !== "reposts");
    optsArchive.classList.toggle("hidden", currentMode !== "archive");
    setStatus("");
  }

  document.querySelectorAll(".mode-btn").forEach((btn) => {
    btn.addEventListener("click", () => setMode(btn.dataset.mode));
  });

  function setStatus(msg, kind = "") {
    status.textContent = msg;
    status.className = "status" + (kind ? ` ${kind}` : "");
  }

  function fillList(el, items) {
    el.innerHTML = "";
    (items || []).forEach((t) => {
      const li = document.createElement("li");
      li.textContent = typeof t === "string" ? t : String(t);
      el.appendChild(li);
    });
    if (!items || !items.length) {
      const li = document.createElement("li");
      li.textContent = "—";
      el.appendChild(li);
    }
  }

  function setAvatar(imgEl, fallbackEl, url, letter) {
    if (url) {
      imgEl.src = url;
      imgEl.classList.remove("hidden");
      fallbackEl.classList.add("hidden");
      imgEl.onerror = () => {
        imgEl.classList.add("hidden");
        fallbackEl.classList.remove("hidden");
        fallbackEl.textContent = (letter || "@").slice(0, 1).toUpperCase();
      };
    } else {
      imgEl.classList.add("hidden");
      fallbackEl.classList.remove("hidden");
      fallbackEl.textContent = (letter || "@").slice(0, 1).toUpperCase();
    }
  }

  function startScanUI(handle) {
    showView("scan");
    scanUser.textContent = `@${handle}`;
    scanName.textContent = "";
    setAvatar(scanAvatar, scanFallback, "", handle);
    liveSteps = false;
    let i = 0;
    scanStep.textContent = "Connexion…";
    clearInterval(stepTimer);
    stepTimer = setInterval(() => {
      if (liveSteps) return;
      i = (i + 1) % steps.length;
      scanStep.textContent = steps[i];
    }, 4000);
  }

  function stopScanUI() {
    clearInterval(stepTimer);
    stepTimer = null;
  }

  function applyQuickProfile(p, handle) {
    if (!p) return;
    const letter = p.nickname || handle;
    // data-URL en priorité (marche tout de suite) ; sinon proxy CDN
    const photo =
      (p.avatar && String(p.avatar).startsWith("data:") ? p.avatar : "") ||
      resolveAvatarUrl(p, p.avatar || "");
    setAvatar(scanAvatar, scanFallback, photo, letter);
    if (p.nickname) scanName.textContent = p.nickname;
  }

  /* —— Historique (IndexedDB = survit au refresh ; localStorage trop petit pour les photos) —— */
  const DB_NAME = "repostly_db";
  const DB_STORE = "recent";
  const DB_VER = 3;
  const INDEX_KEY = "repostly_recent_index";
  let recentCache = [];

  function recentId(mode, handle) {
    return `${mode || "reposts"}:${String(handle || "").toLowerCase()}`;
  }

  function openRecentDb() {
    return new Promise((resolve, reject) => {
      const req = indexedDB.open(DB_NAME, DB_VER);
      req.onupgradeneeded = (ev) => {
        const db = req.result;
        const old = ev.oldVersion || 0;
        if (!db.objectStoreNames.contains("avatars")) {
          db.createObjectStore("avatars", { keyPath: "handle" });
        }
        if (old < 3) {
          // Nouvelle store clé id = mode:handle (reposts + archive pour le même @)
          if (db.objectStoreNames.contains(DB_STORE)) {
            db.deleteObjectStore(DB_STORE);
          }
          db.createObjectStore(DB_STORE, { keyPath: "id" });
        } else if (!db.objectStoreNames.contains(DB_STORE)) {
          db.createObjectStore(DB_STORE, { keyPath: "id" });
        }
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error || new Error("IndexedDB indisponible"));
    });
  }

  function idbReq(req) {
    return new Promise((resolve, reject) => {
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }

  async function idbPut(entry) {
    const db = await openRecentDb();
    try {
      await idbReq(db.transaction(DB_STORE, "readwrite").objectStore(DB_STORE).put(entry));
    } finally {
      db.close();
    }
  }

  async function idbGet(id) {
    const db = await openRecentDb();
    try {
      return await idbReq(
        db.transaction(DB_STORE, "readonly").objectStore(DB_STORE).get(id),
      );
    } finally {
      db.close();
    }
  }

  async function idbGetAll() {
    const db = await openRecentDb();
    try {
      const rows = await idbReq(
        db.transaction(DB_STORE, "readonly").objectStore(DB_STORE).getAll(),
      );
      return Array.isArray(rows) ? rows : [];
    } finally {
      db.close();
    }
  }

  async function idbDelete(id) {
    const db = await openRecentDb();
    try {
      await idbReq(
        db.transaction(DB_STORE, "readwrite").objectStore(DB_STORE).delete(id),
      );
    } finally {
      db.close();
    }
  }

  function writeIndex(list) {
    const index = list.map((x) => ({
      id: x.id,
      handle: x.handle,
      mode: x.mode,
      nickname: x.nickname,
      savedAt: x.savedAt,
    }));
    try {
      localStorage.setItem(INDEX_KEY, JSON.stringify(index.slice(0, RECENT_MAX)));
    } catch (_) {
      /* index optionnel */
    }
  }

  async function idbPutAvatar(handle, avatar) {
    if (!handle || !avatar) return;
    const db = await openRecentDb();
    try {
      if (!db.objectStoreNames.contains("avatars")) return;
      await idbReq(
        db.transaction("avatars", "readwrite").objectStore("avatars").put({
          handle,
          avatar,
        }),
      );
    } finally {
      db.close();
    }
  }

  async function idbGetAvatar(handle) {
    const db = await openRecentDb();
    try {
      if (!db.objectStoreNames.contains("avatars")) return "";
      const row = await idbReq(
        db.transaction("avatars", "readonly").objectStore("avatars").get(handle),
      );
      return row?.avatar || "";
    } finally {
      db.close();
    }
  }

  function resolveAvatarUrl(profile, fallback) {
    const p = profile || {};
    const raw = p.avatar || fallback || "";
    const http = p.avatar_url || (raw.startsWith("http") ? raw : "");
    if (raw.startsWith("data:")) return raw;
    if (http) return `/api/avatar?u=${encodeURIComponent(http)}`;
    return raw || "";
  }

  function slimPayload(data) {
    const clone = JSON.parse(JSON.stringify(data));
    const mode = clone.mode === "archive" ? "archive" : "reposts";
    clone.mode = mode;

    if (mode === "archive") {
      clone.items = (clone.items || []).slice(0, 500).map((it) => ({
        id: it.id,
        url: it.url,
        caption: it.caption,
        music: it.music,
        hashtags: it.hashtags,
        cover: it.cover && !String(it.cover).startsWith("data:") ? it.cover : "",
        file: it.file,
        file_size: it.file_size,
        transcript: it.transcript,
        transcript_source: it.transcript_source,
        has_keyword: it.has_keyword,
        error: it.error,
        plays: it.plays,
        likes: it.likes,
        create_time: it.create_time,
      }));
      // chemins locaux inutiles en UI
      delete clone.out_dir;
    } else {
      const trimMedia = (arr) =>
        (arr || []).slice(0, 24).map((it) => ({
          kind: it.kind,
          caption: it.caption,
          author: it.author,
          music: it.music,
          hashtags: it.hashtags,
          url: it.url,
          cover: it.cover && !String(it.cover).startsWith("data:") ? it.cover : "",
          plays: it.plays,
          likes: it.likes,
          id: it.id,
        }));
      clone.posts = trimMedia(clone.posts);
      clone.reposts = trimMedia(clone.reposts);
    }

    if (clone.profile) {
      if (!clone.profile.avatar_url && String(clone.profile.avatar || "").startsWith("http")) {
        clone.profile.avatar_url = clone.profile.avatar;
      }
      clone.profile.avatar = "";
    }
    return clone;
  }

  async function migrateLegacyLocalStorage() {
    try {
      const legacy = JSON.parse(localStorage.getItem(RECENT_KEY) || "[]");
      if (!Array.isArray(legacy) || !legacy.length) return;
      for (const row of legacy) {
        if (!row?.handle || !row?.data) continue;
        const handle = String(row.handle).toLowerCase();
        const avatar = row.avatar || row.data?.profile?.avatar || "";
        const data = row.data;
        if (data.profile && String(data.profile.avatar || "").startsWith("data:")) {
          await idbPutAvatar(handle, data.profile.avatar);
          data.profile.avatar = "";
        } else if (avatar.startsWith("data:")) {
          await idbPutAvatar(handle, avatar);
        }
        const mode = data?.mode === "archive" ? "archive" : "reposts";
        await idbPut({
          id: recentId(mode, handle),
          mode,
          handle,
          nickname: row.nickname || row.handle,
          avatar: avatar.startsWith("http") ? avatar : "",
          savedAt: row.savedAt || Date.now(),
          data,
        });
      }
      localStorage.removeItem(RECENT_KEY);
    } catch (_) {
      try {
        localStorage.removeItem(RECENT_KEY);
      } catch (__) {}
    }
  }

  async function loadRecentEntries() {
    let rows = [];
    try {
      rows = await idbGetAll();
    } catch (_) {
      rows = [];
    }
    rows.sort((a, b) => (b.savedAt || 0) - (a.savedAt || 0));
    if (rows.length > RECENT_MAX) {
      const drop = rows.slice(RECENT_MAX);
      rows = rows.slice(0, RECENT_MAX);
      for (const d of drop) {
        try {
          await idbDelete(d.id || recentId(d.mode || "reposts", d.handle));
        } catch (_) {}
      }
    }
    // Réhydrate photos depuis le store avatars
    for (const row of rows) {
      if (!row.avatar || row.avatar.length < 8) {
        try {
          const a = await idbGetAvatar(row.handle);
          if (a) row.avatar = a;
        } catch (_) {}
      }
      if (!row.avatar) {
        const http = row.data?.profile?.avatar_url;
        if (http) row.avatar = `/api/avatar?u=${encodeURIComponent(http)}`;
      }
    }
    recentCache = rows;
    writeIndex(rows);
    return rows;
  }

  async function saveRecent(data) {
    const p = data.profile || {};
    const handle = String(data.handle || p.handle || "")
      .replace(/^@/, "")
      .toLowerCase();
    if (!handle) return;
    const mode = data.mode === "archive" ? "archive" : "reposts";
    const id = recentId(mode, handle);

    const fullAvatar = p.avatar || "";
    const httpAvatar = p.avatar_url || (fullAvatar.startsWith("http") ? fullAvatar : "");

    if (fullAvatar.startsWith("data:")) {
      try {
        await idbPutAvatar(handle, fullAvatar);
      } catch (_) {}
    }

    const entry = {
      id,
      mode,
      handle,
      nickname: p.nickname || handle,
      avatar: fullAvatar.startsWith("data:")
        ? fullAvatar
        : httpAvatar
          ? `/api/avatar?u=${encodeURIComponent(httpAvatar)}`
          : "",
      savedAt: Date.now(),
      data: slimPayload(data),
    };

    try {
      await idbPut(entry);
    } catch (err) {
      entry.avatar = httpAvatar
        ? `/api/avatar?u=${encodeURIComponent(httpAvatar)}`
        : "";
      try {
        await idbPut(entry);
      } catch (_) {
        console.warn("Impossible de sauvegarder la recherche récente", err);
        return;
      }
    }

    recentCache = [
      entry,
      ...recentCache.filter((x) => x.id !== id),
    ].slice(0, RECENT_MAX);
    writeIndex(recentCache);
    await renderRecent();
  }

  async function renderRecent() {
    const list =
      recentCache.length > 0 ? recentCache : await loadRecentEntries();

    if (!list.length) {
      recentWrap.classList.add("hidden");
      recentList.innerHTML = "";
      return;
    }

    recentWrap.classList.remove("hidden");
    recentList.innerHTML = "";

    list.forEach((entry) => {
      const mode = entry.mode || entry.data?.mode || "reposts";
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "recent-item";
      btn.setAttribute("aria-label", `Ouvrir @${entry.handle}`);

      const letter = (entry.nickname || entry.handle || "@").slice(0, 1).toUpperCase();

      if (entry.avatar) {
        const img = document.createElement("img");
        img.className = "recent-avatar";
        img.alt = "";
        img.src = entry.avatar;
        img.onerror = () => {
          img.replaceWith(fallbackEl(letter));
        };
        btn.appendChild(img);
      } else {
        btn.appendChild(fallbackEl(letter));
      }

      const meta = document.createElement("div");
      meta.className = "recent-meta";
      const nick = document.createElement("p");
      nick.className = "recent-nick";
      nick.textContent = entry.nickname || entry.handle;
      const handleEl = document.createElement("p");
      handleEl.className = "recent-handle";
      handleEl.textContent =
        mode === "archive"
          ? `@${entry.handle} · textes`
          : `@${entry.handle} · reposts`;
      meta.appendChild(nick);
      meta.appendChild(handleEl);
      btn.appendChild(meta);

      const chev = document.createElement("span");
      chev.className = "recent-chevron";
      chev.setAttribute("aria-hidden", "true");
      chev.textContent = "›";
      btn.appendChild(chev);

      btn.addEventListener("click", async () => {
        setStatus("");
        const entryId = entry.id || recentId(mode, entry.handle);
        let payload = entry.data ? JSON.parse(JSON.stringify(entry.data)) : null;
        if (!payload) {
          try {
            const fresh = await idbGet(entryId);
            payload = fresh?.data ? JSON.parse(JSON.stringify(fresh.data)) : null;
          } catch (_) {}
        }
        if (!payload) {
          setStatus("Resultat introuvable — relance une analyse.", "error");
          return;
        }
        if (!payload.profile) payload.profile = {};
        let photo =
          entry.avatar ||
          (await idbGetAvatar(entry.handle).catch(() => "")) ||
          resolveAvatarUrl(payload.profile, "");
        if (photo) payload.profile.avatar = photo;

        // Ouvre directement la page resultat (pas de nouveau scrape)
        if (mode === "archive" || payload.mode === "archive") {
          setMode("archive");
          renderArchive(payload);
        } else {
          setMode("reposts");
          render(payload);
        }
      });

      recentList.appendChild(btn);
    });
  }

  function fallbackEl(letter) {
    const el = document.createElement("div");
    el.className = "recent-fallback";
    el.textContent = letter;
    return el;
  }

  function renderMediaGrid(el, items) {
    el.innerHTML = "";
    (items || []).forEach((it) => {
      const a = document.createElement("a");
      a.className = "media-tile";
      a.href = it.url || "#";
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      if (it.cover) {
        const img = document.createElement("img");
        img.src = it.cover;
        img.alt = "";
        img.loading = "lazy";
        a.appendChild(img);
      } else {
        const ph = document.createElement("div");
        ph.className = "media-ph";
        ph.textContent = (it.caption || "·").slice(0, 1);
        a.appendChild(ph);
      }
      const cap = document.createElement("span");
      cap.className = "media-cap";
      cap.textContent = (it.caption || it.music || "").slice(0, 80);
      a.appendChild(cap);
      el.appendChild(a);
    });
  }

  function renderCategories(cats) {
    const wrap = document.getElementById("dynamic-categories");
    wrap.innerHTML = "";
    (cats || []).forEach((cat) => {
      const items = cat.items || [];
      if (!cat.title || !items.length) return;
      const article = document.createElement("article");
      article.className = "card";
      const h2 = document.createElement("h2");
      h2.textContent = cat.title;
      const ul = document.createElement("ul");
      ul.className = "chips";
      items.forEach((t) => {
        const li = document.createElement("li");
        li.textContent = typeof t === "string" ? t : String(t);
        ul.appendChild(li);
      });
      article.appendChild(h2);
      article.appendChild(ul);
      wrap.appendChild(article);
    });
  }

  function render(data) {
    const a = data.analysis || {};
    const p = data.profile || {};
    const meta = a._meta || {};
    const handle = data.handle || p.handle || "";
    const nick = p.nickname || handle;
    const letter = nick || handle;
    const posts = data.posts || [];
    const reposts = data.reposts || [];
    const videoTotal = data.video_total || p.video_count || data.posts_count || 0;

    setAvatar(
      document.getElementById("res-avatar"),
      document.getElementById("res-fallback"),
      resolveAvatarUrl(p, p.avatar || ""),
      letter,
    );

    document.getElementById("res-nick").textContent = nick;
    const handleLink = document.getElementById("res-handle");
    handleLink.textContent = `@${handle}`;
    handleLink.href = handle
      ? `https://www.tiktok.com/@${encodeURIComponent(handle)}`
      : "#";

    const bioEl = document.getElementById("res-bio");
    if (p.bio) {
      bioEl.textContent = p.bio;
      bioEl.classList.remove("hidden");
    } else {
      bioEl.classList.add("hidden");
    }

    const scraped = Number(data.reposts_count || reposts.length || 0);
    const requested = Number(data.reposts_requested || p.reposts_requested || scraped);
    const total = Number(data.repost_total || p.repost_count || 0);
    const unknown = Boolean(
      data.repost_total_unknown ||
        data.repost_total_uncertain ||
        p.repost_total_unknown ||
        p.repost_total_uncertain,
    );
    const incomplete = Boolean(data.repost_incomplete) || scraped < requested;
    const countEl = document.getElementById("res-count");
    const analyzedEl = document.getElementById("res-analyzed");

    if (total > scraped && total > 0) {
      countEl.textContent = `${total} repost${total === 1 ? "" : "s"}`;
      analyzedEl.textContent = `${scraped} analysés`;
      analyzedEl.classList.remove("hidden");
    } else if (incomplete) {
      countEl.textContent = `${scraped} analysés / ${requested}`;
      analyzedEl.textContent = "TikTok a limité le scrape";
      analyzedEl.classList.remove("hidden");
    } else if (!unknown && total > 0 && scraped >= total) {
      countEl.textContent = `${total} repost${total === 1 ? "" : "s"}`;
      analyzedEl.textContent = "échantillon complet";
      analyzedEl.classList.remove("hidden");
    } else {
      countEl.textContent = `${scraped} repost${scraped === 1 ? "" : "s"} analysés`;
      analyzedEl.classList.add("hidden");
    }

    const postsPill = document.getElementById("res-posts");
    if (posts.length > 0 || videoTotal > 0) {
      postsPill.textContent = `${videoTotal || posts.length} post${(videoTotal || posts.length) === 1 ? "" : "s"}`;
      postsPill.classList.remove("hidden");
    } else {
      postsPill.classList.add("hidden");
    }

    document.getElementById("res-vibe").textContent = a.vibe || a.tone || "—";
    document.getElementById("res-confidence").textContent = `confiance : ${a.confidence || "?"}`;

    document.getElementById("summary").textContent = a.summary || "";
    document.getElementById("tone").textContent = a.tone || a.vibe || "";

    fillList(document.getElementById("personality"), a.personality);
    fillList(document.getElementById("interests"), (a.interests || []).slice(0, 5));
    fillList(document.getElementById("topics"), (a.topics || []).slice(0, 5));
    fillList(document.getElementById("patterns"), a.content_patterns);
    fillList(
      document.getElementById("creators"),
      a.creator_affinity || (meta.top_authors || []).map((x) => `@${x.author} ×${x.count}`),
    );
    fillList(document.getElementById("keywords"), a.keywords);

    renderCategories(a.categories || meta.categories_computed || []);

    const ownCard = document.getElementById("card-own");
    if (a.own_content_style && posts.length) {
      document.getElementById("own-style").textContent = a.own_content_style;
      ownCard.classList.remove("hidden");
    } else {
      ownCard.classList.add("hidden");
    }

    const postsSection = document.getElementById("section-posts");
    if (posts.length) {
      renderMediaGrid(document.getElementById("posts-grid"), posts);
      postsSection.classList.remove("hidden");
    } else {
      postsSection.classList.add("hidden");
    }

    const repostsSection = document.getElementById("section-reposts");
    if (reposts.length) {
      renderMediaGrid(document.getElementById("reposts-grid"), reposts);
      repostsSection.classList.remove("hidden");
    } else {
      repostsSection.classList.add("hidden");
    }

    const tags = document.getElementById("signals-tags");
    tags.innerHTML = "";
    (meta.top_hashtags || []).forEach((h) => {
      const li = document.createElement("li");
      li.textContent = `#${h.tag} ×${h.count}`;
      tags.appendChild(li);
    });

    const authors = document.getElementById("signals-authors");
    authors.innerHTML = "";
    (meta.top_authors || []).forEach((h) => {
      const li = document.createElement("li");
      li.textContent = `@${h.author} ×${h.count}`;
      authors.appendChild(li);
    });

    showView("results");
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function apiError(data, fallback = "Erreur serveur") {
    const d = data?.detail;
    if (typeof d === "string") return d;
    if (Array.isArray(d)) {
      return d.map((x) => x.msg || x.detail || JSON.stringify(x)).join(" ");
    }
    if (d && typeof d === "object") return JSON.stringify(d);
    return fallback;
  }

  function parseHandle(raw) {
    let url = raw.trim();
    if (!url.includes("tiktok.com") && !url.startsWith("http")) {
      url = url.startsWith("@") ? url : `@${url}`;
    }
    const m = url.match(/@([A-Za-z0-9._]+)/);
    return { url, handle: m ? m[1] : url.replace(/^@/, "") };
  }

  document.getElementById("back-home").addEventListener("click", () => {
    showView("home");
    setStatus("");
  });
  document.getElementById("back-home-archive").addEventListener("click", () => {
    showView("home");
    setStatus("");
  });

  function highlightKeyword(text, keyword = "cheaterbuster") {
    const raw = String(text || "");
    if (!raw) return document.createTextNode("Aucun texte extrait.");
    const frag = document.createDocumentFragment();
    const re = new RegExp(`(${keyword.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})`, "gi");
    let last = 0;
    let m;
    while ((m = re.exec(raw)) !== null) {
      if (m.index > last) {
        frag.appendChild(document.createTextNode(raw.slice(last, m.index)));
      }
      const mark = document.createElement("mark");
      mark.className = "kw-hit";
      mark.textContent = m[0];
      frag.appendChild(mark);
      last = m.index + m[0].length;
    }
    if (last < raw.length) {
      frag.appendChild(document.createTextNode(raw.slice(last)));
    }
    if (!frag.childNodes.length) {
      frag.appendChild(document.createTextNode(raw));
    }
    return frag;
  }

  function textHasKeyword(text, keyword = "cheaterbuster") {
    return new RegExp(keyword.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "i").test(String(text || ""));
  }

  function renderArchive(data) {
    const KEYWORD = "cheaterbuster";
    const p = data.profile || {};
    const handle = data.handle || "";
    const letter = p.nickname || handle;
    const avatar = resolveAvatarUrl(p, p.avatar || "");
    setAvatar(
      document.getElementById("arch-avatar"),
      document.getElementById("arch-fallback"),
      avatar,
      letter,
    );
    document.getElementById("arch-nick").textContent = p.nickname || handle || "—";
    const hEl = document.getElementById("arch-handle");
    hEl.textContent = `@${handle}`;
    hEl.href = `https://www.tiktok.com/@${handle}`;

    const bioEl = document.getElementById("arch-bio");
    const bio = (p.bio || "").trim();
    if (bio) {
      bioEl.textContent = bio;
      bioEl.classList.remove("hidden");
    } else {
      bioEl.textContent = "";
      bioEl.classList.add("hidden");
    }

    const items = data.items || [];
    const kwHits = items.filter(
      (it) =>
        it.has_keyword ||
        textHasKeyword(it.transcript, KEYWORD) ||
        textHasKeyword(it.caption, KEYWORD),
    );

    const dl = Number(data.downloaded || items.length || 0);
    const n = items.length || dl;
    document.getElementById("arch-count").textContent =
      `${n} vidéo${n === 1 ? "" : "s"}`;

    const kwEl = document.getElementById("arch-keyword");
    const hits = Number(data.keyword_hits != null ? data.keyword_hits : kwHits.length);
    kwEl.textContent =
      hits > 0
        ? `${hits} cheaterbuster`
        : "0 cheaterbuster";
    kwEl.classList.toggle("hidden", false);

    const folder = document.getElementById("arch-folder");
    if (folder) folder.classList.add("hidden");

    const fileUrl = (name) =>
      `/api/archive/${encodeURIComponent(handle)}/file/${encodeURIComponent(name)}`;

    const zip = document.getElementById("arch-zip");
    if (data.zip) {
      zip.href = fileUrl(data.zip);
      zip.textContent = "Telecharger le ZIP";
      zip.classList.remove("hidden");
    } else {
      zip.classList.add("hidden");
    }

    const fmtNum = (n) => {
      const v = Number(n) || 0;
      if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`;
      if (v >= 1_000) return `${(v / 1_000).toFixed(1)}K`;
      return String(v);
    };
    const fmtSize = (b) => {
      const n = Number(b) || 0;
      if (n < 1024) return `${n} o`;
      if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} Ko`;
      return `${(n / (1024 * 1024)).toFixed(1)} Mo`;
    };
    const fmtDate = (ts) => {
      const t = Number(ts) || 0;
      if (!t) return "";
      try {
        return new Date(t * 1000).toLocaleDateString("fr-FR");
      } catch {
        return "";
      }
    };

    const isHit = (it) =>
      Boolean(it.has_keyword) ||
      textHasKeyword(it.transcript, KEYWORD) ||
      textHasKeyword(it.caption, KEYWORD);

    const sortItems = (arr, mode) => {
      const out = [...arr];
      const ts = (x) => Number(x.create_time) || 0;
      const views = (x) => Number(x.plays) || 0;
      const likes = (x) => Number(x.likes) || 0;
      switch (mode) {
        case "date-asc":
          out.sort((a, b) => ts(a) - ts(b));
          break;
        case "views-desc":
          out.sort((a, b) => views(b) - views(a));
          break;
        case "views-asc":
          out.sort((a, b) => views(a) - views(b));
          break;
        case "likes-desc":
          out.sort((a, b) => likes(b) - likes(a));
          break;
        case "likes-asc":
          out.sort((a, b) => likes(a) - likes(b));
          break;
        case "date-desc":
        default:
          out.sort((a, b) => ts(b) - ts(a));
          break;
      }
      return out;
    };

    let kwOnly = kwHits.length > 0;
    const sortEl = document.getElementById("arch-sort");
    const filtersEl = document.getElementById("arch-filters");
    const onlyKw = document.getElementById("arch-only-kw");
    const showAll = document.getElementById("arch-show-all");

    if (items.length > 1) {
      filtersEl.classList.remove("hidden");
    } else {
      filtersEl.classList.add("hidden");
    }

    if (kwHits.length) {
      onlyKw.classList.remove("hidden");
      showAll.classList.remove("hidden");
      onlyKw.textContent = "Voir cheaterbuster";
      showAll.textContent = "Voir les autres";
    } else {
      onlyKw.classList.add("hidden");
      showAll.classList.add("hidden");
      kwOnly = false;
    }

    const list = document.getElementById("arch-list");

    const paint = () => {
      const mode = sortEl?.value || "date-desc";
      let view;
      if (kwHits.length) {
        view = items.filter((it) => (kwOnly ? isHit(it) : !isHit(it)));
      } else {
        view = items;
      }
      view = sortItems(view, mode);

      onlyKw.classList.toggle("active-filter", kwOnly && kwHits.length > 0);
      showAll.classList.toggle("active-filter", !kwOnly && kwHits.length > 0);

      list.innerHTML = "";
      view.forEach((it, idx) => {
        const hit = isHit(it);
        const row = document.createElement("article");
        row.className = "arch-item" + (hit ? " has-keyword" : "");
        row.dataset.plays = String(it.plays || 0);
        row.dataset.likes = String(it.likes || 0);
        row.dataset.ts = String(it.create_time || 0);

        const media = document.createElement("div");
        media.className = "arch-media";
        const phone = document.createElement("div");
        phone.className = "arch-phone";

        if (it.file) {
          const video = document.createElement("video");
          video.className = "arch-video";
          video.controls = true;
          video.preload = "metadata";
          video.playsInline = true;
          video.setAttribute("playsinline", "");
          if (it.cover) {
            video.poster = it.cover.startsWith("http")
              ? `/api/avatar?u=${encodeURIComponent(it.cover)}`
              : it.cover;
          }
          video.src = fileUrl(it.file);
          phone.appendChild(video);
        } else if (it.cover) {
          const img = document.createElement("img");
          img.className = "arch-cover";
          img.alt = "";
          img.src = it.cover.startsWith("http")
            ? `/api/avatar?u=${encodeURIComponent(it.cover)}`
            : it.cover;
          img.onerror = () => {
            const ph = document.createElement("div");
            ph.className = "arch-cover placeholder";
            ph.textContent = `#${idx + 1}`;
            img.replaceWith(ph);
          };
          phone.appendChild(img);
        } else {
          const ph = document.createElement("div");
          ph.className = "arch-cover placeholder";
          ph.textContent = `#${idx + 1}`;
          phone.appendChild(ph);
        }
        media.appendChild(phone);
        row.appendChild(media);

        const body = document.createElement("div");
        body.className = "arch-body";
        const title = document.createElement("h3");
        title.textContent = hit ? `Video ${idx + 1} — cheaterbuster` : `Video ${idx + 1}`;
        body.appendChild(title);

        const txLabel = document.createElement("p");
        txLabel.className = "arch-transcript-label";
        txLabel.textContent = "Paroles / texte";
        body.appendChild(txLabel);

        const txEl = document.createElement("p");
        txEl.className = "arch-transcript";
        txEl.appendChild(
          highlightKeyword(it.transcript || "Aucun texte extrait.", KEYWORD),
        );
        body.appendChild(txEl);

        if (it.file) {
          const dlBtn = document.createElement("button");
          dlBtn.type = "button";
          dlBtn.className = "arch-dl-btn";
          dlBtn.textContent = "Download";
          dlBtn.title = "Telecharger la video (qualite max)";
          dlBtn.addEventListener("click", (ev) => {
            ev.preventDefault();
            const a = document.createElement("a");
            a.href = fileUrl(it.file);
            a.download = it.file || `${it.id || "video"}.mp4`;
            document.body.appendChild(a);
            a.click();
            a.remove();
          });
          body.appendChild(dlBtn);
        }

        row.appendChild(body);
        list.appendChild(row);
      });
    };

    onlyKw.onclick = () => {
      kwOnly = true;
      paint();
    };
    showAll.onclick = () => {
      kwOnly = false;
      paint();
    };
    if (sortEl) {
      sortEl.onchange = () => paint();
    }

    paint();

    showView("archive");
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  async function loadCapabilities() {
    try {
      const res = await fetch("/api/capabilities");
      if (!res.ok) return;
      const cap = await res.json();
      const sel = maxVideosEl;
      const limits = cap.archive_limits || [10, 20, 50, 100, 250, 500];
      const current = String(cap.default_archive || 100);
      sel.innerHTML = "";
      limits.forEach((n) => {
        const opt = document.createElement("option");
        opt.value = String(n);
        opt.textContent = n === 0 ? "Tout" : String(n);
        if (String(n) === current) opt.selected = true;
        sel.appendChild(opt);
      });
    } catch (_) {}
  }

  async function readSse(res, onEvent) {
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let finalData = null;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const chunks = buffer.split("\n\n");
      buffer = chunks.pop() || "";
      for (const chunk of chunks) {
        const line = chunk
          .split("\n")
          .map((l) => l.trim())
          .find((l) => l.startsWith("data:"));
        if (!line) continue;
        let ev;
        try {
          ev = JSON.parse(line.slice(5).trim());
        } catch {
          continue;
        }
        if (ev.type === "result" && ev.data) finalData = ev.data;
        if (ev.type === "error") {
          throw new Error(ev.detail || "Erreur");
        }
        onEvent(ev);
      }
    }
    return finalData;
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const raw = profile.value.trim();
    if (!raw) return;

    const { url, handle } = parseHandle(raw);
    go.disabled = true;
    if (goArchive) goArchive.disabled = true;
    setStatus("");
    startScanUI(handle);

    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 45 * 60 * 1000);
    const isArchive = currentMode === "archive";

    try {
      const endpoint = isArchive ? "/api/archive" : "/api/analyze";
      const body = isArchive
        ? {
            profile: url,
            max_videos: Number(maxVideosEl.value),
          }
        : {
            profile: url,
            max_reposts: Number(maxEl.value),
          };

      const res = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
        signal: controller.signal,
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const errBody = await res.json().catch(() => ({}));
        throw new Error(apiError(errBody));
      }

      const finalData = await readSse(res, (ev) => {
        if (ev.type === "profile" && ev.data) {
          liveSteps = true;
          applyQuickProfile(ev.data, handle);
          scanStep.textContent = isArchive
            ? "Profil trouvé — téléchargement…"
            : "Profil trouvé — analyse…";
        } else if (ev.type === "progress" && ev.message) {
          liveSteps = true;
          scanStep.textContent = ev.message;
        }
      });

      if (!finalData) throw new Error("Traitement interrompu — réessaie.");

      applyQuickProfile(finalData.profile || {}, handle);
      scanStep.textContent = isArchive ? "Archive prête" : "Portrait prêt";
      await new Promise((r) => setTimeout(r, 350));

      stopScanUI();
      await saveRecent(finalData);
      if (isArchive || finalData.mode === "archive") {
        renderArchive(finalData);
      } else {
        render(finalData);
      }
    } catch (err) {
      stopScanUI();
      showView("home");
      const msg =
        err.name === "AbortError"
          ? "Timeout — réduis le nombre de vidéos / reposts."
          : /fetch|network|load failed|erreur serveur/i.test(String(err.message || ""))
            ? "Connexion coupée (souvent manque de RAM sur Render Free). Réessaie avec moins d’items."
            : err.message || "Erreur";
      setStatus(msg, "error");
    } finally {
      clearTimeout(timeout);
      go.disabled = false;
      if (goArchive) goArchive.disabled = false;
    }
  });

  async function initRecent() {
    try {
      await migrateLegacyLocalStorage();
      await loadRecentEntries();
      await renderRecent();
    } catch (err) {
      console.warn("Historique récent indisponible", err);
    }
  }

  void loadCapabilities();
  void initRecent();
})();
