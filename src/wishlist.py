"""Steam wishlist matcher (optional feature).

If ``STEAM_ID64`` is unset the whole feature degrades to "no wishlist" silently.
The user's Steam profile and game details (wishlist) must be **public** for the
read-only wishlist endpoint to return anything; this is a privacy toggle, not a
login — no credentials are ever used.

A wishlist hit is any wishlisted game on sale at *any* discount (it bypasses the
70% threshold). Hits come from two places:
  * deals already discovered by other sources whose ``steam_appid`` is wishlisted
    (handled by :func:`split_wishlist_hits`), and
  * wishlisted games discounted *below* the threshold, found by directly
    price-checking a bounded batch of wishlist appids (:meth:`fetch_on_sale`).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import replace

from src.config import Settings
from src.models import Deal
from src.sources.base import DEFAULT_TIMEOUT, build_session

logger = logging.getLogger(__name__)

GETWISHLIST_URL = "https://api.steampowered.com/IWishlistService/GetWishlist/v1/"
APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"
_APP_PAGE = "https://store.steampowered.com/app/{appid}"


def split_wishlist_hits(
    discounts: list[Deal], wishlist_appids: set[int]
) -> tuple[list[Deal], list[Deal]]:
    """Pull wishlisted games out of the discount list into a wishlist bucket.

    Returns ``(wishlist_hits, remaining_discounts)``. Free games are left in the
    free bucket by the caller (free beats wishlist).
    """
    hits: list[Deal] = []
    remaining: list[Deal] = []
    for deal in discounts:
        if deal.steam_appid is not None and deal.steam_appid in wishlist_appids:
            hits.append(replace(deal, is_wishlist=True))
        else:
            remaining.append(deal)
    return hits, remaining


class WishlistMatcher:
    def __init__(
        self,
        settings: Settings,
        session=None,
        sleeper: Callable[[float], None] = time.sleep,
        throttle: float = 1.0,
    ) -> None:
        self.settings = settings
        self.session = session if session is not None else build_session()
        self._sleep = sleeper
        self._throttle = throttle

    def fetch_appids(self) -> set[int]:
        """Return the set of wishlisted appids, or empty set if unavailable."""
        steam_id = self.settings.steam_id64
        if not steam_id:
            return set()
        try:
            resp = self.session.get(
                GETWISHLIST_URL, params={"steamid": steam_id}, timeout=DEFAULT_TIMEOUT
            )
            resp.raise_for_status()
            items = ((resp.json() or {}).get("response") or {}).get("items") or []
        except Exception as exc:  # noqa: BLE001 - optional feature, never crash the run
            logger.warning("wishlist: fetch failed (%s)", type(exc).__name__)
            return set()
        return {int(item["appid"]) for item in items if item.get("appid") is not None}

    def fetch_on_sale(self, appids: set[int]) -> list[Deal]:
        """Price-check a bounded batch of wishlist appids; return discounted ones.

        This catches wishlist games on sale below the discount threshold that the
        other sources filter out. Bounded by ``max_deals_per_run`` and throttled.
        """
        deals: list[Deal] = []
        for appid in list(appids)[: self.settings.max_deals_per_run]:
            self._sleep(self._throttle)
            try:
                deal = self._price_check(appid)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "wishlist: price check failed for %s (%s)", appid, type(exc).__name__
                )
                continue
            if deal is not None:
                deals.append(deal)
        return deals

    def _price_check(self, appid: int) -> Deal | None:
        resp = self.session.get(
            APPDETAILS_URL,
            params={"appids": appid, "cc": self.settings.region, "l": "en"},
            timeout=DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        entry = (resp.json() or {}).get(str(appid)) or {}
        if not entry.get("success"):
            return None
        data = entry.get("data") or {}
        price = data.get("price_overview")
        if not price:
            return None  # free-to-play or not sold in region
        discount = int(price.get("discount_percent") or 0)
        if discount <= 0:
            return None  # not currently on sale

        return Deal(
            title=data.get("name", f"App {appid}"),
            store="Steam",
            url=_APP_PAGE.format(appid=appid),
            source="wishlist",
            source_game_id=str(appid),
            image_url=data.get("header_image"),
            price_old=_paise(price.get("initial")),
            price_new=_paise(price.get("final")),
            currency=price.get("currency", self.settings.currency),
            discount_pct=discount,
            steam_appid=appid,
            is_wishlist=True,
        )


def _paise(value: object) -> float | None:
    """Steam prices are integer minor units (paise); convert to rupees."""
    return round(value / 100, 2) if isinstance(value, int | float) else None
