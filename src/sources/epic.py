"""Epic Games free-promotions source.

Uses the public ``freeGamesPromotions`` backend endpoint (no key). ``fetch``
returns the games that are free *right now* (``ends_at`` carries the end date).

This is an unofficial endpoint, so every parse step is defensive — a malformed
element is skipped, and any top-level failure returns ``[]``.
"""

from __future__ import annotations

import logging

from src.models import Deal
from src.sources.base import DealSource, parse_iso

logger = logging.getLogger(__name__)

EPIC_URL = "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions"
_STORE_PAGE = "https://store.epicgames.com/en-US/p/{slug}"
_IMAGE_PREFERENCE = ("OfferImageWide", "DieselStoreFrontWide", "OfferImageTall", "Thumbnail")


class EpicSource(DealSource):
    name = "epic"

    def fetch(self) -> list[Deal]:
        params = {
            "locale": "en-US",
            "country": self.settings.region,
            "allowCountries": self.settings.region,
        }
        try:
            data = self._get_json(EPIC_URL, params=params)
            elements = data["data"]["Catalog"]["searchStore"]["elements"]
        except Exception as exc:  # noqa: BLE001 - source must never raise
            logger.warning("epic: fetch/parse failed (%s)", type(exc).__name__)
            return []

        deals: list[Deal] = []
        for element in elements:
            try:
                deal = self._parse_element(element)
            except Exception as exc:  # noqa: BLE001
                logger.warning("epic: skipping element (%s)", type(exc).__name__)
                continue
            if deal is not None:
                deals.append(deal)
        return deals

    def _parse_element(self, element: dict) -> Deal | None:
        promotions = element.get("promotions") or {}
        offer = self._first_free_offer(promotions.get("promotionalOffers") or [])
        if offer is None:
            return None

        total = (element.get("price") or {}).get("totalPrice") or {}
        original = total.get("originalPrice")
        price_old = round(original / 100, 2) if isinstance(original, int | float) else None
        currency = total.get("currencyCode") or self.settings.currency

        slug = self._slug(element)
        url = _STORE_PAGE.format(slug=slug) if slug else "https://store.epicgames.com/"
        seller = (element.get("seller") or {}).get("name")
        publisher = self._attr(element, "publisherName") or seller

        return Deal(
            title=element.get("title", "Unknown"),
            store="Epic Games",
            url=url,
            source=self.name,
            source_game_id=str(element.get("id") or slug or element.get("title")),
            image_url=self._image(element),
            price_old=price_old,
            price_new=0.0,
            currency=currency,
            discount_pct=100,
            is_free=True,
            publisher=publisher,
            developer=self._attr(element, "developerName"),
            ends_at=parse_iso(offer.get("endDate")),
        )

    @staticmethod
    def _attr(element: dict, key: str) -> str | None:
        for attr in (element.get("customAttributes") or []):
            if attr.get("key") == key and attr.get("value"):
                return attr["value"]
        return None

    @staticmethod
    def _first_free_offer(groups: list) -> dict | None:
        """Return the first offer whose discount makes the game free (0%)."""
        for group in groups:
            for offer in (group.get("promotionalOffers") or []):
                setting = offer.get("discountSetting") or {}
                if setting.get("discountPercentage") == 0:
                    return offer
        return None

    @staticmethod
    def _slug(element: dict) -> str | None:
        for mapping in ((element.get("catalogNs") or {}).get("mappings") or []):
            if mapping.get("pageSlug"):
                return mapping["pageSlug"]
        return element.get("productSlug")

    @staticmethod
    def _image(element: dict) -> str | None:
        images = {img.get("type"): img.get("url") for img in (element.get("keyImages") or [])}
        for preferred in _IMAGE_PREFERENCE:
            if images.get(preferred):
                return images[preferred]
        return next((url for url in images.values() if url), None)
