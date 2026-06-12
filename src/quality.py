"""Quality classification: preferred-publisher highlighting + a review/Metacritic gate.

Pure functions, no I/O. Used after enrichment to (a) pull deals from preferred
studios into their own section, and (b) drop low-quality games from the regular
discount bucket. Free, wishlist, and preferred deals are never gated.
"""

from __future__ import annotations

import re
from dataclasses import replace

from src.config import Settings
from src.models import Deal


def name_matches(haystack: str, names: list[str]) -> bool:
    """True if any of ``names`` appears in ``haystack`` on a word boundary.

    Case-insensitive, so "Capcom" matches "CAPCOM Co., Ltd." but short names like
    "EA" don't misfire on "crEAtive Assembly".
    """
    text = haystack.lower()
    for name in names:
        token = name.strip().lower()
        if token and re.search(rf"\b{re.escape(token)}\b", text):
            return True
    return False


def is_preferred(deal: Deal, preferred_publishers: list[str]) -> bool:
    """True if any allowlist name appears in the deal's publisher OR developer."""
    if not preferred_publishers:
        return False
    return name_matches(f"{deal.publisher or ''}\n{deal.developer or ''}", preferred_publishers)


def matches_franchise(deal: Deal, franchises: list[str]) -> bool:
    """True if any configured franchise name is a substring of the deal title.

    Case-insensitive substring (not word-boundary) so "Resident Evil" matches
    "Resident Evil 4" and its editions. Used to promote already-discovered titles
    into the AAA / quality-picks section regardless of publisher.
    """
    if not franchises:
        return False
    title = (deal.title or "").lower()
    return any(name.strip().lower() in title for name in franchises if name.strip())


def is_premium(deal: Deal, settings: Settings) -> bool:
    """True for a pricey game now cheap: original > min AND sale <= max."""
    if deal.price_old is None or deal.price_new is None:
        return False
    return (
        deal.price_old > settings.premium_original_min
        and deal.price_new <= settings.premium_sale_max
    )


def passes_gate(deal: Deal, settings: Settings) -> bool:
    """Apply the quality gate. Unknown values pass (don't punish non-Steam games)."""
    if settings.min_review_pct > 0 and deal.review_pct is not None:
        if deal.review_pct < settings.min_review_pct:
            return False
        # A high % only counts if it's backed by enough reviews.
        if settings.min_review_count > 0 and (deal.review_count or 0) < settings.min_review_count:
            return False
    if settings.min_metacritic > 0 and deal.metacritic is not None:
        if deal.metacritic < settings.min_metacritic:
            return False
    return True


def store_allowed(deal: Deal, settings: Settings) -> bool:
    """True if the deal's store passes the store allow/block lists.

    Matching is case-insensitive substring, so "Epic" matches both "Epic Games"
    and "Epic Game Store". An empty allowlist means every store is allowed.
    """
    store = (deal.store or "").lower()
    if settings.exclude_stores:
        if any(s.strip().lower() in store for s in settings.exclude_stores if s.strip()):
            return False
    if settings.stores:
        return any(s.strip().lower() in store for s in settings.stores if s.strip())
    return True


def is_excluded(deal: Deal, settings: Settings) -> bool:
    """True if a deal should be dropped by content filters (EA / genres).

    Filters only act on data we actually have; a deal with unknown genres/release
    state is not excluded.
    """
    if settings.exclude_early_access:
        if deal.coming_soon:
            return True
        if deal.genres and "early access" in deal.genres.lower():
            return True
    if settings.exclude_genres and deal.genres:
        owned = {g.strip().lower() for g in deal.genres.split(",")}
        if any(g.strip().lower() in owned for g in settings.exclude_genres if g.strip()):
            return True
    # Positive genre filter: keep only deals matching at least one include genre.
    # A deal with unknown genres is left alone (same as the exclude side).
    if settings.include_genres and deal.genres:
        owned = {g.strip().lower() for g in deal.genres.split(",")}
        if not any(g.strip().lower() in owned for g in settings.include_genres if g.strip()):
            return True
    return False


def split_quality(
    discounts: list[Deal], settings: Settings
) -> tuple[list[Deal], list[Deal]]:
    """Split discount deals into ``(preferred, gated_discounts)``.

    Preferred-publisher deals and franchise matches are flagged ``is_preferred`` and
    exempt from the gate; the rest must pass :func:`passes_gate` to remain.
    """
    preferred: list[Deal] = []
    remaining: list[Deal] = []
    for deal in discounts:
        tagged = replace(deal, is_premium_deal=is_premium(deal, settings))
        if is_preferred(tagged, settings.preferred_publishers) or matches_franchise(
            tagged, settings.franchises
        ):
            preferred.append(replace(tagged, is_preferred=True))
            continue
        # Wide-net keep rule: a sub-threshold deal only stays if it's a premium
        # pick (preferred handled above; wishlist is a separate bucket upstream).
        if tagged.discount_pct < settings.min_discount_pct and not tagged.is_premium_deal:
            continue
        if passes_gate(tagged, settings):
            remaining.append(tagged)

    # Premium picks float to the top of each section, then by discount, then rating.
    sort_key = lambda d: (d.is_premium_deal, d.discount_pct, d.review_pct or 0)  # noqa: E731
    preferred.sort(key=sort_key, reverse=True)
    remaining.sort(key=sort_key, reverse=True)
    return preferred, remaining
