"""AAA watchlist: price-check specific games across all allowed stores every run.

Unlike the discovery feeds (which surface whatever is deeply discounted), the
watchlist guarantees the games you care about appear the moment they're on sale at
*any* store and *any* percentage. Hits are flagged ``is_preferred`` so they land in
the Quality picks section.

Identity is the ITAD game id, not a Steam appid, so a title discounted only on GOG
or Epic still produces an embed for that store. Each title is resolved once to an
ITAD game id (cached for 30 days), then all ids are priced in a single batched
``games/prices/v2`` call; the cheapest on-sale offer from an allowed store wins.

A legacy Steam-``appdetails`` path is kept *only* for the no-ITAD-key case.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from typing import Any

from src.config import Settings
from src.db import Database
from src.models import Deal
from src.quality import store_allowed
from src.sources.base import DEFAULT_TIMEOUT, build_session
from src.sources.itad import (
    ITADSource,
    _select_best_offer,
    _steam_appid_from_url,
)

logger = logging.getLogger(__name__)

APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"
STORESEARCH_URL = "https://store.steampowered.com/api/storesearch/"
_APP_PAGE = "https://store.steampowered.com/app/{appid}"
_TITLE_TTL_DAYS = 30  # re-resolve a watchlist title -> ITAD id after this long
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _paise(value: object) -> float | None:
    return round(value / 100, 2) if isinstance(value, int | float) else None


def _norm(text: str | None) -> str:
    return _NON_ALNUM.sub("", (text or "").lower())


class WatchlistChecker:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        session: Any = None,
        sleeper: Callable[[float], None] = time.sleep,
        throttle: float = 1.0,
    ) -> None:
        self.settings = settings
        self.db = db
        self.session = session if session is not None else build_session()
        self._sleep = sleeper
        self._throttle = throttle
        self._itad = ITADSource(settings, session=self.session)

    def fetch_on_sale(self) -> list[Deal]:
        """Return on-sale watchlist games as preferred (AAA) deals."""
        titles = list(dict.fromkeys(self.settings.watchlist))
        if not titles:
            return []
        if self.settings.itad_api_key:
            return self._fetch_multi_store(titles)
        return self._fetch_steam_legacy(titles)

    # -- ITAD multi-store path (default) -------------------------------------

    def _fetch_multi_store(self, titles: list[str]) -> list[Deal]:
        title_by_id: dict[str, str] = {}
        unresolved = 0
        for title in titles:
            gid = self._resolve_title_id(title)
            if gid:
                title_by_id.setdefault(gid, title)  # first title wins for an id
            else:
                unresolved += 1
        if unresolved:
            logger.warning(
                "watch: %d watchlist title(s) could not be resolved this run", unresolved
            )
        if not title_by_id:
            return []

        offers_by_id = self._itad.fetch_offers(list(title_by_id))
        deals: list[Deal] = []
        for gid, offers in offers_by_id.items():
            on_sale = [
                o for o in offers
                if int(o.get("cut") or 0) > 0 and self._store_ok(o)
            ]
            best = _select_best_offer(on_sale)
            if best is not None:
                deals.append(self._to_deal(gid, title_by_id[gid], best))
        return deals

    def _resolve_title_id(self, title: str) -> str | None:
        """Cache-first resolution of a title to an ITAD game id (30-day refresh)."""
        fresh, gid = self.db.cached_title_id(title, _TITLE_TTL_DAYS)
        if fresh:
            return gid
        if self._throttle:
            self._sleep(self._throttle)
        gid = self._itad.lookup_title_id(title)
        if gid:
            self.db.upsert_title_id(title, gid)
        return gid

    def _store_ok(self, offer: dict) -> bool:
        """Apply the store allow/block lists to an offer's shop (before picking)."""
        name = (offer.get("shop") or {}).get("name") or ""
        probe = Deal(title="", store=name, url="", source="watchlist", source_game_id="")
        return store_allowed(probe, self.settings)

    def _to_deal(self, game_id: str, title: str, best: dict) -> Deal:
        """Build a watchlist Deal from a selected best-offer dict.

        ``best`` is the processed shape returned by :func:`_select_best_offer`
        (``price``/``regular``/``cut``/``shop``/``url``/``is_low``).
        """
        url = best.get("url") or ""
        return Deal(
            title=title,
            store=best.get("shop") or "Unknown",
            url=url,
            source="watchlist",
            source_game_id=game_id,
            itad_game_id=game_id,
            price_old=best.get("regular"),
            price_new=best.get("price"),
            currency=self.settings.currency,
            discount_pct=int(best.get("cut") or 0),
            is_lowest_ever=bool(best.get("is_low")),
            is_preferred=True,
            steam_appid=_steam_appid_from_url(url),
        )

    # -- legacy Steam-appdetails path (only when no ITAD key) ----------------

    def _fetch_steam_legacy(self, titles: list[str]) -> list[Deal]:
        deals: list[Deal] = []
        seen: set[int] = set()
        for title in titles[: self.settings.max_deals_per_run]:
            try:
                appid = self._steam_search(title)
                if appid is None or appid in seen:
                    continue
                seen.add(appid)
                deal = self._steam_price_check(appid, title)
            except Exception as exc:  # noqa: BLE001 - optional feature, never crash
                logger.warning("watch: legacy check failed for %r (%s)", title, type(exc).__name__)
                continue
            if deal is not None:
                deals.append(deal)
        return deals

    def _steam_search(self, title: str) -> int | None:
        self._sleep(self._throttle)
        resp = self.session.get(
            STORESEARCH_URL,
            params={"term": title, "cc": self.settings.region, "l": "en"},
            timeout=DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        wanted = _norm(title)
        for item in ((resp.json() or {}).get("items") or [])[:3]:
            name = _norm(item.get("name"))
            if name and (name == wanted or wanted.startswith(name)):
                appid = item.get("id")
                return int(appid) if appid else None
        return None

    def _steam_price_check(self, appid: int, fallback_title: str) -> Deal | None:
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
