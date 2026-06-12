"""Steam review summary + Metacritic enrichment with a DB cache.

For each Deal that has a ``steam_appid`` we attach:
  * review summary + % positive + review count (Steam ``appreviews``), and
  * Metacritic score + canonical header image (Steam ``appdetails``).

Results are cached in the ``games`` table and reused while fresh
(``ratings_cache_days``), so repeat runs make far fewer HTTP calls. Steam is
throttled to ~1 request/second and at most ``max_deals_per_run`` games are
enriched per run, free/wishlist deals first. Enrichment never blocks the run:
any failure leaves the Deal usable with ``None`` rating fields.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from src.config import Settings
from src.db import Database
from src.models import Deal
from src.sources.base import DEFAULT_TIMEOUT, build_session, parse_iso

logger = logging.getLogger(__name__)

APPREVIEWS_URL = "https://store.steampowered.com/appreviews/{appid}"
APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"


class RatingsEnricher:
    name = "ratings"

    def __init__(
        self,
        settings: Settings,
        db: Database,
        session=None,
        sleeper: Callable[[float], None] = time.sleep,
        throttle: float = 1.0,
    ) -> None:
        self.settings = settings
        self.db = db
        self.session = session if session is not None else build_session()
        self._sleep = sleeper
        self._throttle = throttle

    def enrich(self, deals: list[Deal]) -> list[Deal]:
        """Return deals with rating fields populated where available (order kept)."""
        ratings_by_appid: dict[int, dict | None] = {}
        budget = self.settings.max_deals_per_run

        for deal in sorted(deals, key=self._priority):
            appid = deal.steam_appid
            if appid is None or appid in ratings_by_appid:
                continue
            if budget <= 0:
                break
            budget -= 1
            ratings_by_appid[appid] = self._get_ratings(appid)

        return [
            self._apply(deal, ratings_by_appid.get(deal.steam_appid) if deal.steam_appid else None)
            for deal in deals
        ]

    @staticmethod
    def _priority(deal: Deal) -> int:
        # Free and wishlist deals are enriched first, within the per-run budget.
        return 0 if (deal.is_free or deal.is_wishlist) else 1

    def _get_ratings(self, appid: int) -> dict | None:
        cached = self.db.get_cached_game(appid)
        # `publisher is None` marks a row created before publisher/developer were
        # captured; refetch once to populate it even if otherwise still fresh.
        if cached and self._is_fresh(cached) and cached.get("publisher") is not None:
            return cached

        fetched = self._fetch_ratings(appid)
        if fetched is None:
            return cached  # total failure: keep stale cache if any, else None

        self.db.upsert_game(appid, **fetched)
        return fetched

    def _is_fresh(self, cached: dict) -> bool:
        fetched_at = parse_iso(cached.get("fetched_at"))
        if fetched_at is None:
            return False
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=UTC)
        age = datetime.now(UTC) - fetched_at
        return age < timedelta(days=self.settings.ratings_cache_days)

    def _fetch_ratings(self, appid: int) -> dict | None:
        """Hit Steam (throttled). Return a rating dict, or ``None`` if nothing came back."""
        self._sleep(self._throttle)
        summary, pct, count = self._fetch_reviews(appid)
        details = self._fetch_details(appid)
        if summary is None and not details:
            return None
        return {
            "title": details.get("title"),
            "review_summary": summary,
            "review_pct": pct,
            "review_count": count,
            "metacritic": details.get("metacritic"),
            "image_url": details.get("header_image"),
            # Empty string (not None) marks "fetched, none listed" so we don't refetch.
            "publisher": details.get("publisher", ""),
            "developer": details.get("developer", ""),
            "genres": details.get("genres", ""),
            "coming_soon": details.get("coming_soon", False),
        }

    def _fetch_reviews(self, appid: int) -> tuple[str | None, int | None, int | None]:
        try:
            resp = self.session.get(
                APPREVIEWS_URL.format(appid=appid),
                params={"json": "1", "language": "all", "purchase_type": "all"},
                timeout=DEFAULT_TIMEOUT,
            )
            resp.raise_for_status()
            summary = (resp.json() or {}).get("query_summary") or {}
        except Exception as exc:  # noqa: BLE001 - enrichment must never raise
            logger.warning("ratings: reviews failed for %s (%s)", appid, type(exc).__name__)
            return None, None, None

        total = summary.get("total_reviews") or 0
        positive = summary.get("total_positive") or 0
        pct = round(positive / total * 100) if total else None
        return summary.get("review_score_desc"), pct, (total or None)

    def _fetch_details(self, appid: int) -> dict:
        """Return appdetails fields (metacritic, image, title, publisher, developer).

        Returns an empty dict on failure or when the app has no usable data.
        """
        try:
            resp = self.session.get(
                APPDETAILS_URL,
                params={"appids": appid, "cc": self.settings.region, "l": "en"},
                timeout=DEFAULT_TIMEOUT,
            )
            resp.raise_for_status()
            entry = (resp.json() or {}).get(str(appid)) or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("ratings: details failed for %s (%s)", appid, type(exc).__name__)
            return {}

        if not entry.get("success"):
            return {}
        data = entry.get("data") or {}
        genres = [g.get("description", "") for g in (data.get("genres") or [])]
        return {
            "metacritic": (data.get("metacritic") or {}).get("score"),
            "header_image": data.get("header_image"),
            "title": data.get("name"),
            "publisher": ", ".join(data.get("publishers") or []),
            "developer": ", ".join(data.get("developers") or []),
            "genres": ", ".join(g for g in genres if g),
            "coming_soon": bool((data.get("release_date") or {}).get("coming_soon")),
        }

    @staticmethod
    def _apply(deal: Deal, ratings: dict | None) -> Deal:
        if not ratings:
            return deal

        # Prefer freshly-enriched Steam values, but fall back to whatever the deal
        # already carried (e.g. CheapShark's rating data) when enrichment is blank.
        def pick(key: str, current: Any) -> Any:
            value = ratings.get(key)
            return value if value is not None else current

        return replace(
            deal,
            review_summary=pick("review_summary", deal.review_summary),
            review_pct=pick("review_pct", deal.review_pct),
            review_count=pick("review_count", deal.review_count),
            metacritic=pick("metacritic", deal.metacritic),
            image_url=deal.image_url or ratings.get("image_url"),
            publisher=deal.publisher or (ratings.get("publisher") or None),
            developer=deal.developer or (ratings.get("developer") or None),
            genres=deal.genres or (ratings.get("genres") or None),
            coming_soon=deal.coming_soon or bool(ratings.get("coming_soon")),
        )
