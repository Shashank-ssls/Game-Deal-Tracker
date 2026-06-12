"""Phase 6 tests: unset ID, appid fetch, sub-threshold sale, reclassify, dedup."""

from __future__ import annotations

import responses

from src.config import Settings
from src.db import Database
from src.models import Deal
from src.wishlist import (
    APPDETAILS_URL,
    GETWISHLIST_URL,
    WishlistMatcher,
    split_wishlist_hits,
)


def _noop_sleep(_seconds: float) -> None:
    return None


def _matcher(steam_id="76561190000000000") -> WishlistMatcher:
    return WishlistMatcher(Settings(steam_id64=steam_id), sleeper=_noop_sleep)


def _discount(title, *, appid, discount=80) -> Deal:
    return Deal(title=title, store="Steam", url=f"https://s/{appid}",
                source="itad", source_game_id=title, price_old=1000.0, price_new=200.0,
                currency="INR", discount_pct=discount, steam_appid=appid)


def test_unset_steam_id_returns_empty():
    assert WishlistMatcher(Settings(steam_id64=None)).fetch_appids() == set()


@responses.activate
def test_fetch_appids_parses_response():
    responses.add(responses.GET, GETWISHLIST_URL, json={
        "response": {"items": [
            {"appid": 100, "priority": 0},
            {"appid": 200, "priority": 1},
        ]}
    })
    assert _matcher().fetch_appids() == {100, 200}


@responses.activate
def test_fetch_appids_failure_returns_empty():
    responses.add(responses.GET, GETWISHLIST_URL, json={"e": "x"}, status=500)
    assert _matcher().fetch_appids() == set()


@responses.activate
def test_wishlist_game_at_40_percent_still_notifies():
    responses.add(responses.GET, APPDETAILS_URL, json={
        "555": {"success": True, "data": {
            "name": "Wishlisted RPG",
            "header_image": "https://img/555.jpg",
            "price_overview": {
                "currency": "INR", "initial": 99900, "final": 59940, "discount_percent": 40
            },
        }}
    })
    deals = _matcher().fetch_on_sale({555})
    assert len(deals) == 1
    deal = deals[0]
    assert deal.is_wishlist is True
    assert deal.discount_pct == 40           # below the 70% threshold, still a hit
    assert deal.price_new == 599.40
    assert deal.steam_appid == 555


@responses.activate
def test_price_check_skips_not_on_sale():
    responses.add(responses.GET, APPDETAILS_URL, json={
        "777": {"success": True, "data": {
            "name": "Full Price Game",
            "price_overview": {"currency": "INR", "initial": 1000, "final": 1000,
                               "discount_percent": 0},
        }}
    })
    assert _matcher().fetch_on_sale({777}) == []


def test_split_reclassifies_wishlisted_discounts():
    wishlisted = _discount("On My List", appid=42)
    other = _discount("Random Deal", appid=99)
    hits, remaining = split_wishlist_hits([wishlisted, other], {42})

    assert [d.title for d in hits] == ["On My List"]
    assert hits[0].is_wishlist is True
    assert [d.title for d in remaining] == ["Random Deal"]
    assert remaining[0].is_wishlist is False


def test_wishlist_dedup_applies_across_runs(tmp_path):
    db = Database(tmp_path / "t.db")
    hits, _ = split_wishlist_hits([_discount("On My List", appid=42, discount=45)], {42})
    hit = hits[0]
    assert hit.is_wishlist is True
    assert db.is_new(hit) is True
    db.mark_seen(hit)
    assert db.is_new(hit) is False  # not re-notified next run
