"""Phase 3 tests: CheapShark — threshold filter, store mapping, USD flag, errors."""

from __future__ import annotations

from pathlib import Path

import responses

from src.config import Settings
from src.sources.cheapshark import (
    CHEAPSHARK_DEALS_URL,
    CHEAPSHARK_STORES_URL,
    CheapSharkSource,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _register_stores():
    responses.add(responses.GET, CHEAPSHARK_STORES_URL,
                  body=_fixture("cheapshark_stores.json"), content_type="application/json")


@responses.activate
def test_filters_below_threshold_and_maps_stores():
    _register_stores()
    responses.add(responses.GET, CHEAPSHARK_DEALS_URL,
                  body=_fixture("cheapshark_deals.json"), content_type="application/json")

    # Pin discovery to the display threshold so the source filters at 70%.
    deals = CheapSharkSource(Settings(discovery_discount_pct=70)).fetch()

    assert {d.title for d in deals} == {"Heavy Discount Game", "Massive Discount Game"}
    heavy = next(d for d in deals if d.title == "Heavy Discount Game")
    assert heavy.store == "Steam"            # storeID 1 mapped
    assert heavy.currency == "USD"
    assert heavy.needs_inr_verify is True
    assert heavy.discount_pct == 75          # 75.0375 rounded
    assert heavy.steam_appid == 111111
    assert heavy.price_new == 4.99


@responses.activate
def test_populates_ratings_from_cheapshark_payload():
    _register_stores()
    responses.add(responses.GET, CHEAPSHARK_DEALS_URL, json=[
        {"title": "Rated Game", "dealID": "D1", "storeID": "1", "gameID": "1",
         "salePrice": "4.99", "normalPrice": "19.99", "savings": "75.0", "steamAppID": "10",
         "steamRatingText": "Very Positive", "steamRatingPercent": "92",
         "steamRatingCount": "12345", "metacriticScore": "88"},
    ])
    deal = CheapSharkSource(Settings()).fetch()[0]
    assert deal.review_summary == "Very Positive"
    assert deal.review_pct == 92
    assert deal.review_count == 12345
    assert deal.metacritic == 88


@responses.activate
def test_malformed_json_returns_empty():
    _register_stores()
    responses.add(responses.GET, CHEAPSHARK_DEALS_URL, body="<<nope>>",
                  content_type="application/json")
    assert CheapSharkSource(Settings()).fetch() == []


@responses.activate
def test_http_500_returns_empty():
    _register_stores()
    responses.add(responses.GET, CHEAPSHARK_DEALS_URL, json={"error": "x"}, status=500)
    assert CheapSharkSource(Settings()).fetch() == []
