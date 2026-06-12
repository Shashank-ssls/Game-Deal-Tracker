"""SQLite persistence layer.

Three responsibilities, no networking (see ARCHITECTURE.md):
  * ``deals_seen`` — dedup memory, so a deal is reported at most once unless it
    gets a deeper discount.
  * ``games`` — ratings/metadata cache to keep API calls low (Phase 4).
  * ``runs`` — a run log for debugging.

The DB file defaults to ``data/tracker.db`` (gitignored, auto-created). WAL mode
is enabled so reads and writes don't block each other.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.models import Deal

SCHEMA = """
CREATE TABLE IF NOT EXISTS deals_seen (
    hash            TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    source_game_id  TEXT NOT NULL,
    discount_pct    INTEGER NOT NULL,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_seen_game ON deals_seen(source, source_game_id);

CREATE TABLE IF NOT EXISTS games (
    steam_appid     INTEGER PRIMARY KEY,
    title           TEXT,
    review_summary  TEXT,
    review_pct      INTEGER,
    review_count    INTEGER,
    metacritic      INTEGER,
    image_url       TEXT,
    publisher       TEXT,
    developer       TEXT,
    genres          TEXT,
    coming_soon     INTEGER,
    fetched_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    deals_found INTEGER NOT NULL,
    deals_new   INTEGER NOT NULL,
    errors      TEXT
);

CREATE TABLE IF NOT EXISTS reminders (
    key          TEXT PRIMARY KEY,   -- source|source_game_id
    reminded_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_index (
    appid INTEGER PRIMARY KEY,
    norm  TEXT NOT NULL              -- normalized name for title->appid lookup
);
CREATE INDEX IF NOT EXISTS idx_app_norm ON app_index(norm);

CREATE TABLE IF NOT EXISTS app_index_meta (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    fetched_at TEXT NOT NULL,
    count      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS derived_watchlist (
    title TEXT PRIMARY KEY      -- titles auto-derived from preferred publishers
);
CREATE TABLE IF NOT EXISTS derived_watchlist_meta (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    fetched_at TEXT NOT NULL,
    count      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS price_history (
    source         TEXT NOT NULL,
    source_game_id TEXT NOT NULL,
    observed_on    TEXT NOT NULL,   -- date (YYYY-MM-DD), one row per game per day
    price_new      REAL,
    PRIMARY KEY (source, source_game_id, observed_on)
);
"""


def _utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string (sortable as text)."""
    return datetime.now(UTC).isoformat()


class Database:
    """Thin wrapper over a SQLite file. One instance per DB path."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        self._migrate()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _migrate(self) -> None:
        """Add columns introduced after a DB was first created (idempotent)."""
        with self._connect() as conn:
            existing = {row["name"] for row in conn.execute("PRAGMA table_info(games)")}
            for column, decl in (
                ("publisher", "TEXT"),
                ("developer", "TEXT"),
                ("genres", "TEXT"),
                ("coming_soon", "INTEGER"),
            ):
                if column not in existing:
                    conn.execute(f"ALTER TABLE games ADD COLUMN {column} {decl}")

    # -- dedup ---------------------------------------------------------------

    def is_new(self, deal: Deal) -> bool:
        """True if this deal should be reported.

        A deal is new when its exact hash has never been seen AND either the game
        has never been seen before, or this discount is strictly deeper than any
        previously recorded discount for the same ``(source, source_game_id)``.
        This re-notifies on "got even cheaper" while staying quiet on equal or
        shallower re-offers.
        """
        deal_hash = deal.dedup_hash()
        with self._connect() as conn:
            seen = conn.execute(
                "SELECT 1 FROM deals_seen WHERE hash = ?", (deal_hash,)
            ).fetchone()
            if seen is not None:
                return False
            row = conn.execute(
                "SELECT MAX(discount_pct) AS max_pct FROM deals_seen "
                "WHERE source = ? AND source_game_id = ?",
                (deal.source, deal.source_game_id),
            ).fetchone()

        prev_max = row["max_pct"] if row is not None else None
        if prev_max is None:
            return True  # never seen this game
        return deal.discount_pct > prev_max

    def mark_seen(self, deal: Deal) -> None:
        """Record a deal as reported (upsert; refreshes ``last_seen``)."""
        deal_hash = deal.dedup_hash()
        now = _utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO deals_seen
                    (hash, source, source_game_id, discount_pct, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(hash) DO UPDATE SET last_seen = excluded.last_seen
                """,
                (deal_hash, deal.source, deal.source_game_id, deal.discount_pct, now, now),
            )

    def purge_expired(self, days: int) -> int:
        """Delete ``deals_seen`` rows older than ``days``. Returns rows removed."""
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM deals_seen WHERE last_seen < ?", (cutoff,))
            return cur.rowcount

    # -- ratings cache -------------------------------------------------------

    def get_cached_game(self, steam_appid: int) -> dict | None:
        """Return the cached game row as a dict, or ``None`` if absent."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM games WHERE steam_appid = ?", (steam_appid,)
            ).fetchone()
        return dict(row) if row is not None else None

    def upsert_game(
        self,
        steam_appid: int,
        *,
        title: str | None = None,
        review_summary: str | None = None,
        review_pct: int | None = None,
        review_count: int | None = None,
        metacritic: int | None = None,
        image_url: str | None = None,
        publisher: str | None = None,
        developer: str | None = None,
        genres: str | None = None,
        coming_soon: bool | None = None,
    ) -> None:
        """Insert or refresh a cached game's metadata/ratings."""
        now = _utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO games
                    (steam_appid, title, review_summary, review_pct, review_count,
                     metacritic, image_url, publisher, developer, genres, coming_soon,
                     fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(steam_appid) DO UPDATE SET
                    title          = excluded.title,
                    review_summary = excluded.review_summary,
                    review_pct     = excluded.review_pct,
                    review_count   = excluded.review_count,
                    metacritic     = excluded.metacritic,
                    image_url      = excluded.image_url,
                    publisher      = excluded.publisher,
                    developer      = excluded.developer,
                    genres         = excluded.genres,
                    coming_soon    = excluded.coming_soon,
                    fetched_at     = excluded.fetched_at
                """,
                (
                    steam_appid,
                    title,
                    review_summary,
                    review_pct,
                    review_count,
                    metacritic,
                    image_url,
                    publisher,
                    developer,
                    genres,
                    None if coming_soon is None else int(coming_soon),
                    now,
                ),
            )

    # -- Steam appid index ---------------------------------------------------

    def app_index_age_days(self) -> float | None:
        """Age of the cached app index in days, or ``None`` if never built."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT fetched_at FROM app_index_meta WHERE id = 1"
            ).fetchone()
        if row is None:
            return None
        fetched = datetime.fromisoformat(row["fetched_at"])
        return (datetime.now(UTC) - fetched).total_seconds() / 86400

    def replace_app_index(self, pairs: Iterable[tuple[int, str]]) -> int:
        """Atomically replace the whole appid->norm index. Returns row count."""
        rows = [(int(appid), norm) for appid, norm in pairs if norm]
        now = _utc_now_iso()
        with self._connect() as conn:
            conn.execute("DELETE FROM app_index")
            conn.executemany(
                "INSERT OR REPLACE INTO app_index (appid, norm) VALUES (?, ?)", rows
            )
            conn.execute(
                "INSERT INTO app_index_meta (id, fetched_at, count) VALUES (1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "fetched_at = excluded.fetched_at, count = excluded.count",
                (now, len(rows)),
            )
        return len(rows)

    def lookup_appids(self, norm: str) -> list[int]:
        """Return all appids whose normalized name equals ``norm`` (may be empty)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT appid FROM app_index WHERE norm = ?", (norm,)
            ).fetchall()
        return [int(row["appid"]) for row in rows]

    # -- derived watchlist (auto from preferred publishers) ------------------

    def derived_watchlist_age_days(self) -> float | None:
        """Age of the cached derived watchlist in days, or ``None`` if never built."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT fetched_at FROM derived_watchlist_meta WHERE id = 1"
            ).fetchone()
        if row is None:
            return None
        fetched = datetime.fromisoformat(row["fetched_at"])
        return (datetime.now(UTC) - fetched).total_seconds() / 86400

    def replace_derived_watchlist(self, titles: Iterable[str]) -> int:
        """Atomically replace the cached derived watchlist. Returns row count."""
        rows = [(t,) for t in dict.fromkeys(t.strip() for t in titles if t.strip())]
        now = _utc_now_iso()
        with self._connect() as conn:
            conn.execute("DELETE FROM derived_watchlist")
            conn.executemany("INSERT OR IGNORE INTO derived_watchlist (title) VALUES (?)", rows)
            conn.execute(
                "INSERT INTO derived_watchlist_meta (id, fetched_at, count) VALUES (1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "fetched_at = excluded.fetched_at, count = excluded.count",
                (now, len(rows)),
            )
        return len(rows)

    def get_derived_watchlist(self) -> list[str]:
        """Return cached derived watchlist titles (may be empty)."""
        with self._connect() as conn:
            rows = conn.execute("SELECT title FROM derived_watchlist").fetchall()
        return [row["title"] for row in rows]

    # -- price history (price-trend / "cheaper than before") -----------------

    def record_price(self, deal: Deal) -> None:
        """Store today's price for a game (one row per game per day; first wins)."""
        if deal.price_new is None:
            return
        today = datetime.now(UTC).date().isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO price_history "
                "(source, source_game_id, observed_on, price_new) VALUES (?, ?, ?, ?)",
                (deal.source, deal.source_game_id, today, deal.price_new),
            )

    def min_price_before_today(self, source: str, source_game_id: str, days: int) -> float | None:
        """Lowest recorded price for a game over the last ``days``, excluding today."""
        today = datetime.now(UTC).date()
        since = (today - timedelta(days=days)).isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MIN(price_new) AS low FROM price_history "
                "WHERE source = ? AND source_game_id = ? "
                "AND observed_on >= ? AND observed_on < ?",
                (source, source_game_id, since, today.isoformat()),
            ).fetchone()
        return row["low"] if row is not None else None

    def purge_old_prices(self, days: int) -> int:
        """Delete price-history rows older than ``days``. Returns rows removed."""
        cutoff = (datetime.now(UTC).date() - timedelta(days=days)).isoformat()
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM price_history WHERE observed_on < ?", (cutoff,))
            return cur.rowcount

    # -- ending-soon reminders ----------------------------------------------

    def was_reminded(self, key: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM reminders WHERE key = ?", (key,)
            ).fetchone()
        return row is not None

    def mark_reminded(self, key: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO reminders (key, reminded_at) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET reminded_at = excluded.reminded_at",
                (key, _utc_now_iso()),
            )

    # -- run log -------------------------------------------------------------

    def log_run(
        self,
        deals_found: int,
        deals_new: int,
        errors: list[str] | None = None,
    ) -> int:
        """Append a run record; returns the new run id."""
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO runs (started_at, deals_found, deals_new, errors) "
                "VALUES (?, ?, ?, ?)",
                (_utc_now_iso(), deals_found, deals_new, json.dumps(errors or [])),
            )
            return int(cur.lastrowid or 0)

    def recent_runs(self, limit: int = 10) -> list[dict]:
        """Return the most recent run records (newest first)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, started_at, deals_found, deals_new, errors "
                "FROM runs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]
