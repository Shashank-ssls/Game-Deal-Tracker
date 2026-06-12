"""Tests for the local Steam appid index (IStoreService/GetAppList cache)."""

from __future__ import annotations

import responses

from src.appindex import GETAPPLIST_URL, SteamAppIndex
from src.config import Settings
from src.db import Database

_KEY = "test-steam-key"


def _index(tmp_path, **kw) -> tuple[SteamAppIndex, Database]:
    db = Database(tmp_path / "test.db")
    settings = Settings(use_appid_index=True, steam_api_key=_KEY, **kw)
    return SteamAppIndex(settings, db), db


def _page(apps, have_more=False, last_appid=0):
    return {"response": {"apps": apps, "have_more_results": have_more, "last_appid": last_appid}}


_APPS = [
    {"appid": 1245620, "name": "ELDEN RING"},
    {"appid": 1, "name": "Doom"},          # base
    {"appid": 2, "name": "DOOM"},          # collides -> ambiguous
    {"appid": 782330, "name": "DOOM Eternal"},
]


@responses.activate
def test_ensure_fresh_downloads_and_caches(tmp_path):
    responses.add(responses.GET, GETAPPLIST_URL, json=_page(_APPS))
    index, db = _index(tmp_path)
    index.ensure_fresh()
    assert len(responses.calls) == 1
    assert db.app_index_age_days() is not None
    assert index.lookup("Elden Ring") == 1245620


@responses.activate
def test_lookup_unique_match_only(tmp_path):
    responses.add(responses.GET, GETAPPLIST_URL, json=_page(_APPS))
    index, _ = _index(tmp_path)
    index.ensure_fresh()
    # "Doom" maps to two appids (Doom / DOOM) -> ambiguous -> defers to online.
    assert index.lookup("Doom") is None
    # A name present once resolves; an unknown name returns None.
    assert index.lookup("DOOM Eternal") == 782330
    assert index.lookup("Never Heard Of It") is None


@responses.activate
def test_pagination_follows_cursor(tmp_path):
    responses.add(responses.GET, GETAPPLIST_URL,
                  json=_page([{"appid": 10, "name": "Half-Life"}], have_more=True, last_appid=10))
    responses.add(responses.GET, GETAPPLIST_URL,
                  json=_page([{"appid": 20, "name": "Portal"}], have_more=False))
    index, db = _index(tmp_path)
    index.ensure_fresh()
    assert len(responses.calls) == 2  # followed have_more_results
    assert index.lookup("Half-Life") == 10
    assert index.lookup("Portal") == 20


@responses.activate
def test_ensure_fresh_skips_when_recent(tmp_path):
    responses.add(responses.GET, GETAPPLIST_URL, json=_page(_APPS))
    index, _ = _index(tmp_path, appindex_cache_days=7)
    index.ensure_fresh()
    index.ensure_fresh()  # still fresh -> no second download
    assert len(responses.calls) == 1


@responses.activate
def test_no_key_skips_download(tmp_path):
    responses.add(responses.GET, GETAPPLIST_URL, json=_page(_APPS))
    db = Database(tmp_path / "test.db")
    index = SteamAppIndex(Settings(use_appid_index=True, steam_api_key=None), db)
    index.ensure_fresh()  # no key -> no HTTP, no crash
    assert len(responses.calls) == 0
    assert db.app_index_age_days() is None


@responses.activate
def test_refresh_failure_is_swallowed(tmp_path):
    responses.add(responses.GET, GETAPPLIST_URL, status=500)
    index, db = _index(tmp_path)
    index.ensure_fresh()  # must not raise
    assert db.app_index_age_days() is None  # nothing cached
