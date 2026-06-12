"""AAA watchlist: price-check specific games every run.

Unlike the discount-sorted source feed (which surfaces the deepest discounts —
usually obscure titles), the watchlist guarantees that the games you actually care
about appear the moment they're discounted at *any* percentage. Hits are flagged
``is_preferred`` so they land in the AAA / quality-picks section.

Each title is resolved to a Steam appid (via the shared resolver) and then
price-checked through ``appdetails`` with ``cc=in``. Bounded and throttled.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from src.config import Settings
from src.models import Deal
from src.resolve import SteamAppidResolver
from src.sources.base import DEFAULT_TIMEOUT, build_session

if TYPE_CHECKING:
    from src.appindex import SteamAppIndex

logger = logging.getLogger(__name__)

APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"
_APP_PAGE = "https://store.steampowered.com/app/{appid}"


def _paise(value: object) -> float | None:
    return round(value / 100, 2) if isinstance(value, int | float) else None


class WatchlistChecker:
    def __init__(
        self,
        settings: Settings,
        session=None,
        sleeper: Callable[[float], None] = time.sleep,
        throttle: float = 1.0,
        index: SteamAppIndex | None = None,
    ) -> None:
        self.settings = settings
        self.session = session if session is not None else build_session()
        self._sleep = sleeper
        self._throttle = throttle
        self._resolver = SteamAppidResolver(
            settings, session=self.session, sleeper=sleeper, throttle=throttle, index=index
        )

    def fetch_on_sale(self, extra_titles: list[str] | None = None) -> list[Deal]:
        """Return on-sale watchlist games as preferred (AAA) deals.

        ``extra_titles`` (e.g. publisher-derived titles) are merged with the
        configured watchlist, de-duplicated while preserving order.
        """
        titles = list(dict.fromkeys(self.settings.watchlist + (extra_titles or [])))
        if not titles:
            return []
        deals: list[Deal] = []
        seen_appids: set[int] = set()
        for title in titles[: self.settings.max_deals_per_run]:
            try:
                appid = self._resolver.search_appid(title)
                if appid is None or appid in seen_appids:
                    continue
                seen_appids.add(appid)
                deal = self._price_check(appid, title)
            except Exception as exc:  # noqa: BLE001 - optional feature, never crash
                logger.warning("watch: failed for %r (%s)", title, type(exc).__name__)
                continue
            if deal is not None:
                deals.append(deal)
        return deals

    def _price_check(self, appid: int, fallback_title: str) -> Deal | None:
        self._sleep(self._throttle)
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
            return None  # only when actually on sale

        return Deal(
            title=data.get("name", fallback_title),
            store="Steam",
            url=_APP_PAGE.format(appid=appid),
            source="watchlist",
            source_game_id=str(appid),
            image_url=data.get("header_image"),
            price_old=_paise(price.get("initial")),
            price_new=_paise(price.get("final")),
            currency=price.get("currency", self.settings.currency),
            discount_pct=discount,
            steam_appid=appid,
            is_preferred=True,
        )
