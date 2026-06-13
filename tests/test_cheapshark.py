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


def _no_sleep(_seconds: float) -> None:
    return None


def _deals_page(n: int, savings: float, start: int = 0) -> list[dict]:
    return [
        {"title": f"G{start + i}", "dealID": f"D{start + i}", "storeID": "1",
         "gameID": str(start + i), "salePrice": "4.99", "normalPrice": "19.99",
         "savings": str(savings), "steamAppID": str(1000 + start + i)}
        for i in range(n)
    ]


@responses.activate
def test_paginates_until_total_page_count():
    _register_stores()
    # Two full 60-item pages; X-Total-Page-Count=2 stops after page 1.
    responses.add(responses.GET, CHEAPSHARK_DEALS_URL, json=_deals_page(60, 80.0, start=0),
                  headers={"X-Total-Page-Count": "2"})
    responses.add(responses.GET, CHEAPSHARK_DEALS_URL, json=_deals_page(60, 75.0, start=60),
                  headers={"X-Total-Page-Count": "2"})
    settings = Settings(discovery_discount_pct=70, max_deals_per_run=500, feed_page_sleep=0)
    deals = CheapSharkSource(settings, sleeper=_no_sleep).fetch()

    assert len(deals) == 120
    assert len(responses.calls) == 3   # 1 stores + 2 deal pages
    # pageSize is the hard 60 cap, not max_deals_per_run.
    assert "pageSize=60" in responses.calls[1].request.url


@responses.activate
def test_stops_on_empty_page():
    _register_stores()
    responses.add(responses.GET, CHEAPSHARK_DEALS_URL, json=_deals_page(60, 80.0),
                  headers={"X-Total-Page-Count": "9"})
    responses.add(responses.GET, CHEAPSHARK_DEALS_URL, json=[],
                  headers={"X-Total-Page-Count": "9"})
    settings = Settings(discovery_discount_pct=70, feed_page_sleep=0)
    deals = CheapSharkSource(settings, sleeper=_no_sleep).fetch()
    assert len(deals) == 60


@responses.activate
def test_storeid_filter_for_allowlist():
    _register_stores()
    responses.add(responses.GET, CHEAPSHARK_DEALS_URL, json=_deals_page(5, 80.0),
                  headers={"X-Total-Page-Count": "1"})
    settings = Settings(stores=["Steam"], discovery_discount_pct=70, feed_page_sleep=0)
    CheapSharkSource(settings, sleeper=_no_sleep).fetch()
    # Steam is storeID 1 in the fixture; the filter must be sent.
    deal_call = next(c for c in responses.calls if CHEAPSHARK_DEALS_URL in c.request.url)
    assert "storeID=1" in deal_call.request.url


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
