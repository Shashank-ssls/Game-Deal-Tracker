"""Cross-store merge, dedupe, and ordering buckets.

Combines deals from every source into two ordered lists:
  * ``free_deals``     — keepable freebies preferred over temporary ones.
  * ``discount_deals`` — one entry per game (cheapest INR offer wins), sorted by
    discount descending, then by review score descending.

A game that is free in one place but paid in another is reported only as free.
No networking happens here.
"""

from __future__ import annotations

import re

from src.models import Deal

_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# Titles that are clearly not single games (course/cert/software bundles from
# stores like Fanatical). ITAD sometimes tags these as type "game", so we also
# screen by title here as a catch-all across every source.
_NON_GAME_TITLE = re.compile(
    r"\b(bundle|e-?learning|certification|course|software|ebook|audiobook)\b",
    re.IGNORECASE,
)


def is_non_game(deal: Deal) -> bool:
    """True when a deal's title looks like a non-game bundle/course."""
    return bool(_NON_GAME_TITLE.search(deal.title or ""))


def _game_key(deal: Deal) -> tuple[str, str]:
    """Stable identity for the same game across stores."""
    if deal.steam_appid is not None:
        return ("appid", str(deal.steam_appid))
    return ("title", _NON_ALNUM.sub("", deal.title.lower()))


def _price_for_sort(deal: Deal) -> float:
    return deal.price_new if deal.price_new is not None else float("inf")


def merge_deals(deals: list[Deal]) -> tuple[list[Deal], list[Deal]]:
    """Return ``(free_deals, discount_deals)`` deduped and ordered."""
    free_by_key: dict[tuple[str, str], Deal] = {}
    paid_by_key: dict[tuple[str, str], Deal] = {}

    for deal in deals:
        if is_non_game(deal):
            continue  # drop course/cert/software bundles outright
        key = _game_key(deal)
        if deal.is_free:
            current = free_by_key.get(key)
            # Prefer a keepable freebie over a temporary one.
            if current is None or (current.is_temporary and not deal.is_temporary):
                free_by_key[key] = deal
        else:
            current = paid_by_key.get(key)
            if current is None or _price_for_sort(deal) < _price_for_sort(current):
                paid_by_key[key] = deal

    # A game that is free anywhere is never also listed as a paid discount.
    free_keys = set(free_by_key)
    discount_deals = [d for k, d in paid_by_key.items() if k not in free_keys]

    discount_deals.sort(
        key=lambda d: (d.discount_pct, d.review_pct or 0),
        reverse=True,
    )
    free_deals = list(free_by_key.values())
    return free_deals, discount_deals
