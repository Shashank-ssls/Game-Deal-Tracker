"""Configuration loading for Game Deal Tracker.

Secrets come ONLY from environment variables (loaded from a gitignored ``.env``).
Non-secret, user-tunable settings come from ``config.yaml`` and fall back to the
built-in defaults that mirror ``config.example.yaml``.

Hard rule (see CLAUDE.md / SECURITY.md): no secret value is ever printed, logged,
or returned by :meth:`Settings.status_report` / :meth:`Settings.validate`.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Repo root = the game-deal-tracker/ directory, one level above src/.
# All paths resolve relative to it so the project runs from any folder.
REPO_ROOT: Path = Path(__file__).resolve().parents[1]
CONFIG_PATH: Path = REPO_ROOT / "config.yaml"
ENV_PATH: Path = REPO_ROOT / ".env"

# Defaults mirror config.example.yaml exactly.
DEFAULT_SETTINGS: dict[str, Any] = {
    "region": "IN",
    "currency": "INR",
    "min_discount_pct": 70,
    "dedup_expiry_days": 30,
    "ratings_cache_days": 7,
    "wishlist_mention": "",
    # Caps enrichment / per-title price-check work ONLY (ratings, watchlist title
    # fallbacks). It no longer sizes the discovery feed requests — see feed_max_deals.
    "max_deals_per_run": 30,
    # Store deal feeds (paginated discovery). feed_max_deals bounds the TOTAL deals
    # ingested per source; feed_page_sleep throttles between feed pages.
    "feed_max_deals": 800,
    "feed_page_sleep": 0.5,
    # Publisher metadata is attached in bulk: how many ITAD game ids per info call.
    "publisher_metadata_batch": 100,
    # Quality / preferred-publisher settings.
    "preferred_publishers": [],
    "min_review_pct": 0,
    "min_metacritic": 0,
    "preferred_mention": "",
    # "Premium pick": a pricey game (original above premium_original_min) that is
    # now meaningfully cheaper. premium_mode picks how "cheaper" is judged:
    #   absolute -> sale price <= premium_sale_max
    #   percent  -> discount >= premium_min_discount_pct
    #   either   -> absolute OR percent (default)   both -> absolute AND percent
    "premium_original_min": 1000,
    "premium_sale_max": 500,
    "premium_mode": "either",
    "premium_min_discount_pct": 60,
    # Hide deals whose ORIGINAL (pre-discount) price is below this many INR, to
    # declutter the feed of budget/indie titles. Free games are always exempt.
    # 0 disables the filter.
    "min_original_price": 0,
    # Trust gate: ignore review % unless backed by at least this many reviews.
    "min_review_count": 0,
    # Wide-net discovery: fetch deals down to this %, but only KEEP a sub-threshold
    # deal if it is premium / preferred / wishlisted (keeps the feed focused).
    "discovery_discount_pct": 50,
    # Content filters.
    "exclude_early_access": False,
    "exclude_genres": [],
    # Positive genre filter: when non-empty, keep only deals whose genres include
    # at least one of these (deals with unknown genres are kept). Exclude wins.
    "include_genres": [],
    # Franchise watch: any discovered deal whose title contains one of these names
    # is promoted into the Quality picks section (exempt from the quality gate),
    # even below the discount threshold. Passive — over the existing fetch pool.
    "franchises": [],
    # Re-notify a deal once when it is within this many hours of ending (0 = off).
    "ending_soon_hours": 48,
    # DEPRECATED (kept so existing configs still load; warned once at startup):
    # deals are now identified by ITAD game ids, so the local Steam app index and
    # title->appid resolution are no longer used.
    "resolve_steam_appids": False,
    "use_appid_index": False,
    "appindex_cache_days": 7,
    # DEPRECATED (kept so existing configs still load; warned once at startup):
    # publisher matching now scans the live store feeds, so the SteamSpy-derived
    # watchlist is gone.
    "derive_watchlist": False,
    "catalogue_cache_days": 7,
    "max_derived_titles": 40,
    # All-store historical low: flag a deal 🔥 only when its price is at/below the
    # lowest it has EVER been across all stores (ITAD), not just one store.
    "all_store_low": False,
    # Price-trend: flag 📉 a deal cheaper than its lowest price seen in this many
    # days (0 = off). Requires the local price-history table to accumulate first.
    "price_drop_window_days": 0,
    # Which sections to post, and an optional cap on the discount section.
    "sections": ["free", "wishlist", "preferred", "discounts"],
    "max_discounts_per_run": 0,
    # AAA watchlist: specific game titles price-checked every run so they surface
    # the moment they're discounted (any %), regardless of the fetch pool.
    "watchlist": [],
    # Store selection (case-insensitive substring match on the deal's store name).
    # stores = allowlist (empty = every store); exclude_stores = blocklist.
    "stores": [],
    "exclude_stores": [],
}

VALID_SECTIONS = ("free", "wishlist", "preferred", "discounts")

# Config keys that are still accepted (so existing config.yaml files keep loading)
# but no longer change behaviour. Each maps to the reason shown in the one-time
# startup warning.
DEPRECATED_KEYS: dict[str, str] = {
    "derive_watchlist": "publisher matching now scans the live store feeds",
    "catalogue_cache_days": "publisher matching now scans the live store feeds",
    "max_derived_titles": "publisher matching now scans the live store feeds",
    "use_appid_index": (
        "deals are identified by ITAD game ids; the local Steam app index is no longer used"
    ),
    "appindex_cache_days": (
        "deals are identified by ITAD game ids; the local Steam app index is no longer used"
    ),
    "resolve_steam_appids": (
        "deals are identified by ITAD game ids; the local Steam app index is no longer used"
    ),
}

# Secret attribute name -> (environment variable name, required?).
SECRET_VARS: dict[str, tuple[str, bool]] = {
    "discord_webhook_url": ("DISCORD_WEBHOOK_URL", True),
    "itad_api_key": ("ITAD_API_KEY", True),
    "steam_id64": ("STEAM_ID64", False),
    "steam_api_key": ("STEAM_API_KEY", False),
}


def _warn_deprecated_keys(loaded: dict[str, Any]) -> None:
    """Warn once (at startup) for each removed config key that is still present."""
    for key in DEPRECATED_KEYS:
        if key in loaded:
            logger.warning(
                "config: ignoring removed setting '%s' (deals now come from live store feeds)",
                key,
            )


@dataclass
class Settings:
    """Typed view of all configuration. Construct via :meth:`load`."""

    # Secrets (may be None when unset). Never print these.
    discord_webhook_url: str | None = None
    itad_api_key: str | None = None
    steam_id64: str | None = None
    steam_api_key: str | None = None

    # Non-secret, user-tunable settings.
    region: str = "IN"
    currency: str = "INR"
    min_discount_pct: int = 70
    dedup_expiry_days: int = 30
    ratings_cache_days: int = 7
    wishlist_mention: str = ""
    max_deals_per_run: int = 30
    feed_max_deals: int = 800
    feed_page_sleep: float = 0.5
    publisher_metadata_batch: int = 100

    # Quality / preferred-publisher settings.
    preferred_publishers: list[str] = field(default_factory=list)
    min_review_pct: int = 0
    min_metacritic: int = 0
    preferred_mention: str = ""
    premium_original_min: int = 1000
    premium_sale_max: int = 500
    premium_mode: str = "either"
    premium_min_discount_pct: int = 60
    min_original_price: int = 0
    min_review_count: int = 0
    discovery_discount_pct: int = 50
    exclude_early_access: bool = False
    exclude_genres: list[str] = field(default_factory=list)
    include_genres: list[str] = field(default_factory=list)
    franchises: list[str] = field(default_factory=list)
    ending_soon_hours: int = 48
    resolve_steam_appids: bool = False
    use_appid_index: bool = False
    appindex_cache_days: int = 7
    derive_watchlist: bool = False
    catalogue_cache_days: int = 7
    max_derived_titles: int = 40
    all_store_low: bool = False
    price_drop_window_days: int = 0
    sections: list[str] = field(default_factory=lambda: list(VALID_SECTIONS))
    max_discounts_per_run: int = 0
    watchlist: list[str] = field(default_factory=list)
    stores: list[str] = field(default_factory=list)
    exclude_stores: list[str] = field(default_factory=list)

    # Resolved DB path (under the repo root).
    db_path: Path = REPO_ROOT / "data" / "tracker.db"

    @classmethod
    def load(
        cls,
        env_path: Path | None = None,
        config_path: Path | None = None,
    ) -> Settings:
        """Load secrets from the environment and settings from ``config.yaml``.

        ``env_path`` / ``config_path`` are overridable for testing; when omitted
        they default to the repo-root locations.
        """
        env_path = env_path if env_path is not None else ENV_PATH
        config_path = config_path if config_path is not None else CONFIG_PATH

        # python-dotenv populates os.environ; it does not override existing vars.
        if env_path.exists():
            load_dotenv(env_path)

        merged: dict[str, Any] = dict(DEFAULT_SETTINGS)
        if config_path.exists():
            with open(config_path, encoding="utf-8") as fh:
                loaded = yaml.safe_load(fh) or {}
            if isinstance(loaded, dict):
                _warn_deprecated_keys(loaded)
                merged.update(
                    {k: v for k, v in loaded.items() if k in DEFAULT_SETTINGS and v is not None}
                )

        # Treat empty strings (placeholder/unset) as missing.
        def _secret(attr: str) -> str | None:
            env_name = SECRET_VARS[attr][0]
            return os.environ.get(env_name) or None

        return cls(
            discord_webhook_url=_secret("discord_webhook_url"),
            itad_api_key=_secret("itad_api_key"),
            steam_id64=_secret("steam_id64"),
            steam_api_key=_secret("steam_api_key"),
            region=str(merged["region"]),
            currency=str(merged["currency"]),
            min_discount_pct=int(merged["min_discount_pct"]),
            dedup_expiry_days=int(merged["dedup_expiry_days"]),
            ratings_cache_days=int(merged["ratings_cache_days"]),
            wishlist_mention=str(merged["wishlist_mention"]),
            max_deals_per_run=int(merged["max_deals_per_run"]),
            feed_max_deals=int(merged["feed_max_deals"]),
            feed_page_sleep=float(merged["feed_page_sleep"]),
            publisher_metadata_batch=int(merged["publisher_metadata_batch"]),
            preferred_publishers=[str(p) for p in (merged["preferred_publishers"] or [])],
            min_review_pct=int(merged["min_review_pct"]),
            min_metacritic=int(merged["min_metacritic"]),
            preferred_mention=str(merged["preferred_mention"]),
            premium_original_min=int(merged["premium_original_min"]),
            premium_sale_max=int(merged["premium_sale_max"]),
            premium_mode=str(merged["premium_mode"]).lower(),
            premium_min_discount_pct=int(merged["premium_min_discount_pct"]),
            min_original_price=int(merged["min_original_price"]),
            min_review_count=int(merged["min_review_count"]),
            discovery_discount_pct=int(merged["discovery_discount_pct"]),
            exclude_early_access=bool(merged["exclude_early_access"]),
            exclude_genres=[str(g) for g in (merged["exclude_genres"] or [])],
            include_genres=[str(g) for g in (merged["include_genres"] or [])],
            franchises=[str(f) for f in (merged["franchises"] or [])],
            ending_soon_hours=int(merged["ending_soon_hours"]),
            resolve_steam_appids=bool(merged["resolve_steam_appids"]),
            use_appid_index=bool(merged["use_appid_index"]),
            appindex_cache_days=int(merged["appindex_cache_days"]),
            derive_watchlist=bool(merged["derive_watchlist"]),
            catalogue_cache_days=int(merged["catalogue_cache_days"]),
            max_derived_titles=int(merged["max_derived_titles"]),
            all_store_low=bool(merged["all_store_low"]),
            price_drop_window_days=int(merged["price_drop_window_days"]),
            sections=[str(s).lower() for s in (merged["sections"] or VALID_SECTIONS)],
            max_discounts_per_run=int(merged["max_discounts_per_run"]),
            watchlist=[str(t) for t in (merged["watchlist"] or [])],
            stores=[str(s) for s in (merged["stores"] or [])],
            exclude_stores=[str(s) for s in (merged["exclude_stores"] or [])],
        )

    @property
    def fetch_discount_pct(self) -> int:
        """Threshold sources fetch at — lower than ``min_discount_pct`` enables
        the wide-net discovery of premium/preferred sub-threshold deals."""
        if 0 < self.discovery_discount_pct < self.min_discount_pct:
            return self.discovery_discount_pct
        return self.min_discount_pct

    def validate(self) -> list[str]:
        """Return a list of human-readable problems. Never includes any value."""
        problems: list[str] = []
        for attr, (env_name, required) in SECRET_VARS.items():
            if required and not getattr(self, attr):
                problems.append(f"missing required secret {env_name}")
        if not 0 <= self.min_discount_pct <= 100:
            problems.append("min_discount_pct must be between 0 and 100")
        if self.max_deals_per_run <= 0:
            problems.append("max_deals_per_run must be a positive integer")
        if not 50 <= self.feed_max_deals <= 5000:
            problems.append("feed_max_deals must be between 50 and 5000")
        if not 0 <= self.feed_page_sleep <= 5:
            problems.append("feed_page_sleep must be between 0 and 5")
        if self.publisher_metadata_batch <= 0:
            problems.append("publisher_metadata_batch must be a positive integer")
        if self.dedup_expiry_days <= 0:
            problems.append("dedup_expiry_days must be a positive integer")
        if self.ratings_cache_days <= 0:
            problems.append("ratings_cache_days must be a positive integer")
        if self.appindex_cache_days <= 0:
            problems.append("appindex_cache_days must be a positive integer")
        if self.catalogue_cache_days <= 0:
            problems.append("catalogue_cache_days must be a positive integer")
        if self.max_derived_titles < 0:
            problems.append("max_derived_titles must be >= 0")
        if self.price_drop_window_days < 0:
            problems.append("price_drop_window_days must be >= 0")
        if not 0 <= self.min_review_pct <= 100:
            problems.append("min_review_pct must be between 0 and 100")
        if not 0 <= self.min_metacritic <= 100:
            problems.append("min_metacritic must be between 0 and 100")
        if not 0 <= self.discovery_discount_pct <= 100:
            problems.append("discovery_discount_pct must be between 0 and 100")
        if self.min_review_count < 0:
            problems.append("min_review_count must be >= 0")
        if self.premium_mode not in ("absolute", "percent", "either", "both"):
            problems.append("premium_mode must be one of: absolute, percent, either, both")
        if not 1 <= self.premium_min_discount_pct <= 99:
            problems.append("premium_min_discount_pct must be between 1 and 99")
        if self.min_original_price < 0:
            problems.append("min_original_price must be >= 0")
        if self.ending_soon_hours < 0:
            problems.append("ending_soon_hours must be >= 0")
        if self.max_discounts_per_run < 0:
            problems.append("max_discounts_per_run must be >= 0")
        unknown = [s for s in self.sections if s not in VALID_SECTIONS]
        if unknown:
            problems.append(f"unknown sections: {', '.join(unknown)}")
        return problems

    def status_report(self) -> list[tuple[str, str]]:
        """``(label, status)`` pairs for ``--check-config``.

        Secrets report only ``set`` / ``MISSING`` / ``not set (optional)`` —
        never their value. Non-secret settings show their (non-sensitive) value.
        """
        report: list[tuple[str, str]] = []
        for attr, (env_name, required) in SECRET_VARS.items():
            if getattr(self, attr):
                state = "set"
            else:
                state = "MISSING" if required else "not set (optional)"
            report.append((env_name, state))
        for key in DEFAULT_SETTINGS:
            report.append((key, str(getattr(self, key))))
        return report
