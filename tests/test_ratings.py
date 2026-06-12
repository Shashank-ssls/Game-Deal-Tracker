"""Phase 4 tests: ratings cache hit (0 HTTP), cache expiry refetch, graceful failure."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import responses

from src.config import Settings
from src.db import Database
from src.enrich.ratings import APPDETAILS_URL, APPREVIEWS_URL, RatingsEnricher
from src.models import Deal

REVIEWS_BODY = {
    "success": 1,
    "query_summary": {
        "review_score_desc": "Very Positive",
        "total_positive": 940,
        "total_reviews": 1000,
    },
}
DETAILS_BODY = {
    "440": {
        "success": True,
        "data": {
            "name": "Team Fortress 2",
            "metacritic": {"score": 92},
            "header_image": "https://img/440header.jpg",
            "publishers": ["Valve"],
            "developers": ["Valve"],
        },
    }
}


def _noop_sleep(_seconds: float) -> None:
    return None


def _enricher(db: Database) -> RatingsEnricher:
    return RatingsEnricher(Settings(), db, sleeper=_noop_sleep)


def _steam_deal(appid: int = 440, image_url=None) -> Deal:
    return Deal(
        title="Team Fortress 2", store="Steam",
        url="https://store.steampowered.com/app/440",
        source="steam", source_game_id=str(appid),
        discount_pct=80, price_new=100.0, steam_appid=appid, image_url=image_url,
    )


def _register_steam():
    responses.add(responses.GET, APPREVIEWS_URL.format(appid=440), json=REVIEWS_BODY)
    responses.add(responses.GET, APPDETAILS_URL, json=DETAILS_BODY)


@responses.activate
def test_cache_hit_makes_zero_http_calls(tmp_path):
    db = Database(tmp_path / "t.db")
    db.upsert_game(440, title="TF2", review_summary="Cached Positive", review_pct=88,
                   review_count=500, metacritic=90, image_url="https://img/cached.jpg",
                   publisher="Valve", developer="Valve")

    result = _enricher(db).enrich([_steam_deal()])

    assert len(responses.calls) == 0  # served entirely from cache
    deal = result[0]
    assert deal.review_summary == "Cached Positive"
    assert deal.review_pct == 88
    assert deal.image_url == "https://img/cached.jpg"
    assert deal.publisher == "Valve"


@responses.activate
def test_cache_expiry_refetches(tmp_path):
    db = Database(tmp_path / "t.db")
    db.upsert_game(440, review_summary="Stale", review_pct=10)
    # Backdate beyond ratings_cache_days (default 7).
    old = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    with sqlite3.connect(db.db_path) as conn:
        conn.execute("UPDATE games SET fetched_at = ?", (old,))
        conn.commit()
    _register_steam()

    result = _enricher(db).enrich([_steam_deal()])

    assert len(responses.calls) > 0  # stale -> refetched
    deal = result[0]
    assert deal.review_summary == "Very Positive"
    assert deal.review_pct == 94  # 940 / 1000
    assert deal.review_count == 1000
    assert deal.metacritic == 92
    assert deal.image_url == "https://img/440header.jpg"  # filled when Deal had none
    assert deal.publisher == "Valve"
    assert deal.developer == "Valve"


@responses.activate
def test_fresh_fetch_then_second_run_uses_cache(tmp_path):
    db = Database(tmp_path / "t.db")
    _register_steam()

    _enricher(db).enrich([_steam_deal()])
    calls_after_first = len(responses.calls)
    assert calls_after_first > 0

    _enricher(db).enrich([_steam_deal()])
    assert len(responses.calls) == calls_after_first  # no new HTTP on second run


@responses.activate
def test_appreviews_failure_leaves_deal_usable(tmp_path):
    db = Database(tmp_path / "t.db")
    responses.add(responses.GET, APPREVIEWS_URL.format(appid=440), json={"e": "x"}, status=500)
    responses.add(responses.GET, APPDETAILS_URL, json={"e": "x"}, status=500)

    deal = _enricher(db).enrich([_steam_deal()])[0]
    assert deal.review_summary is None
    assert deal.review_pct is None
    assert deal.metacritic is None


@responses.activate
def test_pre_migration_row_refetches_to_populate_publisher(tmp_path):
    db = Database(tmp_path / "t.db")
    # A row cached before publisher capture: fresh timestamp but publisher NULL.
    db.upsert_game(440, review_summary="Old", review_pct=80)
    _register_steam()

    deal = _enricher(db).enrich([_steam_deal()])[0]

    assert len(responses.calls) > 0          # refetched despite being "fresh"
    assert deal.publisher == "Valve"


@responses.activate
def test_non_steam_deal_untouched(tmp_path):
    db = Database(tmp_path / "t.db")
    epic = Deal(title="Epic Game", store="Epic Games", url="https://epic/p/x",
                source="epic", source_game_id="x", is_free=True, discount_pct=100,
                price_new=0.0)
    result = _enricher(db).enrich([epic])
    assert len(responses.calls) == 0
    assert result[0].review_summary is None
