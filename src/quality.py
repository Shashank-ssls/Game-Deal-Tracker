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
    """True for a pricey game that is now meaningfully cheaper.

    The thresholds (``premium_original_min`` / ``premium_sale_max``) are amounts in
    ``settings.currency`` (INR), so a deal priced in any other currency never
    qualifies — this keeps the region lock honest even if an unverified USD deal
    reaches classification.

    ``premium_mode`` selects how "cheaper" is judged (all require a pricey original,
    ``price_old > premium_original_min``):

    * ``absolute`` — sale price at/below ``premium_sale_max``
      (e.g. ₹1200 → ₹480 qualifies; ₹3999 @ 80% → ₹800 does NOT).
    * ``percent``  — discount at/above ``premium_min_discount_pct``
      (e.g. ₹3999 @ 80% qualifies; ₹1200 @ 40% does NOT).
    * ``either``   — absolute OR percent (default; catches both shapes).
    * ``both``     — absolute AND percent.

    Unknown prices make a sub-condition False rather than raising.
    """
    if deal.currency != settings.currency:
        return False
    has_pricey_original = (
        deal.price_old is not None and deal.price_old > settings.premium_original_min
    )
    absolute = (
        has_pricey_original
        and deal.price_new is not None
        and deal.price_new <= settings.premium_sale_max
    )
    percent = has_pricey_original and deal.discount_pct >= settings.premium_min_discount_pct

    mode = settings.premium_mode
    if mode == "absolute":
        return absolute
    if mode == "percent":
        return percent
    if mode == "both":
        return absolute and percent
    return absolute or percent  # "either"


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


def below_price_floor(deal: Deal, settings: Settings) -> bool:
    """True if a deal should be dropped for being too cheap originally.

    Free games are never dropped. Deals with an unknown original price are NOT
    dropped (we only filter on data we actually have, consistent with
    is_excluded / passes_gate). The threshold is in settings.currency (INR), so
    non-INR deals are left alone — the region lock handles those. Strictly
    less-than, so a deal priced exactly at the floor is kept.
    """
    if settings.min_original_price <= 0:
        return False
    if deal.is_free:
        return False
    if deal.currency != settings.currency:
        return False
    if deal.price_old is None:
        return False
    return deal.price_old < settings.min_original_price


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
