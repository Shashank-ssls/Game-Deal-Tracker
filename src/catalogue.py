"""Auto-derive watchlist titles from your preferred publishers (opt-in).

The manual ``watchlist`` lists specific games to price-check every run. This adds
the notable games from your ``preferred_publishers`` automatically: it pulls
SteamSpy's most-owned games and keeps those whose developer or publisher matches
your allowlist, so e.g. configuring "Capcom" surfaces Resident Evil / Monster
Hunter / Street Fighter without naming each title.

SteamSpy has no "by publisher" endpoint and its ``all`` feed is rate-limited to one
request per minute, so we fetch a single page (the top ~1000 games by owners) and
cache the derived titles in SQLite, refreshing only every ``catalogue_cache_days``.
Best-effort: any failure logs a warning and falls back to whatever is cached.
"""

from __future__ import annotations

import logging

from src.config import Settings
from src.db import Database
from src.quality import name_matches
from src.sources.base import build_session

logger = logging.getLogger(__name__)

STEAMSPY_URL = "https://steamspy.com/api.php"
STEAMSPY_TIMEOUT = 30


class PublisherCatalogue:
    def __init__(self, settings: Settings, db: Database, session=None) -> None:
        self.settings = settings
        self.db = db
        self.session = session if session is not None else build_session()

    def derived_titles(self) -> list[str]:
        """Return cached derived titles, refreshing them when stale or missing."""
        if not self.settings.preferred_publishers:
            return []
        age = self.db.derived_watchlist_age_days()
        if age is None or age >= self.settings.catalogue_cache_days:
            self._refresh()
        return self.db.get_derived_watchlist()

    def _refresh(self) -> None:
        try:
            titles = self._derive()
        except Exception as exc:  # noqa: BLE001 - optional feature, never crash the run
            logger.warning("catalogue: refresh failed (%s)", type(exc).__name__)
            return
        count = self.db.replace_derived_watchlist(titles)
        logger.info("catalogue: derived %d watchlist title(s) from preferred publishers", count)

    def _derive(self) -> list[str]:
        """Fetch SteamSpy's top games and keep those by a preferred publisher."""
        resp = self.session.get(
            STEAMSPY_URL, params={"request": "all", "page": 0}, timeout=STEAMSPY_TIMEOUT
        )
        resp.raise_for_status()
        games = (resp.json() or {}).values()  # top games by owners, already ranked
        names = self.settings.preferred_publishers
        out: list[str] = []
        for game in games:
            title = (game.get("name") or "").strip()
            haystack = f"{game.get('publisher') or ''}\n{game.get('developer') or ''}"
            if title and name_matches(haystack, names):
                out.append(title)
            if len(out) >= self.settings.max_derived_titles:
                break
        return out
