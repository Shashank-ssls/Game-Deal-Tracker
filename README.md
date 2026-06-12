# 🎮 Game Deal Tracker

A self-hosted Python bot that finds **free games** and **quality discounts** across multiple
stores, checks whether they're actually worth playing (review scores, Metacritic, publisher),
and posts them to **Discord** as rich embeds — region-locked to your country's prices and
remembering what it has already shown so you only hear about new deals.

![Python](https://img.shields.io/badge/python-3.11+-blue)
![License](https://img.shields.io/badge/license-MIT-green)

---

## ⚠️ Disclaimer

- This is a **personal, educational project**, not affiliated with or endorsed by Valve/Steam,
  Epic Games, IsThereAnyDeal, CheapShark, SteamSpy, or Discord.
- It only reads **public data** and uses **official, free APIs** within their normal rate limits.
  It does **not** automate purchases, log into your Steam account, or scrape anything behind a login.
- You supply **your own** API keys and webhook. **No secrets are included in this repository** —
  you create a local `.env` (which is gitignored) from the provided `.env.example`.
- Prices, availability, and store data are best-effort and can be wrong or stale; always confirm
  on the store page before buying. Provided **as-is, with no warranty** (see [LICENSE](LICENSE)).
- Respect each service's Terms of Service. You are responsible for how you run and configure this.

---

## ✨ What it does

Each run, the bot:

1. **Finds deals** across **Epic** (free games), **Steam** (free promos + storefront),
   **CheapShark** (cross-store discovery), and **IsThereAnyDeal** (deals + region pricing).
2. **Region-locks** them — discovered USD deals are re-verified in your currency (default INR);
   anything you can't buy in your region is dropped.
3. **Judges quality** — attaches Steam review summary (e.g. *Very Positive · 94%*), review count,
   Metacritic, publisher/developer and genres, then trims junk with a configurable review/Metacritic gate.
4. **Highlights what you care about**:
   - 🆓 **Free games** first (Epic giveaways, Steam 100%-off / free weekends).
   - 🏷️ **Quality picks** — games from your preferred publishers, your manual **watchlist**, whole
     **franchises** (e.g. anything *Resident Evil* / *Dark Souls*), and titles **auto-derived** from
     your preferred publishers' notable games.
   - 💜 **Wishlist hits** — anything on your Steam wishlist on sale at any discount (optional).
   - 💸 **Discounts** — everything else above your threshold.
5. **Badges**: 💎 *Premium pick* (pricey game now cheap), 🔥 *Lowest ever* (per-store, or true
   all-store all-time low), 📉 *Cheaper than before* (undercuts its recent low).
6. **Posts only new deals** — a local SQLite DB remembers what you've seen (and re-notifies when a
   deal gets *even cheaper*). A deal is marked seen only after Discord accepts the post.

Output order is fixed: **free → quality picks → wishlist → discounts**.

---

## 🚀 Implementation steps

### 1. Clone and install

```bash
git clone https://github.com/YOUR_USERNAME/game-deal-tracker.git
cd game-deal-tracker
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Requires **Python 3.11+**.

### 2. Get your keys (all free)

| Secret | Where to get it | Required? |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | Discord → Server → channel → *Edit Channel → Integrations → Webhooks → New* | ✅ Required |
| `ITAD_API_KEY` | https://isthereanydeal.com → account → **API Keys** (not the OAuth client secret) | ✅ Required |
| `STEAM_API_KEY` | https://steamcommunity.com/dev/apikey | ⬜ Optional — enables the fast offline appid index |
| `STEAM_ID64` | https://steamid.io (paste your profile URL → 17-digit number) | ⬜ Optional — enables wishlist alerts |

> `STEAM_ID64` is a **public** identifier, not a login — the bot never asks for your Steam
> username/password. For wishlist alerts your Steam **profile → Privacy → Game details** must be
> **Public**. For the offline appid index, also obtain a free Steam Web API key above.

### 3. Configure

```bash
cp .env.example .env                  # paste your secrets here (gitignored, never committed)
cp config.example.yaml config.yaml    # optional — tweak region/threshold/publishers/features
python -m src.main --check-config     # validates setup WITHOUT printing any secret value
```

`config.yaml` is optional; without it the built-in defaults apply. Every tunable is documented in
[`config.example.yaml`](config.example.yaml).

### 4. Run

```bash
python -m src.main --dry-run   # prints the exact Discord embed JSON instead of posting
python -m src.main             # posts to Discord for real
python -m src.main -v          # -v (INFO) or -vv (DEBUG) for verbose logs
python -m src.main --stats     # summary of recent runs (found / new / errors)
```

### 5. Schedule it (so it runs daily on its own)

**Local — Windows (Task Scheduler):** Create Basic Task → Daily → *Start a program* → program
`python`, arguments `-m src.main`, **Start in** = the repo folder.

**Local — Linux/macOS (cron):** `crontab -e` →
```
30 9 * * * cd /path/to/game-deal-tracker && ./venv/bin/python -m src.main
```

**Cloud — GitHub Actions:** push the repo, then **Settings → Secrets and variables → Actions** and
add `DISCORD_WEBHOOK_URL`, `ITAD_API_KEY` (+ optional `STEAM_ID64`, `STEAM_API_KEY`). The included
[`daily.yml`](.github/workflows/daily.yml) runs at **03:30 UTC ≈ 09:00 IST** and can be triggered
manually from the Actions tab. The dedup DB is persisted via the Actions cache (best-effort).

---

## ⚙️ Configuration reference (`config.yaml`)

All keys are optional; defaults shown. See [`config.example.yaml`](config.example.yaml) for inline notes.

| Key | Default | Meaning |
|---|---|---|
| `region` / `currency` | `IN` / `INR` | Store region for prices/availability; display currency |
| `min_discount_pct` | `70` | Discount threshold for the deals section |
| `discovery_discount_pct` | `50` | Sources fetch down to this %, but sub-threshold deals are kept only if premium/preferred/wishlisted/franchise |
| `dedup_expiry_days` | `30` | Forget seen deals older than this |
| `ratings_cache_days` | `7` | Re-fetch review scores after this many days |
| `max_deals_per_run` | `30` | Caps per-source fetch, enrichment, and price-checks per run |
| `max_discounts_per_run` | `0` | 0 = unlimited; caps the discount section only |
| `preferred_publishers` | `[]` | Devs/publishers pulled into the pinned "Quality picks" section (word-boundary, case-insensitive) |
| `preferred_mention` / `wishlist_mention` | `""` | Optional `@role`/`@everyone` mention on that section only |
| `min_review_pct` / `min_review_count` / `min_metacritic` | `0` | Quality gate for the regular discount list (0 = off; unknowns pass) |
| `premium_original_min` / `premium_sale_max` | `1000` / `500` | 💎 Premium-pick thresholds (orig > min and sale ≤ max) |
| `exclude_early_access` | `false` | Drop Early Access / unreleased games |
| `exclude_genres` | `[]` | Drop games in these Steam genres |
| `include_genres` | `[]` | Keep **only** deals in these genres (allow-list; unknown genres pass; exclude wins) |
| `franchises` | `[]` | Promote any discovered title containing one of these names into Quality picks (overrides content filters) |
| `watchlist` | `[]` | Specific titles price-checked every run; surface in Quality picks the moment they're on sale |
| `derive_watchlist` | `false` | Auto-add your preferred publishers' notable games (via SteamSpy) to the watchlist |
| `catalogue_cache_days` / `max_derived_titles` | `7` / `40` | Refresh cadence and cap for the derived list |
| `resolve_steam_appids` | `false` | Search Steam by title to attach an appid to non-Steam deals (extra HTTP) |
| `use_appid_index` | `false` | Resolve titles offline from a cached Steam games list (needs `STEAM_API_KEY`) |
| `appindex_cache_days` | `7` | Refresh the cached app list after this many days |
| `all_store_low` | `false` | 🔥 only when at the all-time low across **all** stores (ITAD), not just one |
| `price_drop_window_days` | `0` | 📉 flag deals cheaper than their low over the last N days (0 = off; warms up over a few days) |
| `ending_soon_hours` | `48` | Re-notify a deal once when within this many hours of ending (0 = off) |
| `stores` / `exclude_stores` | `[]` | Allowlist / blocklist of store names (case-insensitive substring) |
| `sections` | all | Which sections to post: `free, wishlist, preferred, discounts` |

---

## 🧪 Tests

```bash
pip install -r requirements-dev.txt
pytest                 # all HTTP is mocked — runs fully offline, needs no keys
ruff check src tests   # lint
mypy src               # type-check
```

---

## 🔐 Security

No secret is ever committed: `.env`, `config.yaml`, and the local `*.db` are gitignored, and the
bot never prints secret values (`--check-config` reports only `set` / `MISSING`). If your Discord
webhook ever leaks, delete & regenerate it in the channel settings and update `.env`.

## 📜 License

[MIT](LICENSE).
