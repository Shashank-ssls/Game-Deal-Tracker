"""Phase 3 tests: ITAD native deals + USD->INR verification + no-key degradation."""

from __future__ import annotations

from pathlib import Path

import responses

from src.config import Settings
from src.models import Deal
from src.sources.itad import (
    ITAD_DEALS_URL,
    ITAD_LOOKUP_URL,
    ITAD_PRICES_URL,
    ITAD_STORELOW_URL,
    ITADSource,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _keyed() -> Settings:
    return Settings(itad_api_key="dummy_key")


def _usd_deal(appid: int = 111111) -> Deal:
    return Deal(
        title="Heavy Discount Game",
        store="Steam",
        url="https://www.cheapshark.com/redirect?dealID=DEAL75",
        source="cheapshark",
        source_game_id="111",
        price_old=19.99,
        price_new=4.99,
        currency="USD",
        discount_pct=75,
        needs_inr_verify=True,
        steam_appid=appid,
    )


@responses.activate
def test_fetch_filters_below_threshold():
    responses.add(responses.GET, ITAD_DEALS_URL, body=_fixture("itad_deals.json"),
                  content_type="application/json")
    deals = ITADSource(_keyed()).fetch()

    assert {d.title for d in deals} == {"Deep Cut Game", "Good Cut Game"}
    deep = next(d for d in deals if d.title == "Deep Cut Game")
    assert deep.currency == "INR"
    assert deep.discount_pct == 90
    assert deep.price_new == 199.0
    assert deep.ends_at is not None


def test_fetch_without_key_returns_empty():
    assert ITADSource(Settings(itad_api_key=None)).fetch() == []


@responses.activate
def test_fetch_drops_non_game_types():
    responses.add(responses.GET, ITAD_DEALS_URL, json={"list": [
        {"id": "g1", "title": "Real Game", "type": "game",
         "deal": {"shop": {"name": "Steam"}, "price": {"amount": 100.0, "currency": "INR"},
                  "regular": {"amount": 1000.0, "currency": "INR"}, "cut": 90, "url": "u1"}},
        {"id": "b1", "title": "AWS Certification Bundle", "type": "bundle",
         "deal": {"shop": {"name": "Fanatical"}, "price": {"amount": 476.0, "currency": "INR"},
                  "regular": {"amount": 47600.0, "currency": "INR"}, "cut": 99, "url": "u2"}},
    ]})
    deals = ITADSource(_keyed()).fetch()
    assert [d.title for d in deals] == ["Real Game"]  # bundle dropped


@responses.activate
def test_fetch_sets_lowest_ever_and_steam_appid_from_url():
    responses.add(responses.GET, ITAD_DEALS_URL, json={"list": [
        {"id": "g", "title": "Low Game", "type": "game", "deal": {
            "shop": {"name": "Steam"},
            "price": {"amount": 150.0, "currency": "INR"},
            "regular": {"amount": 1500.0, "currency": "INR"},
            "storeLow": {"amount": 150.0, "currency": "INR"},
            "cut": 90, "url": "https://store.steampowered.com/app/12345"}},
    ]})
    deal = ITADSource(_keyed()).fetch()[0]
    assert deal.is_lowest_ever is True       # price == store low
    assert deal.steam_appid == 12345         # parsed from the store URL


def _itad_deal(gid: str, price_new: float) -> Deal:
    return Deal(title="G", store="Steam", url="u", source="itad", source_game_id=gid,
                price_old=1000.0, price_new=price_new, discount_pct=80, is_lowest_ever=False)


@responses.activate
def test_flag_all_store_lows_sets_flag_at_or_below_min():
    responses.add(responses.POST, ITAD_STORELOW_URL, json=[
        {"id": "gid1", "lows": [
            {"shop": {"name": "Fanatical"}, "price": {"amount": 300.0, "currency": "INR"}},
            {"shop": {"name": "Steam"}, "price": {"amount": 250.0, "currency": "INR"}},
        ]},
        {"id": "gid2", "lows": [
            {"shop": {"name": "Steam"}, "price": {"amount": 100.0, "currency": "INR"}},
        ]},
    ])
    settings = Settings(itad_api_key="k", all_store_low=True)
    deals = [_itad_deal("gid1", 250.0), _itad_deal("gid2", 200.0)]
    out = ITADSource(settings).flag_all_store_lows(deals)
    assert out[0].is_lowest_ever is True    # 250 <= all-store low (250)
    assert out[1].is_lowest_ever is False   # 200 > all-store low (100)


def test_flag_all_store_lows_noop_when_disabled():
    deals = [_itad_deal("gid1", 250.0)]
    # all_store_low default False -> deals returned untouched, no HTTP.
    assert ITADSource(Settings(itad_api_key="k")).flag_all_store_lows(deals) == deals


@responses.activate
def test_verify_inr_reprices_and_keeps_cheapest():
    responses.add(responses.GET, ITAD_LOOKUP_URL, body=_fixture("itad_lookup.json"),
                  content_type="application/json")
    responses.add(responses.POST, ITAD_PRICES_URL, body=_fixture("itad_prices.json"),
                  content_type="application/json")

    verified = ITADSource(_keyed()).verify_inr([_usd_deal()])

    assert len(verified) == 1
    deal = verified[0]
    assert deal.currency == "INR"
    assert deal.price_new == 399.0   # GOG cheaper than Steam in fixture
    assert deal.discount_pct == 78
    assert deal.needs_inr_verify is False
    assert deal.store == "GOG"


@responses.activate
def test_verify_inr_drops_sub_threshold_in_inr():
    responses.add(responses.GET, ITAD_LOOKUP_URL, body=_fixture("itad_lookup.json"),
                  content_type="application/json")
    # In INR the best cut is only 40% -> below the 70% threshold -> dropped.
    responses.add(
        responses.POST,
        ITAD_PRICES_URL,
        json=[{"id": "itad-verify-1", "deals": [
            {"shop": {"name": "Steam"}, "price": {"amount": 600.0, "currency": "INR"},
             "regular": {"amount": 1000.0, "currency": "INR"}, "cut": 40,
             "url": "https://store.steampowered.com/app/111111"}
        ]}],
    )
    assert ITADSource(_keyed()).verify_inr([_usd_deal()]) == []


@responses.activate
def test_verify_inr_drops_when_not_found_in_in():
    responses.add(responses.GET, ITAD_LOOKUP_URL, json={"found": False})
    assert ITADSource(_keyed()).verify_inr([_usd_deal()]) == []


def test_verify_without_key_drops_usd_but_keeps_freebies():
    free = Deal(
        title="Free Epic Game", store="Epic Games", url="https://epic/p/free",
        source="epic", source_game_id="offer-1", is_free=True, discount_pct=100,
        price_new=0.0,
    )
    result = ITADSource(Settings(itad_api_key=None)).verify_inr([free, _usd_deal()])
    # ITAD outage: freebie still passes through, unverifiable USD deal is dropped.
    assert result == [free]
