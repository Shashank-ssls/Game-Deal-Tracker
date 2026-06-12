"""Phase 2 tests: Steam source — freebies only, malformed JSON, HTTP 500."""

from __future__ import annotations

from pathlib import Path

import responses

from src.config import Settings
from src.sources.steam import STEAM_FEATURED_URL, SteamSource

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@responses.activate
def test_fetch_returns_free_games_only():
    responses.add(responses.GET, STEAM_FEATURED_URL, body=_fixture("steam_featured.json"),
                  content_type="application/json")
    deals = SteamSource(Settings()).fetch()

    # The 80%-off paid game is excluded (it belongs to Phase 3).
    assert {d.title for d in deals} == {"Keepable Freebie", "Cool Game - Free Weekend"}


@responses.activate
def test_keepable_vs_free_weekend_flags():
    responses.add(responses.GET, STEAM_FEATURED_URL, body=_fixture("steam_featured.json"),
                  content_type="application/json")
    deals = {d.title: d for d in SteamSource(Settings()).fetch()}

    keep = deals["Keepable Freebie"]
    assert keep.is_free is True
    assert keep.is_temporary is False
    assert keep.steam_appid == 100
    assert keep.price_old == 599.0
    assert keep.url == "https://store.steampowered.com/app/100"

    weekend = deals["Cool Game - Free Weekend"]
    assert weekend.is_free is True
    assert weekend.is_temporary is True
    assert weekend.steam_appid == 300


@responses.activate
def test_malformed_json_returns_empty():
    responses.add(responses.GET, STEAM_FEATURED_URL, body="<<not json>>",
                  content_type="application/json")
    assert SteamSource(Settings()).fetch() == []


@responses.activate
def test_http_500_returns_empty():
    responses.add(responses.GET, STEAM_FEATURED_URL, json={"error": "boom"}, status=500)
    assert SteamSource(Settings()).fetch() == []
