"""Tests for the AAA watchlist checker."""

from __future__ import annotations

import responses

from src.config import Settings
from src.resolve import STORESEARCH_URL
from src.watch import APPDETAILS_URL, WatchlistChecker


def _noop_sleep(_seconds: float) -> None:
    return None


def _checker(titles) -> WatchlistChecker:
    return WatchlistChecker(Settings(watchlist=titles), sleeper=_noop_sleep)


def test_empty_watchlist_returns_empty():
    assert WatchlistChecker(Settings(watchlist=[])).fetch_on_sale() == []


@responses.activate
def test_on_sale_watchlist_game_becomes_preferred():
    responses.add(responses.GET, STORESEARCH_URL,
                  json={"items": [{"id": 1245620, "name": "ELDEN RING"}]})
    responses.add(responses.GET, APPDETAILS_URL, json={
        "1245620": {"success": True, "data": {
            "name": "ELDEN RING",
            "header_image": "https://img/elden.jpg",
            "price_overview": {"currency": "INR", "initial": 399900, "final": 199900,
                               "discount_percent": 50},
        }}
    })
    deals = _checker(["Elden Ring"]).fetch_on_sale()
    assert len(deals) == 1
    deal = deals[0]
    assert deal.is_preferred is True
    assert deal.steam_appid == 1245620
    assert deal.discount_pct == 50
    assert deal.price_new == 1999.0
    assert deal.source == "watchlist"


@responses.activate
def test_not_on_sale_watchlist_game_skipped():
    responses.add(responses.GET, STORESEARCH_URL,
                  json={"items": [{"id": 1, "name": "Full Price Game"}]})
    responses.add(responses.GET, APPDETAILS_URL, json={
        "1": {"success": True, "data": {
            "name": "Full Price Game",
            "price_overview": {"currency": "INR", "initial": 1000, "final": 1000,
                               "discount_percent": 0},
        }}
    })
    assert _checker(["Full Price Game"]).fetch_on_sale() == []


@responses.activate
def test_unresolved_title_skipped():
    responses.add(responses.GET, STORESEARCH_URL, json={"items": []})
    assert _checker(["Nonexistent Game 9999"]).fetch_on_sale() == []
