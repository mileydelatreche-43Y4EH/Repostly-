(() => {
  const RECENT_KEY = "repostly_recent";
  const RECENT_MAX = 8;
  const THEME_KEY = "repostly_theme";

  const form = document.getElementById("form");
  const profile = document.getElementById("profile");
  const maxEl = document.getElementById("max");
  const go = document.getElementById("go");
  const status = document.getElementById("status");
  const themeToggle = document.getElementById("theme-toggle");

  const viewHome = document.getElementById("view-home");
  const viewScan = document.getElementById("view-scan");
  const viewResults = document.getElementById("view-results");

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
    "Lecture du profil…",
    "Collecte des reposts…",
    "Lecture des posts…",
    "Analyse des goûts…",
    "Portrait en cours…",
  ];
  let stepTimer = null;

  function showView(name) {
    viewHome.classList.toggle("hidden", name !== "home");
    viewScan.classList.toggle("hidden", name !== "scan");
    viewResults.classList.toggle("hidden", name !== "results");
    if (name === "home") void renderRecent();
  }

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
    let i = 0;
    scanStep.textContent = steps[0];
    clearInterval(stepTimer);
    stepTimer = setInterval(() => {
      i = (i + 1) % steps.length;
      scanStep.textContent = steps[i];
    }, 3400);
  }

  function stopScanUI() {
    clearInterval(stepTimer);
    stepTimer = null;
  }

  function applyQuickProfile(p, handle) {
    if (!p) return;
    const letter = p.nickname || handle;
    const photo = resolveAvatarUrl(p, p.avatar || "");
    setAvatar(scanAvatar, scanFallback, photo, letter);
    if (p.nickname) scanName.textContent = p.nickname;
  }

  /* —— Historique (IndexedDB = survit au refresh ; localStorage trop petit pour les photos) —— */
  const DB_NAME = "repostly_db";
  const DB_STORE = "recent";
  const INDEX_KEY = "repostly_recent_index";
  let recentCache = [];

  function openRecentDb() {
    return new Promise((resolve, reject) => {
      const req = indexedDB.open(DB_NAME, 2);
      req.onupgradeneeded = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains(DB_STORE)) {
          db.createObjectStore(DB_STORE, { keyPath: "handle" });
        }
        if (!db.objectStoreNames.contains("avatars")) {
          db.createObjectStore("avatars", { keyPath: "handle" });
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

  async function idbGet(handle) {
    const db = await openRecentDb();
    try {
      return await idbReq(
        db.transaction(DB_STORE, "readonly").objectStore(DB_STORE).get(handle),
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

  async function idbDelete(handle) {
    const db = await openRecentDb();
    try {
      await idbReq(
        db.transaction(DB_STORE, "readwrite").objectStore(DB_STORE).delete(handle),
      );
    } finally {
      db.close();
    }
  }

  function writeIndex(list) {
    const index = list.map((x) => ({
      handle: x.handle,
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
    // Ne pas stocker la data-URL (trop lourde) — on garde avatar_url + store séparé
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
        await idbPut({
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
          await idbDelete(d.handle);
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

    const fullAvatar = p.avatar || "";
    const httpAvatar = p.avatar_url || (fullAvatar.startsWith("http") ? fullAvatar : "");

    // Photo isolée (data-URL OK ici)
    if (fullAvatar.startsWith("data:")) {
      try {
        await idbPutAvatar(handle, fullAvatar);
      } catch (_) {}
    }

    const entry = {
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
      ...recentCache.filter((x) => x.handle !== handle),
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
      handleEl.textContent = `@${entry.handle}`;
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
        let payload = entry.data ? JSON.parse(JSON.stringify(entry.data)) : null;
        if (!payload) {
          try {
            const fresh = await idbGet(entry.handle);
            payload = fresh?.data ? JSON.parse(JSON.stringify(fresh.data)) : null;
          } catch (_) {}
        }
        if (!payload) {
          setStatus("Résultat introuvable — relance une analyse.", "error");
          return;
        }
        if (!payload.profile) payload.profile = {};
        let photo =
          entry.avatar ||
          (await idbGetAvatar(entry.handle).catch(() => "")) ||
          resolveAvatarUrl(payload.profile, "");
        if (photo) payload.profile.avatar = photo;
        render(payload);
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
    const total = Number(data.repost_total || p.repost_count || 0);
    const unknown = Boolean(data.repost_total_unknown);
    const countEl = document.getElementById("res-count");
    const analyzedEl = document.getElementById("res-analyzed");

    if (!unknown && total > 0) {
      countEl.textContent = `${total} repost${total === 1 ? "" : "s"}`;
      if (scraped > 0 && scraped < total) {
        analyzedEl.textContent = `${scraped} analysés`;
        analyzedEl.classList.remove("hidden");
      } else if (scraped > 0 && scraped === total) {
        analyzedEl.textContent = "tous analysés";
        analyzedEl.classList.remove("hidden");
      } else {
        analyzedEl.classList.add("hidden");
      }
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

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const raw = profile.value.trim();
    if (!raw) return;

    const { url, handle } = parseHandle(raw);
    go.disabled = true;
    setStatus("");
    startScanUI(handle);

    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15 * 60 * 1000);

    try {
      try {
        const pr = await fetch("/api/profile", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          signal: controller.signal,
          body: JSON.stringify({ profile: url }),
        });
        const quick = await pr.json().catch(() => ({}));
        if (pr.ok) {
          applyQuickProfile(quick, handle);
          scanStep.textContent = "Profil trouvé — analyse…";
        } else {
          console.warn("profile preview", pr.status, quick);
          scanStep.textContent = "Profil lent — analyse…";
        }
      } catch (err) {
        console.warn("profile preview fail", err);
        scanStep.textContent = "Profil lent — analyse…";
      }

      const res = await fetch("/api/analyze", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        signal: controller.signal,
        body: JSON.stringify({
          profile: url,
          max_reposts: Number(maxEl.value),
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(apiError(data));

      const p = data.profile || {};
      applyQuickProfile(p, handle);
      scanStep.textContent = "Portrait prêt";
      await new Promise((r) => setTimeout(r, 450));

      stopScanUI();
      await saveRecent(data);
      render(data);
    } catch (err) {
      stopScanUI();
      showView("home");
      const msg =
        err.name === "AbortError"
          ? "Timeout — réduis le nombre de reposts."
          : err.message || "Erreur";
      setStatus(msg, "error");
    } finally {
      clearTimeout(timeout);
      go.disabled = false;
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

  void initRecent();
})();
