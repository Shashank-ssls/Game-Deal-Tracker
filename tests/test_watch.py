"""Stage 4 tests: multi-store watchlist pricing (+ legacy no-key Steam path)."""

from __future__ import annotations

import responses

from src.config import Settings
from src.db import Database
from src.sources.itad import ITAD_LOOKUP_URL, ITAD_PRICES_URL
from src.watch import APPDETAILS_URL, STORESEARCH_URL, WatchlistChecker


def _noop_sleep(_seconds: float) -> None:
    return None


def _db(tmp_path) -> Database:
    return Database(tmp_path / "w.db")


def _offer(shop: str, price: float, cut: int, url: str = "") -> dict:
    return {
        "shop": {"name": shop},
        "price": {"amount": price, "currency": "INR"},
        "regular": {"amount": 1799.0, "currency": "INR"},
        "cut": cut,
        "url": url or f"https://{shop.lower()}.com/game",
    }


def _register_lookup(game_id: str) -> None:
    responses.add(responses.GET, ITAD_LOOKUP_URL,
                  json={"found": True, "game": {"id": game_id}})


def _register_prices(game_id: str, offers: list[dict]) -> None:
    responses.add(responses.POST, ITAD_PRICES_URL, json=[{"id": game_id, "deals": offers}])


def _keyed(tmp_path, titles, **kw) -> tuple[WatchlistChecker, Database]:
    db = _db(tmp_path)
    settings = Settings(itad_api_key="k", watchlist=titles, **kw)
    return WatchlistChecker(settings, db, sleeper=_noop_sleep), db


def test_empty_watchlist_returns_empty(tmp_path):
    assert WatchlistChecker(Settings(watchlist=[]), _db(tmp_path)).fetch_on_sale() == []


@responses.activate
def test_gog_only_hit_yields_gog_embed(tmp_path):
    _register_lookup("g-w3")
    _register_prices("g-w3", [_offer("GOG", 399.0, 80, "https://gog.com/witcher3")])
    checker, _ = _keyed(tmp_path, ["The Witcher 3"], stores=["Steam", "Epic", "GOG"])
    deals = checker.fetch_on_sale()
    assert len(deals) == 1
    assert deals[0].store == "GOG"
    assert deals[0].is_preferred is True
    assert deals[0].itad_game_id == "g-w3"
    assert deals[0].price_new == 399.0


@responses.activate
def test_cheapest_of_three_stores_wins(tmp_path):
    _register_lookup("g-x")
    _register_prices("g-x", [
        _offer("Steam", 899.0, 50), _offer("Epic Game Store", 799.0, 55),
        _offer("GOG", 699.0, 60),
    ])
    checker, _ = _keyed(tmp_path, ["Game X"], stores=["Steam", "Epic", "GOG"])
    deals = checker.fetch_on_sale()
    assert deals[0].store == "GOG"
    assert deals[0].price_new == 699.0


@responses.activate
def test_allowlist_excludes_fanatical(tmp_path):
    _register_lookup("g-y")
    _register_prices("g-y", [
        _offer("Fanatical", 199.0, 90),  # cheapest but not allowed
        _offer("GOG", 499.0, 60),
    ])
    checker, _ = _keyed(tmp_path, ["Game Y"], stores=["Steam", "GOG"])
    deals = checker.fetch_on_sale()
    assert len(deals) == 1
    assert deals[0].store == "GOG"   # Fanatical filtered before picking cheapest


@responses.activate
def test_full_price_game_skipped(tmp_path):
    _register_lookup("g-z")
    _register_prices("g-z", [_offer("Steam", 1799.0, 0)])  # cut 0 -> not on sale
    checker, _ = _keyed(tmp_path, ["Game Z"])
    assert checker.fetch_on_sale() == []


@responses.activate
def test_lookup_cache_prevents_repeat_lookups(tmp_path):
    # Pre-seed the title->id cache: a fresh run must do ZERO lookups, one price call.
    db = _db(tmp_path)
    db.upsert_title_id("Cached A", "g-a")
    db.upsert_title_id("Cached B", "g-b")
    responses.add(responses.POST, ITAD_PRICES_URL, json=[
        {"id": "g-a", "deals": [_offer("Steam", 500.0, 50)]},
        {"id": "g-b", "deals": [_offer("GOG", 400.0, 60)]},
    ])
    settings = Settings(itad_api_key="k", watchlist=["Cached A", "Cached B"])
    deals = WatchlistChecker(settings, db, sleeper=_noop_sleep).fetch_on_sale()

    assert len(deals) == 2
    lookup_calls = [c for c in responses.calls if ITAD_LOOKUP_URL in c.request.url]
    price_calls = [c for c in responses.calls if ITAD_PRICES_URL in c.request.url]
    assert lookup_calls == []         # nothing re-resolved
    assert len(price_calls) == 1      # a single batched price call


@responses.activate
def test_no_key_fallback_uses_steam_appdetails(tmp_path):
    # No ITAD key -> legacy Steam path (storesearch + appdetails).
    responses.add(responses.GET, STORESEARCH_URL,
                  json={"items": [{"id": 1245620, "name": "ELDEN RING"}]})
    responses.add(responses.GET, APPDETAILS_URL, json={
        "1245620": {"success": True, "data": {
            "name": "ELDEN RING", "header_image": "https://img/elden.jpg",
            "price_overview": {"currency": "INR", "initial": 399900, "final": 199900,
                               "discount_percent": 50},
        }}
    })
    settings = Settings(itad_api_key=None, watchlist=["Elden Ring"])
    deals = WatchlistChecker(settings, _db(tmp_path), sleeper=_noop_sleep).fetch_on_sale()
    assert len(deals) == 1
    assert deals[0].steam_appid == 1245620
    assert deals[0].discount_pct == 50
    assert deals[0].price_new == 1999.0
