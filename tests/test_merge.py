"""Phase 3 tests: cross-store dedupe, free-beats-paid, ordering."""

from __future__ import annotations

from src.merge import merge_deals
from src.models import Deal


def _paid(title, *, appid=None, price, discount, review_pct=None, store="Steam"):
    return Deal(
        title=title, store=store, url=f"https://store/{title}",
        source=store.lower(), source_game_id=title,
        price_old=price * 2, price_new=price, currency="INR",
        discount_pct=discount, steam_appid=appid, review_pct=review_pct,
    )


def _free(title, *, appid=None, temporary=False):
    return Deal(
        title=title, store="Epic Games", url=f"https://store/{title}",
        source="epic", source_game_id=title, is_free=True, is_temporary=temporary,
        discount_pct=100, price_new=0.0, steam_appid=appid,
    )


def test_cross_store_dedupe_keeps_cheapest():
    steam = _paid("Same Game", appid=42, price=449, discount=70, store="Steam")
    gog = _paid("Same Game", appid=42, price=399, discount=75, store="GOG")
    _, discounts = merge_deals([steam, gog])
    assert len(discounts) == 1
    assert discounts[0].store == "GOG"
    assert discounts[0].price_new == 399


def test_cross_store_dedupe_three_stores_keeps_cheapest():
    # The same game arrives from Steam, Epic and GOG (larger paginated feed); the
    # cheapest INR offer wins and only one entry survives.
    steam = _paid("Triple Game", appid=99, price=899, discount=50, store="Steam")
    epic = _paid("Triple Game", appid=99, price=799, discount=55, store="Epic")
    gog = _paid("Triple Game", appid=99, price=699, discount=60, store="GOG")
    _, discounts = merge_deals([steam, epic, gog])
    assert len(discounts) == 1
    assert discounts[0].store == "GOG"
    assert discounts[0].price_new == 699


def test_free_beats_paid_for_same_game():
    paid = _paid("Hybrid Game", appid=7, price=100, discount=90)
    free = _free("Hybrid Game", appid=7)
    free_deals, discounts = merge_deals([paid, free])
    assert [d.title for d in free_deals] == ["Hybrid Game"]
    assert discounts == []  # not also listed as a paid discount


def test_keepable_freebie_preferred_over_temporary():
    weekend = _free("Promo Game", appid=9, temporary=True)
    keepable = _free("Promo Game", appid=9, temporary=False)
    free_deals, _ = merge_deals([weekend, keepable])
    assert len(free_deals) == 1
    assert free_deals[0].is_temporary is False


def test_discounts_sorted_by_discount_then_rating():
    a = _paid("A", appid=1, price=100, discount=70, review_pct=95)
    b = _paid("B", appid=2, price=100, discount=90, review_pct=50)
    c = _paid("C", appid=3, price=100, discount=70, review_pct=80)
    _, discounts = merge_deals([a, b, c])
    # 90% first; then the two 70% deals ordered by review desc (A 95 before C 80).
    assert [d.title for d in discounts] == ["B", "A", "C"]


def test_non_game_bundles_excluded():
    deals = [
        _paid("Business Certification Bundle", price=476, discount=99),
        _paid("Easy Game Engine eLearning Bundle", price=762, discount=99),
        _paid("Real Game", appid=5, price=300, discount=80),
    ]
    _, discounts = merge_deals(deals)
    assert [d.title for d in discounts] == ["Real Game"]


def test_title_dedupe_when_no_appid():
    one = _paid("Cool Game!", price=300, discount=80, store="Steam")
    two = _paid("cool  game", price=250, discount=85, store="GOG")
    _, discounts = merge_deals([one, two])
    assert len(discounts) == 1
    assert discounts[0].price_new == 250
