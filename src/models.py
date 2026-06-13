"""Shared data models.

``Deal`` is the single shape every source returns (see CLAUDE.md) — no module may
invent a parallel structure. It is frozen so deals are hashable and safe to pass
around without accidental mutation.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Deal:
    """A single game offer from one source.

    Required fields identify the offer; the rest carry pricing, freebie flags,
    and optional rating enrichment (filled by later phases, ``None`` until then).
    """

    # Identity (required).
    title: str
    store: str
    url: str
    source: str           # which DealSource produced this (e.g. "epic", "steam")
    source_game_id: str   # stable id within that source

    # Presentation / pricing.
    image_url: str | None = None
    price_old: float | None = None
    price_new: float | None = None
    currency: str = "INR"
    discount_pct: int = 0
    # True for USD-priced discovery deals (CheapShark) that must be re-priced in
    # INR before they may be reported. Cleared once verified.
    needs_inr_verify: bool = False

    # Freebie flags.
    is_free: bool = False
    is_temporary: bool = False   # e.g. free weekend (not keepable)
    is_wishlist: bool = False    # game is on the user's Steam wishlist
    is_lowest_ever: bool = False # price is at/below the historical low (store, or all-store)
    is_premium_deal: bool = False  # pricey game (orig > min) now cheap (sale <= max)
    is_price_drop: bool = False  # cheaper than its lowest price seen in the trend window
    ends_at: datetime | None = None

    # Steam linkage (enables ratings + wishlist matching).
    steam_appid: int | None = None

    # Cross-source identity: the ITAD game UUID once resolved. Enables multi-store
    # pricing and bulk publisher attachment. ITAD-native deals carry it directly;
    # USD discovery deals gain it during INR verification.
    itad_game_id: str | None = None

    # Studio / catalogue info (enables preferred matching + content filters).
    publisher: str | None = None
    developer: str | None = None
    genres: str | None = None        # comma-joined Steam genres
    coming_soon: bool = False        # not yet released
    is_preferred: bool = False   # matches a configured preferred publisher/developer

    # Optional rating enrichment (Phase 4).
    review_summary: str | None = None
    review_pct: int | None = None
    review_count: int | None = None
    metacritic: int | None = None

    def dedup_hash(self) -> str:
        """Stable identity for dedup: sha256 of source|game_id|price_new|discount.

        A change in price or discount yields a new hash, which is how the DB
        layer detects a meaningfully different offer for the same game.
        """
        raw = f"{self.source}|{self.source_game_id}|{self.price_new}|{self.discount_pct}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
