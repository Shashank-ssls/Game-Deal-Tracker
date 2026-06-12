"""Tests for the opt-in Steam appid resolver."""

from __future__ import annotations

import responses

from src.config import Settings
from src.models import Deal
from src.resolve import STORESEARCH_URL, SteamAppidResolver


def _noop_sleep(_seconds: float) -> None:
    return None


def _resolver(**kw) -> SteamAppidResolver:
    return SteamAppidResolver(Settings(resolve_steam_appids=True, **kw), sleeper=_noop_sleep)


def _deal(title, *, appid=None, is_free=False) -> Deal:
    return Deal(title=title, store="GOG", url="u", source="itad", source_game_id=title,
                price_old=1000.0, price_new=200.0, discount_pct=80,
                steam_appid=appid, is_free=is_free)


def test_disabled_by_default():
    deals = [_deal("Some Game")]
    assert SteamAppidResolver(Settings()).resolve(deals) == deals  # no-op when off


@responses.activate
def test_resolves_matching_title():
    responses.add(responses.GET, STORESEARCH_URL,
                  json={"items": [{"id": 1245620, "name": "ELDEN RING"}]})
    out = _resolver().resolve([_deal("Elden Ring")])
    assert out[0].steam_appid == 1245620


@responses.activate
def test_resolves_edition_prefix():
    responses.add(responses.GET, STORESEARCH_URL,
                  json={"items": [{"id": 99, "name": "Elden Ring"}]})
    out = _resolver().resolve([_deal("Elden Ring Deluxe Edition")])
    assert out[0].steam_appid == 99


@responses.activate
def test_rejects_mismatch():
    # Searching "Doom" must not latch onto "Doom Eternal".
    responses.add(responses.GET, STORESEARCH_URL,
                  json={"items": [{"id": 782330, "name": "DOOM Eternal"}]})
    out = _resolver().resolve([_deal("Doom")])
    assert out[0].steam_appid is None


@responses.activate
def test_skips_deals_that_already_have_appid_or_free():
    # No HTTP should happen for these.
    out = _resolver().resolve([_deal("Has ID", appid=5), _deal("Freebie", is_free=True)])
    assert out[0].steam_appid == 5
    assert len(responses.calls) == 0


class _FakeIndex:
    def __init__(self, mapping):
        self._mapping = mapping

    def lookup(self, title):
        return self._mapping.get(title)


@responses.activate
def test_index_hit_skips_http():
    index = _FakeIndex({"Elden Ring": 1245620})
    resolver = SteamAppidResolver(
        Settings(resolve_steam_appids=True), sleeper=_noop_sleep, index=index
    )
    out = resolver.resolve([_deal("Elden Ring")])
    assert out[0].steam_appid == 1245620
    assert len(responses.calls) == 0  # offline hit, no storesearch call


@responses.activate
def test_index_miss_falls_back_to_online():
    responses.add(responses.GET, STORESEARCH_URL,
                  json={"items": [{"id": 99, "name": "Hades"}]})
    index = _FakeIndex({})  # nothing cached -> online fallback
    resolver = SteamAppidResolver(
        Settings(resolve_steam_appids=True), sleeper=_noop_sleep, index=index
    )
    out = resolver.resolve([_deal("Hades")])
    assert out[0].steam_appid == 99
    assert len(responses.calls) == 1
