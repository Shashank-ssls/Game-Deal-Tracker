"""Steam storefront free-games source.

Phase 2 handles *free* games only (paid discounts arrive in Phase 3). Uses the
keyless ``featuredcategories`` endpoint with ``cc=in&l=en`` so prices are INR.

Two kinds of freebie are recognised:
  * 100%-off promos  -> keepable (``is_temporary=False``).
  * free weekends    -> temporary (``is_temporary=True``), detected by name, since
    the storefront feed exposes no explicit free-weekend flag.
"""

from __future__ import annotations

import logging

from src.models import Deal
from src.sources.base import DealSource

logger = logging.getLogger(__name__)

STEAM_FEATURED_URL = "https://store.steampowered.com/api/featuredcategories"
_APP_PAGE = "https://store.steampowered.com/app/{appid}"


class SteamSource(DealSource):
    name = "steam"

    def fetch(self) -> list[Deal]:
        params = {"cc": self.settings.region, "l": "en"}
        try:
            data = self._get_json(STEAM_FEATURED_URL, params=params)
        except Exception as exc:  # noqa: BLE001 - source must never raise
            logger.warning("steam: fetch/parse failed (%s)", type(exc).__name__)
            return []

        items = (data.get("specials") or {}).get("items") if isinstance(data, dict) else None
        if not items:
            return []

        deals: list[Deal] = []
        for item in items:
            try:
                deal = self._parse_item(item)
            except Exception as exc:  # noqa: BLE001
                logger.warning("steam: skipping item (%s)", type(exc).__name__)
                continue
            if deal is not None:
                deals.append(deal)
        logger.info("steam: fetched %d deals", len(deals))
        return deals

    def _parse_item(self, item: dict) -> Deal | None:
        appid = item.get("id")
        if appid is None:
            return None

        name = item.get("name", "Unknown")
        discount = int(item.get("discount_percent") or 0)
        original = item.get("original_price")
        final = item.get("final_price")

        is_free_weekend = "free weekend" in name.lower()
        keepable_free = discount == 100 or (final == 0 and (original or 0) > 0)
        if not is_free_weekend and not keepable_free:
            return None  # a paid discount -> Phase 3, not a freebie

        price_old = round(original / 100, 2) if isinstance(original, int | float) else None
        return Deal(
            title=name,
            store="Steam",
            url=_APP_PAGE.format(appid=appid),
            source=self.name,
            source_game_id=str(appid),
            image_url=item.get("header_image"),
            price_old=price_old,
            price_new=0.0,
            currency=item.get("currency") or self.settings.currency,
            discount_pct=100,
            is_free=True,
            is_temporary=is_free_weekend,
            ends_at=None,
            steam_appid=int(appid),
        )
