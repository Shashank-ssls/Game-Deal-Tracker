"""IsThereAnyDeal source + INR price verifier.

Two roles (see ARCHITECTURE.md):
  * :meth:`ITADSource.fetch` returns ITAD's own region-IN deals at/above the
    discount threshold (already INR).
  * :meth:`ITADSource.verify_inr` takes USD discovery deals (from CheapShark),
    re-prices them in INR, and drops any that fall below threshold or aren't
    purchasable in IN — the region-lock gate.

Auth is the ITAD **API key** (non-user data), passed as the ``key`` query param.
If no key is configured every method degrades to "return nothing" so the rest of
the run (Epic/Steam freebies) still works.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import replace

import requests

from src.config import Settings
from src.models import Deal
from src.sources.base import DEFAULT_TIMEOUT, DealSource, parse_iso

logger = logging.getLogger(__name__)

ITAD_DEALS_URL = "https://api.isthereanydeal.com/deals/v2"
ITAD_LOOKUP_URL = "https://api.isthereanydeal.com/games/lookup/v1"
ITAD_LOOKUP_SHOP_URL = "https://api.isthereanydeal.com/lookup/id/shop/{shop_id}/v1"
ITAD_PRICES_URL = "https://api.isthereanydeal.com/games/prices/v2"
ITAD_STORELOW_URL = "https://api.isthereanydeal.com/games/storelow/v2"

_STORELOW_BATCH = 100  # max game ids per storelow / prices / lookup request
_FEED_PAGE_LIMIT = 200  # ITAD deals/v2 page size (API courtesy cap)
STEAM_SHOP_ID = 61  # ITAD shop id for Steam (for app/<appid> -> game-id lookup)

# Numeric ITAD shop IDs (from the service/shops/v1 endpoint) for the stores this
# project supports. Keys are lowercase labels matched against the configured store
# names with the same case-insensitive substring semantics as quality.store_allowed.
ITAD_SHOP_IDS: dict[str, int] = {
    "steam": 61,
    "gog": 35,
    "epic": 16,  # "Epic Game Store"
}


def shop_ids_for_stores(stores: list[str]) -> list[int]:
    """Translate configured store names into ITAD shop IDs.

    An empty ``stores`` list yields ``[]`` (meaning "no shops filter"). Names with
    no known mapping are logged and skipped so an unknown store never silently
    widens the filter.
    """
    ids: list[int] = []
    for name in stores:
        token = (name or "").strip().lower()
        if not token:
            continue
        match = next(
            (sid for label, sid in ITAD_SHOP_IDS.items() if label in token or token in label),
            None,
        )
        if match is None:
            logger.warning("itad: no shop-id mapping for store %r; not added to shops filter", name)
            continue
        if match not in ids:
            ids.append(match)
    return ids

_STEAM_APP_RE = re.compile(r"store\.steampowered\.com/app/(\d+)")


def _steam_appid_from_url(url: object) -> int | None:
    """Extract a Steam appid from a store URL, enabling cross-source dedup."""
    match = _STEAM_APP_RE.search(str(url or ""))
    return int(match.group(1)) if match else None


def _is_at_low(price: float | None, store_low: dict | None) -> bool:
    """True when the current price matches or beats the store's historical low."""
    low = _amount(store_low or {})
    return price is not None and low is not None and price <= low


