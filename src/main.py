"""Game Deal Tracker — entry point / orchestrator.

This is the only module permitted to print to stdout for CLI output. Business
logic and HTTP live in the source/enrich/notify modules added in later phases.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from src.appindex import SteamAppIndex
from src.catalogue import PublisherCatalogue
from src.config import Settings
from src.db import Database
from src.enrich.ratings import RatingsEnricher
from src.merge import merge_deals
from src.models import Deal
from src.notify.discord import DiscordNotifier
from src.quality import (
    is_excluded,
    is_premium,
    matches_franchise,
    split_quality,
    store_allowed,
)
from src.resolve import SteamAppidResolver
from src.sources.cheapshark import CheapSharkSource
from src.sources.epic import EpicSource
from src.sources.itad import ITADSource
from src.sources.steam import SteamSource
from src.watch import WatchlistChecker
from src.wishlist import WishlistMatcher, split_wishlist_hits


def _deal_key(deal: Deal) -> str:
    return f"{deal.source}|{deal.source_game_id}"


def _mark_price_trends(deals: list[Deal], db: Database, window: int) -> list[Deal]:
    """Flag deals cheaper than their lowest price over ``window`` days; record today's.

    Free games are skipped (no meaningful price). The flag never affects dedup —
    it only drives the "📉 Cheaper than before" badge.
    """
    out: list[Deal] = []
    for deal in deals:
        drop = False
        if not deal.is_free and deal.price_new is not None:
            low = db.min_price_before_today(deal.source, deal.source_game_id, window)
            if low is not None and deal.price_new < low:
                drop = True
        db.record_price(deal)
        out.append(replace(deal, is_price_drop=True) if drop else deal)
    return out


def _ending_soon(deal: Deal, hours: int) -> bool:
    """True if the deal ends within the next ``hours`` (0 disables)."""
    if hours <= 0 or deal.ends_at is None:
        return False
    ends = deal.ends_at
    if ends.tzinfo is None:
        ends = ends.replace(tzinfo=UTC)
    now = datetime.now(UTC)
    return now <= ends <= now + timedelta(hours=hours)


def check_config(settings: Settings) -> int:
    """Print which settings are set/missing (never values). Return exit code."""
    print("Game Deal Tracker - configuration check\n")
    for label, status in settings.status_report():
        print(f"  {label}: {status}")

    problems = settings.validate()
    print()
    if problems:
        print("Problems found:")
        for problem in problems:
            print(f"  - {problem}")
        print("\nFix the above (see .env.example / config.example.yaml) and re-run.")
        return 1

    print("All required configuration is present.")
    return 0


def collect_deals(
    settings: Settings, index: SteamAppIndex | None = None
) -> tuple[list[Deal], list[Deal]]:
    """Fetch every source, INR-verify USD discoveries, and merge into buckets.

    Returns ``(free_deals, discount_deals)``. Each source swallows its own errors,
    so one failing source never aborts the run.
    """
    itad = ITADSource(settings)

    found: list[Deal] = []
    found.extend(EpicSource(settings).fetch())
    found.extend(SteamSource(settings).fetch())
    found.extend(itad.fetch())

    # CheapShark is USD discovery: re-price in INR via ITAD, dropping deals that
    # fall below threshold or aren't purchasable in IN.
    found.extend(itad.verify_inr(CheapSharkSource(settings).fetch()))

    free_deals, discount_deals = merge_deals(found)
    # Optionally resolve Steam appids for non-Steam deals so they can be enriched
    # and classified (opt-in; no-op when disabled).
    discount_deals = SteamAppidResolver(settings, index=index).resolve(discount_deals)
    return free_deals, discount_deals


class _WarningCollector(logging.Handler):
    """Captures WARNING+ log records so per-run errors can be persisted."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(self.format(record))


