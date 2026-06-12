"""Best-effort Steam appid resolution for non-Steam deals (opt-in).

Searches the Steam storefront by title so deals from GOG/Epic/Fanatical can still
gain ratings and preferred-publisher classification. Matching is conservative (to
avoid attaching the wrong appid), bounded by ``max_deals_per_run``, and throttled.
Disabled unless ``resolve_steam_appids`` is set.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING

from src.config import Settings
from src.models import Deal
from src.sources.base import DEFAULT_TIMEOUT, build_session

if TYPE_CHECKING:
    from src.appindex import SteamAppIndex

logger = logging.getLogger(__name__)

STORESEARCH_URL = "https://store.steampowered.com/api/storesearch/"
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _norm(text: str | None) -> str:
    return _NON_ALNUM.sub("", (text or "").lower())


class SteamAppidResolver:
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
        self._index = index

    def resolve(self, deals: list[Deal]) -> list[Deal]:
        """Fill ``steam_appid`` for deals that lack one (paid deals only).

        The ``max_deals_per_run`` budget bounds online searches only; free index
        hits never count against it, so they resolve even after the budget runs out.
        """
        if not self.settings.resolve_steam_appids:
            return deals
        budget = self.settings.max_deals_per_run
        out: list[Deal] = []
        for deal in deals:
            if deal.steam_appid is not None or deal.is_free:
                out.append(deal)
                continue
            appid = self._index.lookup(deal.title) if self._index is not None else None
            if appid is None and budget > 0:
                budget -= 1
                appid = self._online_search(deal.title)
            out.append(replace(deal, steam_appid=appid) if appid else deal)
        return out

    def search_appid(self, title: str) -> int | None:
        """Public title->appid search (used by the watchlist).

        Offline first: a confident hit in the local index skips the HTTP call.
        """
        if self._index is not None:
            hit = self._index.lookup(title)
            if hit is not None:
                return hit
        return self._online_search(title)

    def _online_search(self, title: str) -> int | None:
        """Resolve a title via the Steam storefront search (one throttled call)."""
        try:
            self._sleep(self._throttle)
            resp = self.session.get(
                STORESEARCH_URL,
                params={"term": title, "cc": self.settings.region, "l": "en"},
                timeout=DEFAULT_TIMEOUT,
            )
            resp.raise_for_status()
            items = (resp.json() or {}).get("items") or []
        except Exception as exc:  # noqa: BLE001 - best-effort, never crash the run
            logger.warning("resolve: search failed for %r (%s)", title, type(exc).__name__)
            return None

        wanted = _norm(title)
        for item in items[:3]:
            name = _norm(item.get("name"))
            # Accept an exact match, or where the deal title is an edition of the
            # search result (e.g. "Elden Ring Deluxe" startswith "Elden Ring").
            if name and (name == wanted or wanted.startswith(name)):
                appid = item.get("id")
                return int(appid) if appid else None
        return None
