"""CheapShark cross-store discount discovery.

CheapShark aggregates deals across many stores with a discount percentage and no
auth, which makes it ideal for *finding* heavily-discounted games. Its prices are
USD, however, so every Deal here is flagged ``needs_inr_verify=True`` and must be
re-priced in INR (via ITAD) before it is ever reported. This keeps the India
region lock honest.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

import requests

from src.config import Settings
from src.models import Deal
from src.sources.base import DEFAULT_TIMEOUT, DealSource

logger = logging.getLogger(__name__)

CHEAPSHARK_DEALS_URL = "https://www.cheapshark.com/api/1.0/deals"
CHEAPSHARK_STORES_URL = "https://www.cheapshark.com/api/1.0/stores"
_REDIRECT_URL = "https://www.cheapshark.com/redirect?dealID={deal_id}"

_PAGE_SIZE = 60  # CheapShark hard cap; larger values are silently clamped
_TOTAL_PAGES_HEADER = "X-Total-Page-Count"


class CheapSharkSource(DealSource):
    name = "cheapshark"

    def __init__(
        self,
        settings: Settings,
        session: requests.Session | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(settings, session)
        self._sleep = sleeper

    def fetch(self) -> list[Deal]:
        """Page through CheapShark deals (savings-sorted, on-sale) for allowed stores.

        Each store in the allowlist is paginated separately (the API filters one
        ``storeID`` at a time) to save quota; with no allowlist a single all-store
        sweep runs. Bounded by ``feed_max_deals`` across the whole run.
        """
        store_map = self._store_names()
        store_ids = self._allowed_store_ids(store_map)
        targets: list[str | None] = list(store_ids) if store_ids else [None]

        deals: list[Deal] = []
        for store_id in targets:
            if len(deals) >= self.settings.feed_max_deals:
                break
            budget = self.settings.feed_max_deals - len(deals)
            deals.extend(self._fetch_store(store_id, store_map, budget))
        result = deals[: self.settings.feed_max_deals]
        logger.info("cheapshark: fetched %d deals", len(result))
        return result

    def _fetch_store(
        self, store_id: str | None, store_map: dict[str, str], budget: int
    ) -> list[Deal]:
        """Paginate one store (or all stores when ``store_id`` is None)."""
        out: list[Deal] = []
        page = 0
        while len(out) < budget:
            params: dict[str, str | int] = {
                "sortBy": "Savings",
                "onSale": 1,
                "pageSize": _PAGE_SIZE,
                "pageNumber": page,
            }
            if store_id is not None:
                params["storeID"] = store_id
            if self.settings.min_review_pct > 0:
                params["steamRating"] = self.settings.min_review_pct
            try:
                resp = self.session.get(
                    CHEAPSHARK_DEALS_URL, params=params, timeout=DEFAULT_TIMEOUT
                )
                resp.raise_for_status()
                raw = resp.json()
                total_pages = _to_int(resp.headers.get(_TOTAL_PAGES_HEADER))
            except Exception as exc:  # noqa: BLE001 - source must never raise
                logger.warning("cheapshark: fetch/parse failed at page %d (%s)", page,
                                type(exc).__name__)
                break
            if not isinstance(raw, list) or not raw:
                break

            page_min_savings: float | None = None
            for item in raw:
                savings = _to_float(item.get("savings"))
                if savings is not None and (page_min_savings is None or savings < page_min_savings):
                    page_min_savings = savings
                try:
                    deal = self._parse_item(item, store_map)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("cheapshark: skipping item (%s)", type(exc).__name__)
                    continue
                if deal is not None:
                    out.append(deal)

            # Savings-sorted descending: once the page's minimum drops below the
            # discovery threshold, deeper pages can only be shallower — stop.
            if (
                len(raw) < _PAGE_SIZE
                or (page_min_savings is not None
                    and page_min_savings < self.settings.fetch_discount_pct)
                or (total_pages is not None and page + 1 >= total_pages)
                or len(out) >= budget
            ):
                break
            page += 1
            self._sleep(self.settings.feed_page_sleep)
        return out

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

    def _allowed_store_ids(self, store_map: dict[str, str]) -> list[str]:
        """CheapShark storeIDs whose name matches the configured allowlist.

        Same case-insensitive substring semantics as ``quality.store_allowed``. An
        empty allowlist (or unknown store map) yields ``[]`` -> all-store sweep.
        """
        if not self.settings.stores or not store_map:
            return []
        wanted = [s.strip().lower() for s in self.settings.stores if s.strip()]
        return [
            sid for sid, name in store_map.items()
            if any(w in (name or "").lower() for w in wanted)
        ]

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
