"""Integration test: collect_deals wires all sources + INR verify + merge + filter."""

from __future__ import annotations

import json
from pathlib import Path

import responses

from src import main as m
from src.config import Settings
from src.db import Database
from src.enrich.publishers import ITAD_INFO_URL
from src.main import collect_deals
from src.models import Deal
from src.sources.cheapshark import CHEAPSHARK_DEALS_URL, CHEAPSHARK_STORES_URL
from src.sources.epic import EPIC_URL
from src.sources.itad import (
    ITAD_DEALS_URL,
    ITAD_LOOKUP_SHOP_URL,
    ITAD_LOOKUP_URL,
    ITAD_PRICES_URL,
    STEAM_SHOP_ID,
)
from src.sources.steam import STEAM_FEATURED_URL

FIXTURES = Path(__file__).parent / "fixtures"
SHOP_LOOKUP_URL = ITAD_LOOKUP_SHOP_URL.format(shop_id=STEAM_SHOP_ID)


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

    free, discounts, counts = collect_deals(Settings(itad_api_key="k"))
    assert counts["itad"] >= 1 and counts["cheapshark"] >= 1  # per-source observability

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
    monkeypatch.setattr(m, "collect_deals", lambda settings: ([], list(discounts), {}))
    monkeypatch.setattr(m.WishlistMatcher, "fetch_appids", lambda self: set())
    monkeypatch.setattr(m.WatchlistChecker, "fetch_on_sale", lambda self: [])
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


def test_silent_source_warns_when_others_have_data(caplog):
    counts = {"itad": 0, "cheapshark": 50, "steam": 3, "epic": 1}
    with caplog.at_level("WARNING", logger="src.main"):
        m._warn_silent_sources(Settings(itad_api_key="k"), counts)
    assert "itad returned 0 deals" in caplog.text


def test_all_empty_sources_do_not_warn(caplog):
    counts = {"itad": 0, "cheapshark": 0, "steam": 0, "epic": 0}
    with caplog.at_level("WARNING", logger="src.main"):
        m._warn_silent_sources(Settings(itad_api_key="k"), counts)
    assert "returned 0 deals" not in caplog.text  # genuinely quiet day, not a failure


def test_cheapshark_silence_warns_only_with_itad_key(caplog):
    counts = {"itad": 100, "cheapshark": 0, "steam": 3, "epic": 1}
    with caplog.at_level("WARNING", logger="src.main"):
        m._warn_silent_sources(Settings(itad_api_key="k"), counts)
    assert "cheapshark returned 0 deals" in caplog.text
    caplog.clear()
    with caplog.at_level("WARNING", logger="src.main"):
        m._warn_silent_sources(Settings(itad_api_key=None), counts)
    assert "cheapshark returned 0 deals" not in caplog.text


@responses.activate
def test_discord_500_does_not_write_deals_seen(tmp_path, monkeypatch):
    # The real notifier runs against a webhook that 500s; deals_seen must stay empty
    # so the deal is retried next run (mark_seen only after a successful post).
    import sqlite3

    webhook = "https://discord.test/wh"
    responses.add(responses.POST, webhook, status=500)
    settings = Settings(db_path=tmp_path / "t.db", discord_webhook_url=webhook,
                        min_discount_pct=70)
    monkeypatch.setattr(
        m, "collect_deals",
        lambda s: ([], [_disc("Deal A", gid="a", genres="Action")], {}),
    )
    monkeypatch.setattr(m.WishlistMatcher, "fetch_appids", lambda self: set())
    monkeypatch.setattr(m.WatchlistChecker, "fetch_on_sale", lambda self: [])
    monkeypatch.setattr(m.RatingsEnricher, "enrich", lambda self, deals: deals)
    monkeypatch.setattr(m.ITADSource, "flag_all_store_lows", lambda self, deals: deals)

    assert m.run_pipeline(settings, dry_run=False) == 0
    n = sqlite3.connect(settings.db_path).execute(
        "SELECT count(*) FROM deals_seen"
    ).fetchone()[0]
    assert n == 0  # failed post -> nothing marked seen -> retried next run


def test_run_pipeline_region_lock_drops_non_inr_paid_deal(tmp_path, monkeypatch):
    settings = Settings(
        db_path=tmp_path / "t.db", discord_webhook_url="https://discord.test/wh",
        min_discount_pct=70,
    )
    inr = _disc("INR Deal", gid="inr", genres="Action")  # currency defaults to INR
    usd = Deal(title="USD Deal", store="Steam", url="u", source="cheapshark",
               source_game_id="usd", price_old=40.0, price_new=6.0, currency="USD",
               discount_pct=85, needs_inr_verify=True)
    captured = _stub_pipeline(monkeypatch, [inr, usd])
    assert m.run_pipeline(settings, dry_run=False) == 0
    titles = {d.title for d in captured["discounts"]}
    assert "INR Deal" in titles
    assert "USD Deal" not in titles  # region lock drops the unverified USD deal


