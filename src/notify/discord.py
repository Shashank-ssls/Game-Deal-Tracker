"""Discord webhook embed builder + poster.

Builds one rich embed per Deal and posts them in the fixed order
free -> wishlist -> discounts, chunked to Discord's 10-embeds-per-message limit.

Critical invariant (ARCHITECTURE.md): a deal is marked as seen **only after**
Discord accepts the message containing it, so a failed post is retried next run
instead of being silently lost. This module decides nothing about *what* is new;
it only posts and reports success via the ``mark_seen`` callback.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterable
from datetime import date

from src.config import Settings
from src.models import Deal
from src.sources.base import DEFAULT_TIMEOUT, build_session

logger = logging.getLogger(__name__)

COLOR_FREE = 0xF1C40F       # gold
COLOR_WISHLIST = 0x9B59B6   # purple
COLOR_PREFERRED = 0x1ABC9C  # teal (preferred publishers)
COLOR_DISCOUNT = 0x2ECC71   # green

MAX_EMBEDS_PER_MESSAGE = 10
_MAX_POST_ATTEMPTS = 5
_DEFAULT_RETRY_AFTER = 1.0


def _chunks(items: list, size: int) -> Iterable[list]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


class DiscordNotifier:
    def __init__(
        self,
        settings: Settings,
        session=None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self.webhook_url = settings.discord_webhook_url
        self.session = session if session is not None else build_session()
        self._sleep = sleeper

    # -- public API ----------------------------------------------------------

    def post(
        self,
        free: list[Deal],
        wishlist: list[Deal],
        discounts: list[Deal],
        *,
        preferred: list[Deal] | None = None,
        mark_seen: Callable[[Deal], None],
        dry_run: bool = False,
    ) -> int:
        """Post deals in order; return the number of embeds successfully handled.

        Order: free -> preferred publishers (AAA) -> wishlist -> discounts.
        ``mark_seen`` is invoked per deal only after the message carrying it is
        accepted (and never in dry-run). Each item carries its section's mention.
        """
        preferred = preferred or []
        # Order: free -> preferred (AAA) -> wishlist -> discounts.
        # (deal, color, section_mention)
        tagged: list[tuple[Deal, int, str]] = []
        tagged += [(d, COLOR_FREE, "") for d in free]
        tagged += [(d, COLOR_PREFERRED, self.settings.preferred_mention) for d in preferred]
        tagged += [(d, COLOR_WISHLIST, self.settings.wishlist_mention) for d in wishlist]
        tagged += [(d, COLOR_DISCOUNT, "") for d in discounts]

        if not tagged:
            return 0  # nothing new -> stay silent

        if not dry_run and not self.webhook_url:
            logger.warning("discord: no webhook configured; nothing posted")
            return 0

        summary = self._summary(len(free), len(wishlist), len(preferred), len(discounts))
        handled = 0

        for index, chunk in enumerate(_chunks(tagged, MAX_EMBEDS_PER_MESSAGE)):
            payload: dict = {"embeds": [self.build_embed(d, color) for d, color, _ in chunk]}
            mentions = [m for _, _, m in chunk if m]
            content = self._content_line(
                summary=summary if index == 0 else None,
                mentions=mentions,
            )
            if content:
                payload["content"] = content

            if dry_run:
                print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
                handled += len(chunk)
                continue

            if self._post(payload):
                for deal, _, _ in chunk:
                    mark_seen(deal)
                handled += len(chunk)
            else:
                logger.warning("discord: chunk %d failed; will retry next run", index)

        return handled

    # -- embed construction --------------------------------------------------

    def build_embed(self, deal: Deal, color: int) -> dict:
        embed: dict = {
            "title": deal.title,
            "color": color,
            "description": self._price_line(deal),
            "fields": self._fields(deal),
            "footer": {"text": f"Game Deal Tracker · run {date.today().isoformat()}"},
        }
        if deal.url:
            embed["url"] = deal.url
        if deal.image_url:
            embed["image"] = {"url": deal.image_url}
        return embed

    def _price_line(self, deal: Deal) -> str:
        old = self._money(deal.price_old, deal.currency)
        if deal.is_free or deal.price_new == 0:
            tag = "Free weekend — temporary" if deal.is_temporary else "Free to keep"
            return f"🎁 **{tag}**" + (f"  ·  was ~~{old}~~" if old else "")
        new = self._money(deal.price_new, deal.currency)
        extras = []
        if deal.is_premium_deal:
            extras.append("💎 Premium pick")
        if deal.is_lowest_ever:
            extras.append("🔥 Lowest ever")
        if deal.is_price_drop:
            extras.append("📉 Cheaper than before")
        suffix = ("  ·  " + "  ·  ".join(extras)) if extras else ""
        if old and new:
            return f"~~{old}~~ → **{new}**  ·  **-{deal.discount_pct}%**{suffix}"
        if new:
            return f"**{new}**  ·  **-{deal.discount_pct}%**{suffix}"
        return f"**-{deal.discount_pct}%**{suffix}"

    @staticmethod
    def _money(amount: float | None, currency: str) -> str | None:
        if amount is None:
            return None
        if currency == "INR":
            return f"₹{amount:,.0f}"
        return f"{currency} {amount:,.2f}"

    @staticmethod
    def _fields(deal: Deal) -> list[dict]:
        """Inline fields, kept short so Discord lays them out in a clean grid."""
        fields = [{"name": "Store", "value": deal.store or "Unknown", "inline": True}]
        if deal.publisher:
            # Show only the first studio to keep columns aligned.
            pub = deal.publisher.split(",")[0].strip()
            name = "⭐ Publisher" if deal.is_preferred else "Publisher"
            fields.append({"name": name, "value": pub, "inline": True})
        if deal.review_summary and deal.review_pct is not None:
            fields.append(
                {"name": "Rating", "value": f"{deal.review_summary} · {deal.review_pct}%",
                 "inline": True}
            )
        if deal.metacritic:
            fields.append({"name": "Metacritic", "value": str(deal.metacritic), "inline": True})
        if deal.ends_at:
            fields.append(
                {"name": "Ends", "value": deal.ends_at.date().isoformat(), "inline": True}
            )
        return fields

    @staticmethod
    def _content_line(*, summary: str | None, mentions: list[str]) -> str | None:
        parts: list[str] = []
        seen: set[str] = set()
        for mention in mentions:  # de-duplicate while preserving order
            if mention not in seen:
                seen.add(mention)
                parts.append(mention)
        if summary:
            parts.append(summary)
        return " ".join(parts) if parts else None

    @staticmethod
    def _summary(free: int, wishlist: int, preferred: int, discounts: int) -> str:
        def plural(count: int, noun: str) -> str:
            return f"{count} {noun}" + ("" if count == 1 else "s")

        return (
            f"{plural(free, 'free game')}, "
            f"{plural(preferred, 'quality pick')}, "
            f"{plural(wishlist, 'wishlist deal')}, "
            f"{plural(discounts, 'deal')}"
        )

    # -- HTTP with rate-limit handling ---------------------------------------

    def _post(self, payload: dict) -> bool:
        for _ in range(_MAX_POST_ATTEMPTS):
            try:
                response = self.session.post(
                    self.webhook_url, json=payload, timeout=DEFAULT_TIMEOUT
                )
            except Exception as exc:  # noqa: BLE001 - a Discord failure must not crash the run
                logger.warning("discord: post error (%s)", type(exc).__name__)
                return False

            if response.status_code == 429:
                wait = self._retry_after(response)
                logger.info("discord: rate limited, waiting %.2fs", wait)
                self._sleep(wait)
                continue
            if 200 <= response.status_code < 300:
                return True
            logger.warning("discord: post failed (status %d)", response.status_code)
            return False

        logger.warning("discord: gave up after %d rate-limit retries", _MAX_POST_ATTEMPTS)
        return False

    @staticmethod
    def _retry_after(response) -> float:
        try:
            value = response.json().get("retry_after")
            if value is not None:
                return float(value)
        except Exception:  # noqa: BLE001
            pass
        header = response.headers.get("Retry-After")
        try:
            return float(header) if header is not None else _DEFAULT_RETRY_AFTER
        except (TypeError, ValueError):
            return _DEFAULT_RETRY_AFTER
