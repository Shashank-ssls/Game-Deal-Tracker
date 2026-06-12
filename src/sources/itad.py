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
from dataclasses import replace

from src.models import Deal
from src.sources.base import DEFAULT_TIMEOUT, DealSource, parse_iso

logger = logging.getLogger(__name__)

ITAD_DEALS_URL = "https://api.isthereanydeal.com/deals/v2"
ITAD_LOOKUP_URL = "https://api.isthereanydeal.com/games/lookup/v1"
ITAD_PRICES_URL = "https://api.isthereanydeal.com/games/prices/v2"
ITAD_STORELOW_URL = "https://api.isthereanydeal.com/games/storelow/v2"

_STORELOW_BATCH = 100  # max game ids per storelow request

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

    @property
    def _key(self) -> str | None:
        return self.settings.itad_api_key

    # -- native ITAD deals ---------------------------------------------------

    def fetch(self) -> list[Deal]:
        if not self._key:
            logger.info("itad: no API key configured; skipping ITAD deals")
            return []
        params = {
            "key": self._key,
            "country": self.settings.region,
            "sort": "-cut",
            "limit": self.settings.max_deals_per_run,
            "nondeals": "false",
            "mature": "false",
        }
        try:
            data = self._get_json(ITAD_DEALS_URL, params=params)
            entries = data.get("list") or []
        except Exception as exc:  # noqa: BLE001 - source must never raise
            logger.warning("itad: fetch/parse failed (%s)", type(exc).__name__)
            return []

        deals: list[Deal] = []
        for entry in entries:
            try:
                deal = self._parse_entry(entry)
            except Exception as exc:  # noqa: BLE001
                logger.warning("itad: skipping entry (%s)", type(exc).__name__)
                continue
            if deal is not None:
                deals.append(deal)
        return deals

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
        return Deal(
            title=entry.get("title", "Unknown"),
            store=(deal.get("shop") or {}).get("name", "Unknown"),
            url=url,
            source=self.name,
            source_game_id=str(entry.get("id") or entry.get("slug") or entry.get("title")),
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

    # -- INR verification of USD discovery deals -----------------------------

    def verify_inr(self, deals: list[Deal]) -> list[Deal]:
        """Re-price USD deals in INR; drop sub-threshold / IN-unavailable ones."""
        to_verify = [d for d in deals if d.needs_inr_verify]
        if not to_verify:
            return [d for d in deals if not d.needs_inr_verify]
        if not self._key:
            logger.info("itad: no API key; cannot verify INR, dropping %d deal(s)", len(to_verify))
            return [d for d in deals if not d.needs_inr_verify]

        verified: list[Deal] = [d for d in deals if not d.needs_inr_verify]
        for deal in to_verify:
            try:
                updated = self._verify_one(deal)
            except Exception as exc:  # noqa: BLE001
                logger.warning("itad: verify failed for %r (%s)", deal.title, type(exc).__name__)
                updated = None
            if updated is not None:
                verified.append(updated)
        return verified

    def _verify_one(self, deal: Deal) -> Deal | None:
        game_id = self._lookup_game_id(deal)
        if not game_id:
            return None
        best = self._best_inr_price(game_id)
        if best is None:
            return None
        if best["cut"] < self.settings.min_discount_pct:
            return None
        return replace(
            deal,
            price_old=best["regular"],
            price_new=best["price"],
            currency=self.settings.currency,
            discount_pct=int(best["cut"]),
            needs_inr_verify=False,
            is_lowest_ever=best["is_low"],
            store=best["shop"] or deal.store,
            url=best["url"] or deal.url,
            steam_appid=deal.steam_appid or _steam_appid_from_url(best["url"]),
        )

    def _lookup_game_id(self, deal: Deal) -> str | None:
        params: dict[str, object] = {"key": self._key}
        if deal.steam_appid:
            params["appid"] = deal.steam_appid
        else:
            params["title"] = deal.title
        data = self._get_json(ITAD_LOOKUP_URL, params=params)
        if isinstance(data, dict) and data.get("found"):
            return (data.get("game") or {}).get("id")
        return None

    def _best_inr_price(self, game_id: str) -> dict | None:
        """Return the cheapest INR offer as a dict, or ``None`` if none in region."""
        response = self.session.post(
            ITAD_PRICES_URL,
            params={"key": self._key, "country": self.settings.region},
            json=[game_id],
            timeout=DEFAULT_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload:
            return None
        offers = (payload[0] or {}).get("deals") or []
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
