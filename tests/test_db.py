"""Phase 1 tests: dedup, deeper-discount re-notify, purge, ratings cache, run log."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from src.db import Database
from src.models import Deal


def make_deal(
    *,
    source: str = "steam",
    source_game_id: str = "440",
    discount_pct: int = 70,
    price_new: float = 300.0,
    steam_appid: int | None = 440,
) -> Deal:
    return Deal(
        title="Team Fortress 2",
        store="Steam",
        url="https://store.steampowered.com/app/440",
        source=source,
        source_game_id=source_game_id,
        price_old=1000.0,
        price_new=price_new,
        discount_pct=discount_pct,
        steam_appid=steam_appid,
    )


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "test.db")


def test_db_file_and_tables_created(tmp_path):
    path = tmp_path / "sub" / "tracker.db"
    Database(path)  # parent dir auto-created
    assert path.exists()
    with sqlite3.connect(path) as conn:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    assert {"deals_seen", "games", "runs"} <= names


def test_same_deal_twice_is_not_new(db):
    deal = make_deal()
    assert db.is_new(deal) is True
    db.mark_seen(deal)
    assert db.is_new(deal) is False


def test_deeper_discount_notifies_again(db):
    first = make_deal(discount_pct=70, price_new=300.0)
    assert db.is_new(first) is True
    db.mark_seen(first)

    deeper = make_deal(discount_pct=85, price_new=150.0)
    assert db.is_new(deeper) is True  # got even cheaper
    db.mark_seen(deeper)
    assert db.is_new(deeper) is False  # already reported at 85%


def test_shallower_discount_does_not_renotify(db):
    db.mark_seen(make_deal(discount_pct=80, price_new=200.0))
    shallower = make_deal(discount_pct=60, price_new=400.0)
    assert db.is_new(shallower) is False


def test_purge_removes_old_rows(db, tmp_path):
    deal = make_deal()
    db.mark_seen(deal)
    # Backdate last_seen well beyond the purge window.
    old = (datetime.now(UTC) - timedelta(days=99)).isoformat()
    with sqlite3.connect(db.db_path) as conn:
        conn.execute("UPDATE deals_seen SET last_seen = ?", (old,))
        conn.commit()

    removed = db.purge_expired(days=30)
    assert removed == 1
    assert db.is_new(deal) is True  # forgotten, so new again


def test_purge_keeps_recent_rows(db):
    db.mark_seen(make_deal())
    assert db.purge_expired(days=30) == 0


def test_publisher_developer_roundtrip(db):
    db.upsert_game(440, title="TF2", publisher="Valve", developer="Valve Corp")
    cached = db.get_cached_game(440)
    assert cached["publisher"] == "Valve"
    assert cached["developer"] == "Valve Corp"


def test_migration_adds_columns_to_legacy_db(tmp_path):
    path = tmp_path / "legacy.db"
    # Simulate a pre-feature games table without publisher/developer columns.
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE games (steam_appid INTEGER PRIMARY KEY, title TEXT, "
            "review_summary TEXT, review_pct INTEGER, review_count INTEGER, "
            "metacritic INTEGER, image_url TEXT, fetched_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO games (steam_appid, title, fetched_at) VALUES (1, 'Old', '2020-01-01')"
        )
        conn.commit()

    db = Database(path)  # _migrate() should add the new columns
    cached = db.get_cached_game(1)
    assert "publisher" in cached and cached["publisher"] is None
    db.upsert_game(1, title="Old", publisher="Now Has One")
    assert db.get_cached_game(1)["publisher"] == "Now Has One"


def test_game_cache_roundtrip(db):
    assert db.get_cached_game(440) is None
    db.upsert_game(
        440,
        title="Team Fortress 2",
        review_summary="Very Positive",
        review_pct=92,
        review_count=12345,
        metacritic=92,
        image_url="https://img/440.jpg",
    )
    cached = db.get_cached_game(440)
    assert cached is not None
    assert cached["title"] == "Team Fortress 2"
    assert cached["review_summary"] == "Very Positive"
    assert cached["review_pct"] == 92
    assert cached["review_count"] == 12345
    assert cached["metacritic"] == 92
    assert cached["image_url"] == "https://img/440.jpg"
    assert cached["fetched_at"]


def test_upsert_game_updates_existing(db):
    db.upsert_game(440, review_pct=50)
    db.upsert_game(440, review_pct=95, review_summary="Overwhelmingly Positive")
    cached = db.get_cached_game(440)
    assert cached["review_pct"] == 95
    assert cached["review_summary"] == "Overwhelmingly Positive"


def test_reminders_roundtrip(db):
    assert db.was_reminded("epic|123") is False
    db.mark_reminded("epic|123")
    assert db.was_reminded("epic|123") is True
    db.mark_reminded("epic|123")  # idempotent
    assert db.was_reminded("epic|123") is True


def test_recent_runs_newest_first(db):
    db.log_run(deals_found=1, deals_new=1)
    db.log_run(deals_found=2, deals_new=0, errors=["x"])
    runs = db.recent_runs(limit=10)
    assert len(runs) == 2
    assert runs[0]["deals_found"] == 2  # newest first
    assert runs[1]["deals_found"] == 1


def test_app_index_replace_and_lookup(db):
    assert db.app_index_age_days() is None
    db.replace_app_index([(1, "doom"), (2, "doom"), (3, "eldenring")])
    assert db.lookup_appids("eldenring") == [3]
    assert sorted(db.lookup_appids("doom")) == [1, 2]
    assert db.lookup_appids("missing") == []
    assert db.app_index_age_days() is not None


def test_app_index_replace_is_atomic_swap(db):
    db.replace_app_index([(1, "old")])
    db.replace_app_index([(2, "new")])
    assert db.lookup_appids("old") == []  # previous contents gone
    assert db.lookup_appids("new") == [2]


def test_derived_watchlist_roundtrip(db):
    assert db.derived_watchlist_age_days() is None
    assert db.get_derived_watchlist() == []
    db.replace_derived_watchlist(["Resident Evil 4", "Monster Hunter", "Resident Evil 4 "])
    assert set(db.get_derived_watchlist()) == {"Resident Evil 4", "Monster Hunter"}  # deduped
    assert db.derived_watchlist_age_days() is not None
    db.replace_derived_watchlist(["Street Fighter 6"])
    assert db.get_derived_watchlist() == ["Street Fighter 6"]  # atomic swap


def test_price_history_records_and_finds_min(db):
    deal = make_deal(source="itad", source_game_id="g1", price_new=500.0)
    # Backdate two prior observations.
    with sqlite3.connect(db.db_path) as conn:
        for day_ago, price in ((3, 700.0), (1, 600.0)):
            d = (datetime.now(UTC) - timedelta(days=day_ago)).date().isoformat()
            conn.execute(
                "INSERT INTO price_history (source, source_game_id, observed_on, price_new) "
                "VALUES (?, ?, ?, ?)", ("itad", "g1", d, price))
        conn.commit()
    assert db.min_price_before_today("itad", "g1", days=7) == 600.0
    # Today's record does not count toward the "before today" minimum.
    db.record_price(deal)
    assert db.min_price_before_today("itad", "g1", days=7) == 600.0


def test_record_price_one_per_day(db):
    db.record_price(make_deal(source="itad", source_game_id="g2", price_new=300.0))
    db.record_price(make_deal(source="itad", source_game_id="g2", price_new=250.0))  # ignored
    with sqlite3.connect(db.db_path) as conn:
        rows = conn.execute(
            "SELECT price_new FROM price_history WHERE source_game_id='g2'").fetchall()
    assert [r[0] for r in rows] == [300.0]  # first write of the day wins


def test_purge_old_prices(db):
    with sqlite3.connect(db.db_path) as conn:
        old = (datetime.now(UTC) - timedelta(days=40)).date().isoformat()
        conn.execute("INSERT INTO price_history VALUES ('itad','g3',?,100.0)", (old,))
        conn.commit()
    assert db.purge_old_prices(days=30) == 1


def test_log_run_increments_ids(db):
    first_id = db.log_run(deals_found=10, deals_new=3)
    second_id = db.log_run(deals_found=5, deals_new=0, errors=["epic timeout"])
    assert second_id > first_id
    with sqlite3.connect(db.db_path) as conn:
        row = conn.execute(
            "SELECT deals_found, deals_new, errors FROM runs WHERE id = ?",
            (second_id,),
        ).fetchone()
    assert row[0] == 5 and row[1] == 0
    assert "epic timeout" in row[2]