class ITADSource(DealSource):
    name = "itad"

    def __init__(
        self,
        settings: Settings,
        session: requests.Session | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(settings, session)
        self._sleep = sleeper

    @property
    def _key(self) -> str | None:
        return self.settings.itad_api_key

    # -- native ITAD deals (paginated, shop-filtered) ------------------------

    def fetch(self) -> list[Deal]:
        """Page through ITAD's deal database for the configured shops.

        Pages ``offset=0,200,...`` sorted by deepest cut, stopping when the feed is
        exhausted, the cut drops below the discovery threshold, or ``feed_max_deals``
        is reached. The shops filter (Stage 1) bounds the run to the user's stores.
        """
        if not self._key:
            logger.info("itad: no API key configured; skipping ITAD deals")
            return []
        shops = shop_ids_for_stores(self.settings.stores)
        cap = self.settings.feed_max_deals
        deals: list[Deal] = []
        offset = 0
        while len(deals) < cap:
            params: dict[str, object] = {
                "key": self._key,
                "country": self.settings.region,
                "sort": "-cut",
                "limit": _FEED_PAGE_LIMIT,
                "offset": offset,
                "nondeals": "false",
                "mature": "false",
            }
            if shops:
                params["shops"] = ",".join(str(s) for s in shops)
            try:
                data = self._get_json(ITAD_DEALS_URL, params=params)
                entries = data.get("list") or []
            except Exception as exc:  # noqa: BLE001 - source must never raise
                logger.warning(
                    "itad: fetch/parse failed at offset %d (%s)", offset, type(exc).__name__
                )
                break
            if not entries:
                break
            for entry in entries:
                try:
                    deal = self._parse_entry(entry)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("itad: skipping entry (%s)", type(exc).__name__)
                    continue
                if deal is not None:
                    deals.append(deal)

            last_cut = int((entries[-1].get("deal") or {}).get("cut") or 0)
            if (
                len(entries) < _FEED_PAGE_LIMIT
                or data.get("hasMore") is False
                or last_cut < self.settings.fetch_discount_pct
                or len(deals) >= cap
            ):
                break
            offset += _FEED_PAGE_LIMIT
            self._sleep(self.settings.feed_page_sleep)
        result = deals[:cap]
        logger.info("itad: fetched %d deals", len(result))
        return result

    def _parse_entry(self, entry: dict) -> Deal | None:
        # Drop non-game entries (bundles, packages, DLC) — these are noise like
        # certification/eLearning bundles, not games. Untyped entries are kept.
        if entry.get("type") not in (None, "game"):
            return None
        deal = entry.get("deal") or {}
        cut = int(deal.get("cut") or 0)
        if cut < self.settings.fetch_discount_pct:
            return None
        price = deal.get("price") or {}
        regular = deal.get("regular") or {}
        price_new = _amount(price)
        url = deal.get("url", "")
        game_id = entry.get("id")
        return Deal(
            title=entry.get("title", "Unknown"),
            store=(deal.get("shop") or {}).get("name", "Unknown"),
            url=url,
            source=self.name,
            source_game_id=str(game_id or entry.get("slug") or entry.get("title")),
            itad_game_id=str(game_id) if game_id else None,
            image_url=(entry.get("assets") or {}).get("boxart"),
            price_old=_amount(regular),
            price_new=price_new,
            currency=price.get("currency", self.settings.currency),
            discount_pct=cut,
            is_free=price_new == 0,
            is_lowest_ever=_is_at_low(price_new, deal.get("storeLow")),
            steam_appid=_steam_appid_from_url(url),
            ends_at=parse_iso(deal.get("expiry")),
        )

    # -- all-store historical low --------------------------------------------

    def flag_all_store_lows(self, deals: list[Deal]) -> list[Deal]:
        """Re-flag ``is_lowest_ever`` using the all-store, all-time low (ITAD).

        Only ITAD-native deals are refined (they carry their ITAD game id as
        ``source_game_id``); a deal is flagged when its price is at/below the
        lowest it has ever reached across *any* store. Best-effort and opt-in.
        """
        if not self.settings.all_store_low or not self._key:
            return deals
        ids = [
            d.source_game_id for d in deals
            if d.source == self.name and d.price_new is not None and not d.is_free
        ]
        if not ids:
            return deals
        lows = self._fetch_all_store_lows(list(dict.fromkeys(ids)))
        if not lows:
            return deals
        out: list[Deal] = []
        for deal in deals:
            low = lows.get(deal.source_game_id) if deal.source == self.name else None
            if low is not None and deal.price_new is not None:
                out.append(replace(deal, is_lowest_ever=deal.price_new <= low))
            else:
                out.append(deal)
        return out

    def _fetch_all_store_lows(self, game_ids: list[str]) -> dict[str, float]:
        """Map each game id to its lowest price ever across all stores."""
        lows: dict[str, float] = {}
        for start in range(0, len(game_ids), _STORELOW_BATCH):
            batch = game_ids[start : start + _STORELOW_BATCH]
            try:
                resp = self.session.post(
                    ITAD_STORELOW_URL,
                    params={"key": self._key, "country": self.settings.region},
                    json=batch,
                    timeout=DEFAULT_TIMEOUT,
                )
                resp.raise_for_status()
                payload = resp.json() or []
            except Exception as exc:  # noqa: BLE001 - optional, never crash the run
                logger.warning("itad: storelow failed (%s)", type(exc).__name__)
                continue
            for entry in payload:
                gid = entry.get("id")
                amounts = [
                    a for a in (_amount(low.get("price") or {}) for low in entry.get("lows") or [])
                    if a is not None
                ]
                if gid and amounts:
                    lows[gid] = min(amounts)
        return lows

    # -- INR verification of USD discovery deals (batched) -------------------

    def verify_inr(self, deals: list[Deal]) -> list[Deal]:
        """Re-price USD deals in INR; drop sub-threshold / IN-unavailable ones.

        Resolves ITAD game ids in bulk (steam appids first, capped title fallback),
        then prices them 100-at-a-time — so an 800-deal feed costs a handful of HTTP
        calls instead of two per deal.
        """
        to_verify = [d for d in deals if d.needs_inr_verify]
        passthrough = [d for d in deals if not d.needs_inr_verify]
        if not to_verify:
            return passthrough
        if not self._key:
            logger.info("itad: no API key; cannot verify INR, dropping %d deal(s)", len(to_verify))
            return passthrough

        game_ids = self._resolve_game_ids(to_verify)
        unique_ids = list(dict.fromkeys(gid for gid in game_ids if gid))
        prices = self._batch_prices(unique_ids)

        verified = list(passthrough)
        for deal, gid in zip(to_verify, game_ids, strict=True):
            if not gid:
                continue
            offer = prices.get(gid)
            if offer is None:  # not purchasable in region
                continue
            if offer["cut"] < self.settings.min_discount_pct:
                continue
            verified.append(
                replace(
                    deal,
                    price_old=offer["regular"],
                    price_new=offer["price"],
                    currency=self.settings.currency,
                    discount_pct=int(offer["cut"]),
                    needs_inr_verify=False,
                    is_lowest_ever=offer["is_low"],
                    store=offer["shop"] or deal.store,
                    url=offer["url"] or deal.url,
                    steam_appid=deal.steam_appid or _steam_appid_from_url(offer["url"]),
                    itad_game_id=gid,
                )
            )
        return verified

    def _resolve_game_ids(self, deals: list[Deal]) -> list[str | None]:
        """ITAD game id per deal (aligned to ``deals``).

        Steam appids are resolved in bulk via the shop-id lookup; the remaining
        titles fall back to per-title lookups, capped at ``max_deals_per_run``.
        """
        result: list[str | None] = [None] * len(deals)

        appid_keys = {
            i: f"app/{d.steam_appid}" for i, d in enumerate(deals) if d.steam_appid
        }
        if appid_keys:
            mapping = self._lookup_shop_ids(STEAM_SHOP_ID, list(dict.fromkeys(appid_keys.values())))
            for i, key in appid_keys.items():
                result[i] = mapping.get(key)

        budget = self.settings.max_deals_per_run
        for i, deal in enumerate(deals):
            if result[i] is not None or budget <= 0:
                continue
            budget -= 1
            result[i] = self.lookup_title_id(deal.title)
        return result

    def _lookup_shop_ids(self, shop_id: int, keys: list[str]) -> dict[str, str]:
        """Bulk-map shop game keys (e.g. ``app/220``) to ITAD game ids."""
        out: dict[str, str] = {}
        url = ITAD_LOOKUP_SHOP_URL.format(shop_id=shop_id)
        for start in range(0, len(keys), _STORELOW_BATCH):
            batch = keys[start : start + _STORELOW_BATCH]
            try:
                resp = self.session.post(
                    url, params={"key": self._key}, json=batch, timeout=DEFAULT_TIMEOUT
                )
                resp.raise_for_status()
                payload = resp.json() or {}
            except Exception as exc:  # noqa: BLE001 - degrade to title fallback
                logger.warning("itad: shop-id lookup failed (%s)", type(exc).__name__)
                continue
            out.update({key: gid for key, gid in payload.items() if gid})
        return out

    def lookup_title_id(self, title: str) -> str | None:
        """Resolve one title to an ITAD game id (best-effort, throttled fallback)."""
        if not self._key:
            return None
        try:
            data = self._get_json(ITAD_LOOKUP_URL, params={"key": self._key, "title": title})
        except Exception as exc:  # noqa: BLE001
            logger.warning("itad: title lookup failed for %r (%s)", title, type(exc).__name__)
            return None
        if isinstance(data, dict) and data.get("found"):
            return (data.get("game") or {}).get("id")
        return None

    def fetch_offers(self, game_ids: list[str]) -> dict[str, list[dict]]:
        """Map each game id to its raw INR offers (``games/prices/v2``, 100/call).

        Callers that only want the cheapest offer use :func:`_select_best_offer`;
        the watchlist filters offers by store first, so it needs the full list.
        """
        out: dict[str, list[dict]] = {}
        if not self._key:
            return out
        for start in range(0, len(game_ids), _STORELOW_BATCH):
            batch = game_ids[start : start + _STORELOW_BATCH]
            try:
                resp = self.session.post(
                    ITAD_PRICES_URL,
                    params={"key": self._key, "country": self.settings.region},
                    json=batch,
                    timeout=DEFAULT_TIMEOUT,
                )
                resp.raise_for_status()
                payload = resp.json() or []
            except Exception as exc:  # noqa: BLE001
                logger.warning("itad: prices batch failed (%s)", type(exc).__name__)
                continue
            for entry in payload:
                gid = entry.get("id")
                if gid:
                    out[gid] = entry.get("deals") or []
        return out

    def _batch_prices(self, game_ids: list[str]) -> dict[str, dict]:
        """Map each game id to its cheapest INR offer (``games/prices/v2``, 100/call)."""
        best: dict[str, dict] = {}
        for gid, offers in self.fetch_offers(game_ids).items():
            offer = _select_best_offer(offers)
            if offer is not None:
                best[gid] = offer
        return best


def _select_best_offer(offers: list[dict]) -> dict | None:
    """Pick the cheapest INR offer from a list, or ``None`` if none priced."""
    best: dict | None = None
    for offer in offers:
        amount = _amount(offer.get("price") or {})
        if amount is None:
            continue
        if best is None or amount < best["price"]:
            best = {
                "price": amount,
                "regular": _amount(offer.get("regular") or {}),
                "cut": int(offer.get("cut") or 0),
                "shop": (offer.get("shop") or {}).get("name"),
                "url": offer.get("url"),
                "is_low": _is_at_low(amount, offer.get("storeLow")),
            }
    return best


def _amount(price: dict) -> float | None:
    value = price.get("amount")
    return round(float(value), 2) if isinstance(value, int | float) else None
