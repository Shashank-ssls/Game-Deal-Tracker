"""Tests for the SteamSpy publisher catalogue (auto-derived watchlist)."""

from __future__ import annotations

import responses

from src.catalogue import STEAMSPY_URL, PublisherCatalogue
from src.config import Settings
from src.db import Database

_STEAMSPY = {
    "730": {"name": "Counter-Strike 2", "developer": "Valve", "publisher": "Valve"},
    "1245620": {"name": "ELDEN RING", "developer": "FromSoftware",
                "publisher": "Bandai Namco Entertainment"},
    "1": {"name": "Indie Thing", "developer": "Tiny Studio", "publisher": "Tiny Studio"},
    "2": {"name": "Resident Evil 4", "developer": "CAPCOM Co., Ltd.", "publisher": "Capcom"},
}


def _catalogue(tmp_path, **kw) -> tuple[PublisherCatalogue, Database]:
    db = Database(tmp_path / "t.db")
    settings = Settings(preferred_publishers=["Capcom", "FromSoftware"], **kw)
    return PublisherCatalogue(settings, db), db


@responses.activate
def test_derives_titles_for_preferred_publishers(tmp_path):
    responses.add(responses.GET, STEAMSPY_URL, json=_STEAMSPY)
    cat, _ = _catalogue(tmp_path)
    titles = cat.derived_titles()
    assert set(titles) == {"ELDEN RING", "Resident Evil 4"}  # Valve/Tiny excluded


@responses.activate
def test_results_are_cached(tmp_path):
    responses.add(responses.GET, STEAMSPY_URL, json=_STEAMSPY)
    cat, _ = _catalogue(tmp_path, catalogue_cache_days=7)
    cat.derived_titles()
    cat.derived_titles()  # second call served from cache
    assert len(responses.calls) == 1


@responses.activate
def test_respects_max_derived_titles(tmp_path):
    responses.add(responses.GET, STEAMSPY_URL, json=_STEAMSPY)
    cat, _ = _catalogue(tmp_path, max_derived_titles=1)
    assert len(cat.derived_titles()) == 1


def test_no_preferred_publishers_returns_empty(tmp_path):
    db = Database(tmp_path / "t.db")
    assert PublisherCatalogue(Settings(preferred_publishers=[]), db).derived_titles() == []


@responses.activate
def test_failure_falls_back_to_cache(tmp_path):
    responses.add(responses.GET, STEAMSPY_URL, json=_STEAMSPY)
    cat, db = _catalogue(tmp_path)
    cat.derived_titles()  # warm the cache
    responses.reset()
    responses.add(responses.GET, STEAMSPY_URL, status=500)
    # Force a refresh attempt by ageing out the cache, then fail: keep cached titles.
    db.replace_derived_watchlist(db.get_derived_watchlist())  # keep titles, refresh ts
    import sqlite3
    with sqlite3.connect(db.db_path) as conn:
        conn.execute("UPDATE derived_watchlist_meta SET fetched_at = '2000-01-01T00:00:00+00:00'")
        conn.commit()
    titles = cat.derived_titles()  # refresh fails -> returns whatever is cached
    assert set(titles) == {"ELDEN RING", "Resident Evil 4"}