def test_run_pipeline_price_floor_exempts_free_and_watchlist(tmp_path, monkeypatch):
    settings = Settings(
        db_path=tmp_path / "t.db", discord_webhook_url="https://discord.test/wh",
        min_discount_pct=70, min_original_price=630,
    )
    # Both clear the discount threshold and gate; only the price floor decides them.
    cheap = _disc("Cheap Indie", gid="ind", genres="Action", discount=75,
                  price_old=300.0, price_new=75.0)     # below ₹630 floor -> dropped
    pricey = _disc("Pricey Deal", gid="pr", genres="Action", discount=75,
                   price_old=2000.0, price_new=500.0)  # above floor -> kept
    free = Deal(title="Free Game", store="Epic Games", url="u", source="epic",
                source_game_id="fg", is_free=True, price_new=0.0, discount_pct=100)
    watch = Deal(title="Cheap Watched", store="Steam", url="u", source="watchlist",
                 source_game_id="123", steam_appid=123, currency="INR",
                 price_old=500.0, price_new=250.0, discount_pct=50, is_preferred=True)

    captured: dict = {}
    monkeypatch.setattr(m, "collect_deals", lambda s: ([free], [cheap, pricey], {}))
    monkeypatch.setattr(m.WishlistMatcher, "fetch_appids", lambda self: set())
    monkeypatch.setattr(m.WatchlistChecker, "fetch_on_sale", lambda self: [watch])
    monkeypatch.setattr(m.RatingsEnricher, "enrich", lambda self, deals: deals)
    monkeypatch.setattr(m.ITADSource, "flag_all_store_lows", lambda self, deals: deals)

    def fake_post(self, free_, wishlist, discounts, *, preferred=None, mark_seen, dry_run=False):
        captured.update(free=free_, preferred=preferred or [], discounts=discounts)
        for d in free_ + wishlist + (preferred or []) + discounts:
            mark_seen(d)
        return len(free_) + len(wishlist) + len(preferred or []) + len(discounts)
    monkeypatch.setattr(m.DiscordNotifier, "post", fake_post)

    assert m.run_pipeline(settings, dry_run=False) == 0
    disc = {d.title for d in captured["discounts"]}
    assert "Cheap Indie" not in disc                 # below floor -> dropped
    assert "Pricey Deal" in disc                     # above floor -> kept
    assert "Free Game" in {d.title for d in captured["free"]}        # free exempt
    assert "Cheap Watched" in {d.title for d in captured["preferred"]}  # watchlist exempt


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


# -- Stage 5 end-to-end integration ------------------------------------------

def _itad_steam_entry(i: int, *, cut: int, price: float, gid: str | None = None,
                      title: str | None = None) -> dict:
    gid = gid or f"g{i}"
    return {
        "id": gid, "title": title or f"Game {i}", "type": "game",
        "deal": {
            "shop": {"name": "Steam"},
            "price": {"amount": price, "currency": "INR"},
            "regular": {"amount": price * 4, "currency": "INR"},
            "cut": cut,
            "url": f"https://store.steampowered.com/app/{700000 + i}",
        },
    }


def _itad_offer(price: float, cut: int, shop: str = "Steam") -> dict:
    return {"shop": {"name": shop}, "price": {"amount": price, "currency": "INR"},
            "regular": {"amount": price * 4, "currency": "INR"}, "cut": cut,
            "url": "https://store.steampowered.com/app/900000"}


