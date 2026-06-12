"""Integration test: collect_deals wires all sources + INR verify + merge + filter."""

from __future__ import annotations

from pathlib import Path

import responses

from src import main as m
from src.config import Settings
from src.main import collect_deals
from src.models import Deal
from src.sources.cheapshark import CHEAPSHARK_DEALS_URL, CHEAPSHARK_STORES_URL
from src.sources.epic import EPIC_URL
from src.sources.itad import ITAD_DEALS_URL, ITAD_LOOKUP_URL, ITAD_PRICES_URL
from src.sources.steam import STEAM_FEATURED_URL

FIXTURES = Path(__file__).parent / "fixtures"


def _fx(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@responses.activate
def test_collect_deals_merges_all_sources_and_filters_bundles():
    responses.add(responses.GET, EPIC_URL, body=_fx("epic_free.json"),
                  content_type="application/json")
    responses.add(responses.GET, STEAM_FEATURED_URL, body=_fx("steam_featured.json"),
                  content_type="application/json")
    responses.add(responses.GET, ITAD_DEALS_URL, json={"list": [
        {"id": "g", "title": "ITAD Game", "type": "game", "deal": {
            "shop": {"name": "Steam"}, "price": {"amount": 100.0, "currency": "INR"},
            "regular": {"amount": 1000.0, "currency": "INR"}, "cut": 90,
            "url": "https://store.steampowered.com/app/777"}},
        {"id": "b", "title": "Noise Bundle", "type": "bundle", "deal": {
            "shop": {"name": "Fanatical"}, "price": {"amount": 50.0, "currency": "INR"},
            "regular": {"amount": 5000.0, "currency": "INR"}, "cut": 99, "url": "u"}},
    ]})
    responses.add(responses.GET, CHEAPSHARK_STORES_URL, body=_fx("cheapshark_stores.json"),
                  content_type="application/json")
    responses.add(responses.GET, CHEAPSHARK_DEALS_URL, json=[
        {"title": "CS Game", "dealID": "D1", "storeID": "1", "gameID": "9",
         "salePrice": "3.00", "normalPrice": "15.00", "savings": "80.0", "steamAppID": "999"},
    ])
    responses.add(responses.GET, ITAD_LOOKUP_URL, json={"found": True, "game": {"id": "gid"}})
    responses.add(responses.POST, ITAD_PRICES_URL, json=[{"id": "gid", "deals": [
        {"shop": {"name": "Steam"}, "price": {"amount": 350.0, "currency": "INR"},
         "regular": {"amount": 1500.0, "currency": "INR"}, "cut": 77,
         "url": "https://store.steampowered.com/app/999"},
    ]}])

    free, discounts = collect_deals(Settings(itad_api_key="k"))

    free_titles = {d.title for d in free}
    disc_titles = {d.title for d in discounts}
    assert "Free Game Now" in free_titles      # Epic freebie
    assert "Keepable Freebie" in free_titles    # Steam freebie
    assert "ITAD Game" in disc_titles           # ITAD native INR deal
    assert "CS Game" in disc_titles             # CheapShark, verified to INR
    assert "Noise Bundle" not in disc_titles    # non-game bundle filtered out

    cs = next(d for d in discounts if d.title == "CS Game")
    assert cs.currency == "INR"
    assert cs.discount_pct == 77
    assert cs.steam_appid == 999


def _disc(title, *, gid, genres, discount=80, price_new=200.0, price_old=1000.0):
    return Deal(title=title, store="Steam", url=f"u/{gid}", source="itad",
                source_game_id=gid, price_old=price_old, price_new=price_new,
                discount_pct=discount, genres=genres)


def _stub_pipeline(monkeypatch, discounts):
    """Neuter every network collaborator so run_pipeline runs on supplied deals."""
    monkeypatch.setattr(m, "collect_deals", lambda settings, index=None: ([], list(discounts)))
    monkeypatch.setattr(m.WishlistMatcher, "fetch_appids", lambda self: set())
    monkeypatch.setattr(m.WatchlistChecker, "fetch_on_sale", lambda self, extra_titles=None: [])
    monkeypatch.setattr(m.RatingsEnricher, "enrich", lambda self, deals: deals)
    monkeypatch.setattr(m.ITADSource, "flag_all_store_lows", lambda self, deals: deals)
    captured: dict = {}

    def fake_post(self, free, wishlist, discounts, *, preferred=None, mark_seen, dry_run=False):
        captured.update(free=free, wishlist=wishlist, preferred=preferred or [],
                        discounts=discounts)
        for d in free + wishlist + (preferred or []) + discounts:
            mark_seen(d)
        return len(free) + len(wishlist) + len(preferred or []) + len(discounts)

    monkeypatch.setattr(m.DiscordNotifier, "post", fake_post)
    return captured


def test_run_pipeline_promotes_franchise_over_content_filter(tmp_path, monkeypatch):
    # include_genres would drop the non-RPG franchise title, and it's sub-threshold +
    # not premium so the gate would drop it too — yet franchise watch must surface it.
    settings = Settings(
        db_path=tmp_path / "t.db", discord_webhook_url="https://discord.test/wh",
        franchises=["Resident Evil"], include_genres=["RPG"], min_discount_pct=70,
    )
    franchise = _disc("Resident Evil 4", gid="re4", genres="Action", discount=60, price_new=400.0)
    rpg = _disc("Some RPG", gid="rpg", genres="Indie, RPG")
    puzzle = _disc("Puzzle Game", gid="pz", genres="Casual, Puzzle")
    captured = _stub_pipeline(monkeypatch, [franchise, rpg, puzzle])

    assert m.run_pipeline(settings, dry_run=False) == 0
    pref = {d.title for d in captured["preferred"]}
    disc = {d.title for d in captured["discounts"]}
    assert "Resident Evil 4" in pref          # franchise promoted, filter + gate bypassed
    assert all(d.is_preferred for d in captured["preferred"])
    assert "Some RPG" in disc                  # matches include_genres + threshold
    assert "Puzzle Game" not in pref | disc    # dropped by include_genres


def test_run_pipeline_flags_price_drop(tmp_path, monkeypatch):
    settings = Settings(
        db_path=tmp_path / "t.db", discord_webhook_url="https://discord.test/wh",
        price_drop_window_days=7, min_discount_pct=70,
    )
    # Seed yesterday's higher price so today's deal reads as a drop.
    from datetime import UTC, datetime, timedelta

    from src.db import Database
    db = Database(settings.db_path)
    import sqlite3
    y = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
    with sqlite3.connect(db.db_path) as conn:
        conn.execute("INSERT INTO price_history VALUES ('itad','cheap',?,500.0)", (y,))
        conn.commit()

    cheaper = _disc("Cheaper Now", gid="cheap", genres="Action", price_new=300.0)
    captured = _stub_pipeline(monkeypatch, [cheaper])
    assert m.run_pipeline(settings, dry_run=False) == 0
    posted = captured["discounts"][0]
    assert posted.title == "Cheaper Now"
    assert posted.is_price_drop is True