def run_pipeline(settings: Settings, dry_run: bool) -> int:
    """Collect deals, post new ones to Discord (free -> wishlist -> discounts).

    Deals are marked seen only after the Discord message carrying them is
    accepted (handled inside the notifier), so a failed post is retried next run.
    In dry-run the notifier prints embed JSON and nothing is marked or posted.
    Any source/notifier warning during the run is recorded in the run log.
    """
    db = Database(settings.db_path)
    # Forget deals older than the configured window so the dedup table stays small.
    db.purge_expired(settings.dedup_expiry_days)

    collector = _WarningCollector()
    collector.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    src_logger = logging.getLogger("src")
    src_logger.addHandler(collector)
    try:
        return _run(settings, dry_run, db, collector)
    finally:
        src_logger.removeHandler(collector)


def _run(settings: Settings, dry_run: bool, db: Database, collector: _WarningCollector) -> int:
    # Local Steam appid index (opt-in): cached once, then shared by the resolver
    # and the watchlist so most title->appid lookups skip the online search.
    index: SteamAppIndex | None = None
    if settings.use_appid_index:
        index = SteamAppIndex(settings, db)
        index.ensure_fresh()

    free_deals, discount_deals = collect_deals(settings, index=index)

    # Wishlist matching (optional): reclassify wishlisted discounts as wishlist
    # hits, and price-check wishlist games on sale below the threshold.
    wishlist_deals: list[Deal] = []
    matcher = WishlistMatcher(settings)
    wl_appids = matcher.fetch_appids()
    if wl_appids:
        wishlist_deals, discount_deals = split_wishlist_hits(discount_deals, wl_appids)
        already = {d.steam_appid for d in (free_deals + wishlist_deals) if d.steam_appid}
        wishlist_deals += matcher.fetch_on_sale(wl_appids - already)

    # AAA watchlist: price-check specific games so they surface whenever on sale,
    # independent of the discount-sorted fetch pool. These are forced into the
    # preferred (AAA) section. Optionally augmented with publisher-derived titles.
    extra_titles: list[str] = []
    if settings.derive_watchlist:
        extra_titles = PublisherCatalogue(settings, db).derived_titles()
    watch_deals = WatchlistChecker(settings, index=index).fetch_on_sale(extra_titles)
    watch_appids = {d.steam_appid for d in watch_deals if d.steam_appid}
    if watch_appids:  # avoid listing a watched game twice
        discount_deals = [d for d in discount_deals if d.steam_appid not in watch_appids]

    # Store selection (only show deals from chosen sites) — before enrichment so we
    # don't waste Steam calls on filtered-out deals.
    if settings.stores or settings.exclude_stores:
        free_deals = [d for d in free_deals if store_allowed(d, settings)]
        discount_deals = [d for d in discount_deals if store_allowed(d, settings)]
        wishlist_deals = [d for d in wishlist_deals if store_allowed(d, settings)]
        watch_deals = [d for d in watch_deals if store_allowed(d, settings)]

    # Attach Steam review / Metacritic / publisher data (cached) before reporting.
    enriched = RatingsEnricher(settings, db).enrich(
        free_deals + wishlist_deals + discount_deals + watch_deals
    )

    # Refine the 🔥 flag to a true all-store, all-time low (opt-in; ITAD-native deals).
    enriched = ITADSource(settings).flag_all_store_lows(enriched)

    # Content filters (Early Access / unreleased / excluded or included genres).
    # Franchise matches always win — a watched franchise title is never filtered out.
    if settings.exclude_early_access or settings.exclude_genres or settings.include_genres:
        enriched = [
            d for d in enriched
            if matches_franchise(d, settings.franchises) or not is_excluded(d, settings)
        ]

    watch_e = [d for d in enriched if d.source == "watchlist"]
    free_deals = [d for d in enriched if d.is_free]
    wishlist_deals = [d for d in enriched if d.is_wishlist and not d.is_free]
    discount_deals = [
        d for d in enriched
        if d.source != "watchlist" and not d.is_free and not d.is_wishlist
    ]

    # Pull preferred-publisher games into their own section; gate the rest.
    preferred_deals, discount_deals = split_quality(discount_deals, settings)
    # Merge watchlist hits into the AAA section (premium-flagged, deduped by appid).
    watch_e = [replace(d, is_premium_deal=is_premium(d, settings)) for d in watch_e]
    pref_appids = {d.steam_appid for d in preferred_deals if d.steam_appid}
    watch_e = [d for d in watch_e if d.steam_appid not in pref_appids]
    preferred_deals = watch_e + preferred_deals

    # Section toggles + optional cap on the (sorted) discount list.
    if "free" not in settings.sections:
        free_deals = []
    if "wishlist" not in settings.sections:
        wishlist_deals = []
    if "preferred" not in settings.sections:
        preferred_deals = []
    if "discounts" not in settings.sections:
        discount_deals = []
    elif settings.max_discounts_per_run > 0:
        discount_deals = discount_deals[: settings.max_discounts_per_run]

    # Price-trend: flag deals cheaper than their recent low, and record today's
    # prices so the trend window keeps filling (opt-in; off when window is 0).
    if settings.price_drop_window_days > 0:
        db.purge_old_prices(max(settings.price_drop_window_days, 30))
        window = settings.price_drop_window_days
        free_deals = _mark_price_trends(free_deals, db, window)
        wishlist_deals = _mark_price_trends(wishlist_deals, db, window)
        preferred_deals = _mark_price_trends(preferred_deals, db, window)
        discount_deals = _mark_price_trends(discount_deals, db, window)

    found_total = (
        len(free_deals) + len(wishlist_deals) + len(preferred_deals) + len(discount_deals)
    )

    # A deal is reported if it is new OR within its ending-soon window (once).
    def to_notify(deals: list[Deal]) -> list[Deal]:
        out = []
        for d in deals:
            soon = _ending_soon(d, settings.ending_soon_hours) and not db.was_reminded(_deal_key(d))
            if db.is_new(d) or soon:
                out.append(d)
        return out

    def on_posted(deal: Deal) -> None:
        db.mark_seen(deal)
        if _ending_soon(deal, settings.ending_soon_hours):
            db.mark_reminded(_deal_key(deal))

    new_free = to_notify(free_deals)
    new_wishlist = to_notify(wishlist_deals)
    new_preferred = to_notify(preferred_deals)
    new_discounts = to_notify(discount_deals)
    new_total = len(new_free) + len(new_wishlist) + len(new_preferred) + len(new_discounts)

    if new_total == 0:
        print("No new deals found.")
    else:
        notifier = DiscordNotifier(settings)
        handled = notifier.post(
            new_free,
            new_wishlist,
            new_discounts,
            preferred=new_preferred,
            mark_seen=on_posted,
            dry_run=dry_run,
        )
        action = "rendered" if dry_run else "posted"
        print(
            f"{action} {handled}/{new_total} new deal(s) "
            f"({len(new_free)} free, {len(new_preferred)} quality, "
            f"{len(new_wishlist)} wishlist, {len(new_discounts)} >= {settings.min_discount_pct}%)."
        )

    db.log_run(deals_found=found_total, deals_new=new_total, errors=collector.messages or None)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.main",
        description="Track free games and 70%+ discounts; post them to Discord.",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Validate setup and report set/missing settings (prints no secret values).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the pipeline and print embed JSON instead of posting to Discord.",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Print a summary of recent runs (found/new/errors) and exit.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Increase log verbosity (-v for INFO, -vv for DEBUG).",
    )
    return parser


def print_stats(settings: Settings, limit: int = 10) -> int:
    """Print the last ``limit`` runs from the run log."""
    runs = Database(settings.db_path).recent_runs(limit)
    if not runs:
        print("No runs recorded yet.")
        return 0
    print(f"Last {len(runs)} run(s):\n")
    print(f"  {'started (UTC)':<28}{'found':>7}{'new':>6}  errors")
    for run in runs:
        errors = json.loads(run["errors"] or "[]")
        started = run["started_at"][:19].replace("T", " ")
        print(f"  {started:<28}{run['deals_found']:>7}{run['deals_new']:>6}  {len(errors)}")
    return 0


def _configure_logging(verbosity: int) -> None:
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )


def _force_utf8_output() -> None:
    """Ensure stdout/stderr can emit ₹, →, ≥ etc. regardless of console codepage."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_output()
    args = build_parser().parse_args(argv)
    _configure_logging(args.verbose)
    settings = Settings.load()

    if args.check_config:
        return check_config(settings)

    if args.stats:
        return print_stats(settings)

    return run_pipeline(settings, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