@responses.activate
def test_end_to_end_pipeline_feeds_to_dry_run(tmp_path, monkeypatch):
    # Real collect -> verify -> merge -> publisher tagging -> quality split ->
    # watchlist injection -> notifier (capture). Only enrichment/wishlist/all-store
    # collaborators are neutered (they are not part of the path under test).
    settings = Settings(
        db_path=tmp_path / "e2e.db", discord_webhook_url="https://discord.test/wh",
        itad_api_key="k", stores=["Steam"], preferred_publishers=["Capcom"],
        watchlist=["Watched Game"], feed_page_sleep=0, discovery_discount_pct=1,
        min_discount_pct=70, publisher_metadata_batch=3,
    )

    # Pre-resolve the watchlist title (no lookup HTTP / sleep) and pre-seed one
    # filler discount as already seen so it is NOT re-posted this run.
    db = Database(settings.db_path)
    db.upsert_title_id("Watched Game", "g-watch")
    db.mark_seen(Deal(title="Game 1", store="Steam", url="u", source="itad",
                      source_game_id="g1", price_new=200.0, discount_pct=80))

    # --- feeds ---
    responses.add(responses.GET, EPIC_URL, body=_fx("epic_free.json"),
                  content_type="application/json")
    responses.add(responses.GET, STEAM_FEATURED_URL, body=_fx("steam_featured.json"),
                  content_type="application/json")

    # ITAD: 2 pages. Page 0 is a full 200-entry page (forces a second request) and
    # carries the Capcom title (deepest cut, enriched first within the budget).
    page0 = {"hasMore": True, "list": (
        [_itad_steam_entry(0, cut=95, price=250.0, gid="g-capcom", title="Capcom Hit")]
        + [_itad_steam_entry(i, cut=80, price=200.0) for i in range(1, 200)]
    )}
    page1 = {"hasMore": False, "list": [_itad_steam_entry(200, cut=80, price=200.0)]}
    responses.add(responses.GET, ITAD_DEALS_URL, json=page0)
    responses.add(responses.GET, ITAD_DEALS_URL, json=page1)

    # CheapShark: stores + 2 pages (60 + short) for the single allowed Steam store.
    responses.add(responses.GET, CHEAPSHARK_STORES_URL, body=_fx("cheapshark_stores.json"),
                  content_type="application/json")
    cs0 = [{"title": f"CS {i}", "dealID": f"D{i}", "storeID": "1", "gameID": str(i),
            "salePrice": "3.00", "normalPrice": "15.00", "savings": "80.0",
            "steamAppID": str(800000 + i)} for i in range(60)]
    cs1 = [{"title": f"CS {i}", "dealID": f"D{i}", "storeID": "1", "gameID": str(i),
            "salePrice": "3.00", "normalPrice": "15.00", "savings": "80.0",
            "steamAppID": str(800000 + i)} for i in range(60, 64)]
    responses.add(responses.GET, CHEAPSHARK_DEALS_URL, json=cs0,
                  headers={"X-Total-Page-Count": "2"})
    responses.add(responses.GET, CHEAPSHARK_DEALS_URL, json=cs1,
                  headers={"X-Total-Page-Count": "2"})

    # ITAD bulk appid->id lookup (CheapShark verification).
    def _shop_cb(request):
        keys = json.loads(request.body)
        return (200, {}, json.dumps({k: f"gid-{k.split('/')[1]}" for k in keys}))
    responses.add_callback(responses.POST, SHOP_LOOKUP_URL, callback=_shop_cb,
                           content_type="application/json")

    # ITAD prices (used by both INR verify and the watchlist).
    def _prices_cb(request):
        ids = json.loads(request.body)
        out = []
        for gid in ids:
            price, cut = (300.0, 77) if gid == "g-watch" else (350.0, 77)
            out.append({"id": gid, "deals": [_itad_offer(price, cut)]})
        return (200, {}, json.dumps(out))
    responses.add_callback(responses.POST, ITAD_PRICES_URL, callback=_prices_cb,
                           content_type="application/json")

    # ITAD info (publisher attachment): Capcom for the Capcom id, generic otherwise.
    def _info_cb(request):
        gid = request.params.get("id")
        name = "CAPCOM Co., Ltd." if gid == "g-capcom" else "Indie Co"
        body = {"publishers": [{"name": name}], "developers": [{"name": name}]}
        return (200, {}, json.dumps(body))
    responses.add_callback(responses.GET, ITAD_INFO_URL, callback=_info_cb,
                           content_type="application/json")

    # Neuter collaborators outside the path under test.
    monkeypatch.setattr(m.WishlistMatcher, "fetch_appids", lambda self: set())
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

    assert m.run_pipeline(settings, dry_run=True) == 0

    free_titles = {d.title for d in captured["free"]}
    pref_titles = [d.title for d in captured["preferred"]]
    disc_titles = {d.title for d in captured["discounts"]}

    # Free games surfaced (Steam freebie; Epic is filtered out by stores=[Steam]).
    assert "Keepable Freebie" in free_titles
    # Capcom title attached a publisher on the live feed -> Quality picks (preferred).
    assert "Capcom Hit" in pref_titles
    # Watchlist title injected at the top of the preferred section (priced via ITAD).
    assert pref_titles[0] == "Watched Game"
    assert "Capcom Hit" in pref_titles
    # CheapShark deals were INR-verified and merged into the discounts section.
    assert any(t.startswith("CS ") for t in disc_titles)
    # The pre-seen filler deal is NOT re-posted (dedup intact).
    assert "Game 1" not in disc_titles
