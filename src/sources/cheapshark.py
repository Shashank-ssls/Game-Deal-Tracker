"""CheapShark cross-store discount discovery.

CheapShark aggregates deals across many stores with a discount percentage and no
auth, which makes it ideal for *finding* heavily-discounted games. Its prices are
USD, however, so every Deal here is flagged ``needs_inr_verify=True`` and must be
re-priced in INR (via ITAD) before it is ever reported. This keeps the India
region lock honest.
"""

from __future__ import annotations

import logging
from typing import Any

from src.models import Deal
from src.sources.base import DealSource

logger = logging.getLogger(__name__)

CHEAPSHARK_DEALS_URL = "https://www.cheapshark.com/api/1.0/deals"
CHEAPSHARK_STORES_URL = "https://www.cheapshark.com/api/1.0/stores"
_REDIRECT_URL = "https://www.cheapshark.com/redirect?dealID={deal_id}"


class CheapSharkSource(DealSource):
    name = "cheapshark"

    def fetch(self) -> list[Deal]:
        # Sort by "Deal Rating" (balances discount + popularity) instead of pure
        # savings, so the pool is good games on sale rather than obscure deep
        # discounts. Bias toward the quality bar with the steamRating filter.
        params: dict[str, object] = {
            "sortBy": "Deal Rating",
            "pageSize": self.settings.max_deals_per_run,
        }
        if self.settings.min_review_pct > 0:
            params["steamRating"] = self.settings.min_review_pct
        try:
            stores = self._store_names()
            raw = self._get_json(CHEAPSHARK_DEALS_URL, params=params)
        except Exception as exc:  # noqa: BLE001 - source must never raise
            logger.warning("cheapshark: fetch/parse failed (%s)", type(exc).__name__)
            return []

        if not isinstance(raw, list):
            return []

        deals: list[Deal] = []
        for item in raw:
            try:
                deal = self._parse_item(item, stores)
            except Exception as exc:  # noqa: BLE001
                logger.warning("cheapshark: skipping item (%s)", type(exc).__name__)
                continue
            if deal is not None:
                deals.append(deal)
        return deals

    def _store_names(self) -> dict[str, str]:
        """Map ``storeID`` -> store name (cached for the duration of this call)."""
        try:
            stores = self._get_json(CHEAPSHARK_STORES_URL)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cheapshark: store lookup failed (%s)", type(exc).__name__)
            return {}
        if not isinstance(stores, list):
            return {}
        return {str(s.get("storeID")): s.get("storeName", "Unknown") for s in stores}

    def _parse_item(self, item: dict, stores: dict[str, str]) -> Deal | None:
        savings = float(item.get("savings") or 0)
        if savings < self.settings.fetch_discount_pct:
            return None

        store_id = str(item.get("storeID"))
        deal_id = item.get("dealID")
        steam_appid = item.get("steamAppID")
        return Deal(
            title=item.get("title", "Unknown"),
            store=stores.get(store_id, f"Store {store_id}"),
            url=_REDIRECT_URL.format(deal_id=deal_id),
            source=self.name,
            source_game_id=str(item.get("gameID") or deal_id),
            image_url=item.get("thumb"),
            price_old=_to_float(item.get("normalPrice")),
            price_new=_to_float(item.get("salePrice")),
            currency="USD",
            discount_pct=round(savings),
            needs_inr_verify=True,
            steam_appid=int(steam_appid) if steam_appid else None,
            # CheapShark ships Steam rating + Metacritic, so the quality gate can
            # act immediately without an extra Steam call (enrichment may refine).
            review_pct=_to_int(item.get("steamRatingPercent")),
            review_count=_to_int(item.get("steamRatingCount")),
            review_summary=item.get("steamRatingText") or None,
            metacritic=_to_int(item.get("metacriticScore")),
        )


def _to_float(value: Any) -> float | None:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    try:
        result = int(float(value))
    except (TypeError, ValueError):
        return None
    return result or None
