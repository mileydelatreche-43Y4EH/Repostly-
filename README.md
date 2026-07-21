# TikTok Intel

Lien profil → **scrape local** des reposts (Playwright) → résumé + accroches DM (Claude Haiku).

**Plus besoin d’Apify.**

## Comment on récupère les reposts

1. Chromium ouvre `tiktok.com/@user`
2. Clic sur l’onglet **Reposts**
3. Intercepte l’API interne `/api/repost/item_list/`
4. Scroll + pagination via le `fetch` du navigateur (signé par TikTok)

Coût ≈ **0 €** côté scrape (juste ton PC + électricité). Seul Claude est facturé.

## Setup

```powershell
cd c:\Users\miley\screen-replay\tiktok-intel
.\.venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

`.env` : `ANTHROPIC_API_KEY` (déjà configurée en local).

## Lancer

```powershell
lancer.bat
```

→ http://127.0.0.1:8787

Si captcha : mets `SCRAPE_HEADLESS=0` dans `.env` et résous-le une fois.

## Limites

- Onglet Reposts **public** uniquement (sinon TikTok ne montre rien)
- TikTok peut bloquer / captcha → réessayer plus tard
- Commence à **50–100** reposts pour tester
