"""Local Steam appid index built from Steam's ``IStoreService/GetAppList`` (opt-in).

Steam's storefront search (see :mod:`src.resolve`) costs one throttled HTTP call
per title. ``GetAppList`` returns Steam's whole ``appid -> name`` catalogue, which we
normalize and cache in SQLite so most title->appid resolutions happen offline and
instantly. The online search remains the fallback whenever the index is ambiguous
(or disabled).

This uses ``IStoreService/GetAppList/v1`` (requires a free Steam Web API key) with
``include_games=true`` and DLC/software/video/hardware excluded — so the index holds
games only. That makes the accuracy rule below far more effective than the old,
key-less ``ISteamApps/GetAppList`` (which mixed in DLC, demos, soundtracks, and tools).

Accuracy rule: a lookup only resolves when **exactly one** indexed app has the
normalized name; zero or multiple matches defer to the online search.
"""

from __future__ import annotations

import logging

from src.config import Settings
from src.db import Database
from src.resolve import _norm
from src.sources.base import build_session

logger = logging.getLogger(__name__)

GETAPPLIST_URL = "https://api.steampowered.com/IStoreService/GetAppList/v1/"
# Each page is large; allow well beyond the shared 10s timeout.
APPLIST_TIMEOUT = 60
_PAGE_SIZE = 50000
_MAX_PAGES = 50  # safety bound against an unbounded pagination loop


class SteamAppIndex:
    def __init__(self, settings: Settings, db: Database, session=None) -> None:
        self.settings = settings
        self.db = db
        self.session = session if session is not None else build_session()

    def ensure_fresh(self) -> None:
        """Download and cache the app list if missing or older than the TTL.

        Requires ``STEAM_API_KEY``; without it the index is simply skipped (the
        resolver falls back to the online search). Best-effort: any failure logs a
        warning and leaves the existing (possibly stale) index in place; it never
        raises so a run is never aborted.
        """
        if not self.settings.steam_api_key:
            logger.info("appindex: no STEAM_API_KEY set; skipping (online search used)")
            return
        age = self.db.app_index_age_days()
        if age is not None and age < self.settings.appindex_cache_days:
            return
        try:
            pairs = self._download()
        except Exception as exc:  # noqa: BLE001 - optional feature, never crash the run
            logger.warning("appindex: refresh failed (%s)", type(exc).__name__)
            return
        count = self.db.replace_app_index(pairs)
        logger.info("appindex: cached %d games", count)

    def _download(self) -> list[tuple[int, str]]:
        """Page through GetAppList (games only) into ``(appid, norm)`` pairs."""
        out: list[tuple[int, str]] = []
        last_appid = 0
        for _ in range(_MAX_PAGES):
            resp = self.session.get(
                GETAPPLIST_URL,
                params={
                    "key": self.settings.steam_api_key,
                    "include_games": "true",
                    "include_dlc": "false",
                    "include_software": "false",
                    "include_videos": "false",
                    "include_hardware": "false",
                    "max_results": _PAGE_SIZE,
                    "last_appid": last_appid,
                },
                timeout=APPLIST_TIMEOUT,
            )
            resp.raise_for_status()
            data = (resp.json() or {}).get("response") or {}
            for app in data.get("apps") or []:
                appid = app.get("appid")
                norm = _norm(app.get("name"))
                if appid and norm:
                    out.append((int(appid), norm))
            if not data.get("have_more_results"):
                break
            last_appid = data.get("last_appid") or last_appid
        return out

    def lookup(self, title: str) -> int | None:
        """Return the appid only when exactly one indexed app matches ``title``."""
        matches = self.db.lookup_appids(_norm(title))
        return matches[0] if len(matches) == 1 else None
