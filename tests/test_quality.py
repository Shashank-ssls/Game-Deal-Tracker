"""Tests for quality classification: preferred match, gate, and split."""

from __future__ import annotations

from src.config import Settings
from src.models import Deal
from src.quality import (
    is_excluded,
    is_preferred,
    is_premium,
    matches_franchise,
    passes_gate,
    split_quality,
    store_allowed,
)

PREFERRED = ["Capcom", "Bandai Namco", "FromSoftware", "SEGA"]


def _deal(title, *, publisher=None, developer=None, review_pct=None, metacritic=None,
          price_old=1000.0, price_new=200.0, discount_pct=80, review_count=None,
          genres=None, coming_soon=False):
    return Deal(
        title=title, store="Steam", url=f"https://s/{title}", source="itad",
        source_game_id=title, price_old=price_old, price_new=price_new, currency="INR",
        discount_pct=discount_pct, steam_appid=hash(title) % 100000,
        publisher=publisher, developer=developer, review_pct=review_pct, metacritic=metacritic,
        review_count=review_count, genres=genres, coming_soon=coming_soon,
    )


def test_is_preferred_matches_publisher_case_insensitive():
    assert is_preferred(_deal("A", publisher="CAPCOM Co., Ltd."), PREFERRED) is True


def test_is_preferred_matches_developer():
    # FromSoftware develops; Bandai Namco publishes — matching dev must work.
    assert is_preferred(_deal("Elden Ring", developer="FromSoftware Inc.",
                              publisher="Bandai Namco Entertainment"), PREFERRED) is True


def test_is_preferred_no_match():
    assert is_preferred(_deal("Indie", publisher="Tiny Studio"), PREFERRED) is False


def test_is_preferred_empty_allowlist():
    assert is_preferred(_deal("A", publisher="Capcom"), []) is False


def test_is_preferred_word_boundary_avoids_false_positive():
    # "EA" must NOT match "Creative Assembly" (contains the letters e-a).
    assert is_preferred(_deal("Total War", developer="Creative Assembly"), ["EA"]) is False
    # ...but does match a real publisher.
    assert is_preferred(_deal("FIFA", publisher="Electronic Arts (EA)"), ["EA"]) is True


def test_is_preferred_matches_full_studio_names():
    assert is_preferred(_deal("Hades", developer="Supergiant Games"),
                        ["Supergiant Games"]) is True
    assert is_preferred(_deal("Don't Starve", publisher="Klei Entertainment"),
                        ["Klei Entertainment"]) is True
    assert is_preferred(_deal("X", publisher="CAPCOM Co., Ltd."), ["Capcom"]) is True


def test_gate_drops_below_review_threshold():
    settings = Settings(min_review_pct=80)
    assert passes_gate(_deal("Good", review_pct=90), settings) is True
    assert passes_gate(_deal("Bad", review_pct=40), settings) is False


def test_gate_passes_unknown_review():
    settings = Settings(min_review_pct=80)
    assert passes_gate(_deal("Unknown", review_pct=None), settings) is True


def test_gate_metacritic_soft():
    settings = Settings(min_metacritic=75)
    assert passes_gate(_deal("Low", metacritic=50), settings) is False
    assert passes_gate(_deal("NoScore", metacritic=None), settings) is True


def test_gate_off_by_default():
    assert passes_gate(_deal("Anything", review_pct=10, metacritic=10), Settings()) is True


def test_min_review_count_requires_enough_reviews():
    settings = Settings(min_review_pct=80, min_review_count=50)
    # 95% but only 12 reviews -> not trusted -> fails.
    assert passes_gate(_deal("Thin", review_pct=95, review_count=12), settings) is False
    # 95% from 500 reviews -> passes.
    assert passes_gate(_deal("Solid", review_pct=95, review_count=500), settings) is True


def test_exclude_early_access_by_genre_and_coming_soon():
    settings = Settings(exclude_early_access=True)
    assert is_excluded(_deal("EA", genres="Action, Early Access"), settings) is True
    assert is_excluded(_deal("Unreleased", coming_soon=True), settings) is True
    assert is_excluded(_deal("Done", genres="Action, RPG"), settings) is False


def test_exclude_genres():
    settings = Settings(exclude_genres=["Sexual Content", "Gore"])
    assert is_excluded(_deal("NSFW", genres="Casual, Sexual Content"), settings) is True
    assert is_excluded(_deal("Clean", genres="Action"), settings) is False


def test_unknown_genres_not_excluded():
    settings = Settings(exclude_early_access=True, exclude_genres=["Gore"])
    assert is_excluded(_deal("NonSteam", genres=None), settings) is False


def test_include_genres_keeps_only_matching():
    settings = Settings(include_genres=["RPG", "Action"])
    assert is_excluded(_deal("RPG game", genres="Indie, RPG"), settings) is False
    assert is_excluded(_deal("Puzzle game", genres="Casual, Puzzle"), settings) is True


