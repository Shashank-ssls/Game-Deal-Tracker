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
from collections.abc import Iterator
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
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,
    deals_found   INTEGER NOT NULL,
    deals_new     INTEGER NOT NULL,
    errors        TEXT,
    source_counts TEXT          -- JSON {itad,cheapshark,steam,epic} per-source fetched counts
);

CREATE TABLE IF NOT EXISTS reminders (
    key          TEXT PRIMARY KEY,   -- source|source_game_id
    reminded_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS title_lookup (
    title          TEXT PRIMARY KEY,   -- watchlist title -> resolved ITAD game id
    itad_game_id   TEXT,
    fetched_at     TEXT NOT NULL
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

    # Tables retired by the feed-driven redesign: deals are now identified by ITAD
    # game ids, so the local Steam app index and the SteamSpy-derived watchlist are
    # gone. Dropped on startup if a pre-existing DB still has them.
    _RETIRED_TABLES = (
        "app_index",
        "app_index_meta",
        "derived_watchlist",
        "derived_watchlist_meta",
    )

    def _migrate(self) -> None:
        """Add columns introduced after a DB was first created (idempotent)."""
        dropped = False
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
            run_cols = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
            if "source_counts" not in run_cols:
                conn.execute("ALTER TABLE runs ADD COLUMN source_counts TEXT")
            tables = {
                row["name"]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            for table in self._RETIRED_TABLES:
                if table in tables:
                    conn.execute(f"DROP TABLE {table}")
                    dropped = True
        if dropped:
            self._vacuum()  # reclaim the space the (large) app index held

    def _vacuum(self) -> None:
        """Compact the database file (must run outside a transaction)."""
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        try:
            conn.execute("VACUUM")
        finally:
            conn.close()

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

    # -- watchlist title -> ITAD game id cache -------------------------------

    def cached_title_id(self, title: str, max_age_days: int) -> tuple[bool, str | None]:
        """Return ``(is_fresh_hit, itad_game_id)`` for a watchlist title.

        A fresh hit (younger than ``max_age_days``) always carries a resolved id —
        only successful resolutions are cached — so the watchlist can skip the ITAD
        lookup. ``(False, None)`` means "never looked up or stale; resolve again".
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT itad_game_id, fetched_at FROM title_lookup WHERE title = ?", (title,)
            ).fetchone()
        if row is None or not row["itad_game_id"]:
            return False, None
        fetched = datetime.fromisoformat(row["fetched_at"])
        age_days = (datetime.now(UTC) - fetched).total_seconds() / 86400
        if age_days >= max_age_days:
            return False, None
        return True, row["itad_game_id"]

    def upsert_title_id(self, title: str, itad_game_id: str) -> None:
        """Cache a resolved watchlist title -> ITAD game id (refreshes timestamp)."""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO title_lookup (title, itad_game_id, fetched_at) VALUES (?, ?, ?) "
                "ON CONFLICT(title) DO UPDATE SET "
                "itad_game_id = excluded.itad_game_id, fetched_at = excluded.fetched_at",
                (title, itad_game_id, _utc_now_iso()),
            )

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
        source_counts: dict[str, int] | None = None,
    ) -> int:
        """Append a run record; returns the new run id."""
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO runs (started_at, deals_found, deals_new, errors, source_counts) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    _utc_now_iso(),
                    deals_found,
                    deals_new,
                    json.dumps(errors or []),
                    json.dumps(source_counts) if source_counts else None,
                ),
            )
            return int(cur.lastrowid or 0)

    def recent_runs(self, limit: int = 10) -> list[dict]:
        """Return the most recent run records (newest first)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, started_at, deals_found, deals_new, errors, source_counts "
                "FROM runs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]
