"""Attach publisher/developer metadata to live-feed deals (for preferred matching).

The discovery feeds (ITAD + CheapShark) rarely carry studio metadata, yet the
"Quality picks" section is decided by matching ``preferred_publishers`` against a
deal's publisher/developer. :class:`PublisherTagger` fills that gap on the live
feed — no locally stored game lists — in priority order:

  a. the SQLite ``games`` cache (publisher/developer the ratings enricher already
     stored for Steam appids) — zero HTTP;
  b. ITAD ``games/info/v2`` using the deal's resolved ``itad_game_id`` — one GET
     per game, bounded per run by ``publisher_metadata_batch``.

NOTE: ITAD exposes developer/publisher only through the single-id ``games/info/v2``
endpoint (there is no batch info endpoint — ``games/overview/v2`` omits studios), so
``publisher_metadata_batch`` caps the *number of info lookups per run* rather than
ids-per-request. Fetched metadata is persisted into the ``games`` cache (keyed by
Steam appid) so subsequent runs skip the call.

Franchise matching is unaffected: it is title-based (``quality.matches_franchise``)
and needs no attached metadata.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import replace

import requests

from src.config import Settings
from src.db import Database
from src.models import Deal
from src.sources.base import DEFAULT_TIMEOUT, build_session

logger = logging.getLogger(__name__)

ITAD_INFO_URL = "https://api.isthereanydeal.com/games/info/v2"


class PublisherTagger:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        session: requests.Session | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        throttle: float = 0.0,
    ) -> None:
        self.settings = settings
        self.db = db
        self.session = session if session is not None else build_session()
        self._sleep = sleeper
        self._throttle = throttle

    @property
    def _key(self) -> str | None:
        return self.settings.itad_api_key

    def tag(self, deals: list[Deal]) -> list[Deal]:
        """Return ``deals`` with publisher/developer filled where it was missing.

        No-op when no ``preferred_publishers`` are configured (only preferred
        matching consumes attached metadata; franchise matching is title-based).
        """
        if not self.settings.preferred_publishers:
            return deals

        budget = self.settings.publisher_metadata_batch
        out: list[Deal] = []
        for deal in deals:
            if deal.publisher or deal.developer:
                out.append(deal)
                continue

            publisher, developer = self._from_cache(deal)
            if publisher is None and developer is None and deal.itad_game_id and budget > 0:
                budget -= 1
                publisher, developer = self._from_itad(deal.itad_game_id)
                if (publisher or developer) and deal.steam_appid is not None:
                    self.db.upsert_game(
                        deal.steam_appid, publisher=publisher, developer=developer
                    )

            if publisher or developer:
                out.append(replace(deal, publisher=publisher, developer=developer))
            else:
                out.append(deal)
        return out

    def _from_cache(self, deal: Deal) -> tuple[str | None, str | None]:
        """Read publisher/developer from the games cache (zero HTTP)."""
        if deal.steam_appid is None:
            return None, None
        row = self.db.get_cached_game(deal.steam_appid)
        if not row:
            return None, None
        return row.get("publisher"), row.get("developer")

    def _from_itad(self, game_id: str) -> tuple[str | None, str | None]:
        """Fetch publisher/developer for one ITAD game id (best-effort)."""
        if not self._key:
            return None, None
        if self._throttle:
            self._sleep(self._throttle)
        try:
            resp = self.session.get(
                ITAD_INFO_URL,
                params={"key": self._key, "id": game_id},
                timeout=DEFAULT_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json() or {}
        except Exception as exc:  # noqa: BLE001 - optional enrichment, never crash
            logger.warning("publishers: info lookup failed (%s)", type(exc).__name__)
            return None, None
        return _join_names(data.get("publishers")), _join_names(data.get("developers"))


def _join_names(entries: object) -> str | None:
    """Comma-join the ``name`` fields of an ITAD developers/publishers array."""
    if not isinstance(entries, list):
        return None
    names = [str(e.get("name")).strip() for e in entries if isinstance(e, dict) and e.get("name")]
    return ", ".join(names) or None