def test_include_genres_keeps_unknown_genres():
    # No genre data -> not dropped (don't punish non-Steam deals).
    settings = Settings(include_genres=["RPG"])
    assert is_excluded(_deal("NonSteam", genres=None), settings) is False


def test_exclude_wins_over_include():
    settings = Settings(include_genres=["RPG"], exclude_genres=["Gore"])
    assert is_excluded(_deal("Bloody RPG", genres="RPG, Gore"), settings) is True


def test_matches_franchise_substring_case_insensitive():
    franchises = ["Resident Evil", "Dark Souls"]
    assert matches_franchise(_deal("RESIDENT EVIL 4"), franchises) is True
    assert matches_franchise(_deal("Dark Souls III"), franchises) is True
    assert matches_franchise(_deal("Hollow Knight"), franchises) is False
    assert matches_franchise(_deal("Anything"), []) is False


def test_franchise_promoted_into_preferred_even_subthreshold():
    settings = Settings(min_discount_pct=70, franchises=["Resident Evil"])
    deals = [
        _deal("Resident Evil 4", publisher="Indie Co", discount_pct=55,
              price_old=1000, price_new=450),  # below threshold, not premium
        _deal("Random Indie", publisher="Indie Co", discount_pct=55,
              price_old=1000, price_new=450),  # dropped: sub-threshold, no promo
    ]
    preferred, remaining = split_quality(deals, settings)
    assert [d.title for d in preferred] == ["Resident Evil 4"]
    assert preferred[0].is_preferred is True
    assert "Random Indie" not in {d.title for d in remaining}


def _store_deal(store):
    return Deal(title="G", store=store, url="u", source="itad", source_game_id="g",
                price_old=1000.0, price_new=200.0, discount_pct=80)


def test_store_allowlist():
    s = Settings(stores=["Steam", "GOG"])
    assert store_allowed(_store_deal("Steam"), s) is True
    assert store_allowed(_store_deal("GOG"), s) is True
    assert store_allowed(_store_deal("Fanatical"), s) is False


def test_store_allowlist_substring_and_case():
    s = Settings(stores=["epic"])
    assert store_allowed(_store_deal("Epic Games"), s) is True
    assert store_allowed(_store_deal("Epic Game Store"), s) is True


def test_store_blocklist():
    s = Settings(exclude_stores=["Fanatical", "GameBillet"])
    assert store_allowed(_store_deal("Steam"), s) is True
    assert store_allowed(_store_deal("Fanatical"), s) is False


def test_no_store_filter_allows_all():
    assert store_allowed(_store_deal("Anything"), Settings()) is True


def test_keep_filter_drops_subthreshold_non_premium():
    # discovery may surface a 60% deal; it stays only if premium/preferred.
    settings = Settings(min_discount_pct=70, preferred_publishers=[])
    deals = [
        _deal("Mid Deal", discount_pct=60, price_old=900, price_new=360),   # 60%, not premium
        _deal("Big Deal", discount_pct=85, price_old=900, price_new=135),   # >=70 kept
        _deal("Premium 60", discount_pct=60, price_old=2000, price_new=400),  # 60% but premium
    ]
    _, remaining = split_quality(deals, settings)
    titles = {d.title for d in remaining}
    assert "Big Deal" in titles
    assert "Premium 60" in titles   # kept because premium
    assert "Mid Deal" not in titles  # dropped: sub-threshold and not premium


def test_is_premium_thresholds():
    settings = Settings()  # defaults: original > 1000, sale <= 500
    assert is_premium(_deal("Steal", price_old=2999, price_new=499), settings) is True
    assert is_premium(_deal("CheapBase", price_old=800, price_new=200), settings) is False
    assert is_premium(_deal("StillPricey", price_old=2000, price_new=900), settings) is False
    assert is_premium(_deal("Edge", price_old=1001, price_new=500), settings) is True


def test_premium_picks_sort_first_and_flagged():
    settings = Settings()
    deals = [
        _deal("Cheapie", price_old=600, price_new=120),       # high % but not premium
        _deal("AAA Steal", price_old=2999, price_new=499),    # premium
    ]
    _, remaining = split_quality(deals, settings)
    assert remaining[0].title == "AAA Steal"
    assert remaining[0].is_premium_deal is True
    assert remaining[1].is_premium_deal is False


def test_split_pulls_preferred_and_gates_rest():
    settings = Settings(preferred_publishers=PREFERRED, min_review_pct=80)
    deals = [
        _deal("Capcom Game", publisher="Capcom", review_pct=30),  # preferred -> kept despite low %
        _deal("Great Indie", publisher="Indie Co", review_pct=95),  # passes gate
        _deal("Junk", publisher="Indie Co", review_pct=40),         # gated out
    ]
    preferred, remaining = split_quality(deals, settings)

    assert [d.title for d in preferred] == ["Capcom Game"]
    assert preferred[0].is_preferred is True
    assert {d.title for d in remaining} == {"Great Indie"}
