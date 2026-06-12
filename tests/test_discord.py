"""Phase 5 tests: chunking, 429 retry, section ordering, mark_seen-after-success."""

from __future__ import annotations

import json
from dataclasses import replace

import responses

from src.config import Settings
from src.models import Deal
from src.notify.discord import (
    COLOR_DISCOUNT,
    COLOR_FREE,
    COLOR_PREFERRED,
    COLOR_WISHLIST,
    DiscordNotifier,
)

WEBHOOK = "https://discord.test/api/webhooks/1/token"


def _settings(**kw) -> Settings:
    return Settings(discord_webhook_url=WEBHOOK, **kw)


def _noop_sleep(_seconds: float) -> None:
    return None


def _notifier(settings=None) -> DiscordNotifier:
    return DiscordNotifier(settings or _settings(), sleeper=_noop_sleep)


def _free(title) -> Deal:
    return Deal(title=title, store="Epic Games", url=f"https://e/{title}",
                source="epic", source_game_id=title, is_free=True, discount_pct=100,
                price_new=0.0)


def _discount(title, *, appid=None) -> Deal:
    return Deal(title=title, store="Steam", url=f"https://s/{title}",
                source="steam", source_game_id=title, price_old=1000.0, price_new=200.0,
                currency="INR", discount_pct=80, steam_appid=appid)


def _bodies(calls):
    return [json.loads(c.request.body) for c in calls]


@responses.activate
def test_chunks_at_ten_embeds_per_message():
    responses.add(responses.POST, WEBHOOK, status=204)
    deals = [_discount(f"Game{i}") for i in range(25)]
    seen: list[Deal] = []

    handled = _notifier().post([], [], deals, mark_seen=seen.append)

    assert handled == 25
    assert len(responses.calls) == 3  # 10 + 10 + 5
    sizes = [len(b["embeds"]) for b in _bodies(responses.calls)]
    assert sizes == [10, 10, 5]
    assert len(seen) == 25


@responses.activate
def test_429_then_success_retries():
    responses.add(responses.POST, WEBHOOK, json={"retry_after": 0.2}, status=429)
    responses.add(responses.POST, WEBHOOK, status=204)
    seen: list[Deal] = []

    handled = _notifier().post([_free("F")], [], [], mark_seen=seen.append)

    assert handled == 1
    assert len(responses.calls) == 2  # rate-limited once, then succeeded
    assert [d.title for d in seen] == ["F"]


@responses.activate
def test_section_ordering_and_colors():
    responses.add(responses.POST, WEBHOOK, status=204)
    free = [_free("FreeGame")]
    wishlist = [_discount("WishGame", appid=42)]
    discounts = [_discount("DealGame")]

    _notifier(_settings(wishlist_mention="@everyone")).post(
        free, wishlist, discounts, mark_seen=lambda d: None
    )

    body = _bodies(responses.calls)[0]
    titles = [e["title"] for e in body["embeds"]]
    colors = [e["color"] for e in body["embeds"]]
    assert titles == ["FreeGame", "WishGame", "DealGame"]
    assert colors == [COLOR_FREE, COLOR_WISHLIST, COLOR_DISCOUNT]
    # Mention present (wishlist in chunk) and the run summary on the first message.
    assert "@everyone" in body["content"]
    assert "1 wishlist deal" in body["content"]


def test_lowest_ever_renders_in_description():
    deal = replace(_discount("LowGame", appid=1), is_lowest_ever=True)
    embed = _notifier().build_embed(deal, COLOR_DISCOUNT)
    assert "Lowest ever" in embed["description"]


def test_free_price_line_uses_gift_label():
    deal = _free("Freebie")
    embed = _notifier().build_embed(deal, COLOR_FREE)
    assert "Free to keep" in embed["description"]
    assert "-100%" not in embed["description"]


def test_premium_pick_badge_in_description():
    deal = replace(_discount("AAA Steal", appid=3), price_old=2999.0, price_new=499.0,
                   is_premium_deal=True)
    embed = _notifier().build_embed(deal, COLOR_DISCOUNT)
    assert "Premium pick" in embed["description"]


def test_publisher_truncated_to_first_studio():
    deal = replace(_discount("X", appid=2), publisher="Oriol Cosp Games, Hawthorn Games")
    embed = _notifier().build_embed(deal, COLOR_DISCOUNT)
    pub = next(f for f in embed["fields"] if "Publisher" in f["name"])
    assert pub["value"] == "Oriol Cosp Games"


@responses.activate
def test_preferred_section_color_mention_and_publisher():
    responses.add(responses.POST, WEBHOOK, status=204)
    pref = replace(_discount("Capcom Game", appid=5), publisher="Capcom", is_preferred=True)

    _notifier(_settings(preferred_mention="<@&999>")).post(
        [], [], [], preferred=[pref], mark_seen=lambda d: None
    )

    body = _bodies(responses.calls)[0]
    embed = body["embeds"][0]
    assert embed["color"] == COLOR_PREFERRED
    assert "<@&999>" in body["content"]
    assert "1 quality pick" in body["content"]
    pub_field = next(f for f in embed["fields"] if "Publisher" in f["name"])
    assert pub_field["value"] == "Capcom"
    assert pub_field["name"].startswith("⭐")


@responses.activate
def test_section_order_with_all_four_buckets():
    responses.add(responses.POST, WEBHOOK, status=204)
    notifier = _notifier()
    notifier.post(
        [_free("F")], [_discount("W", appid=1)], [_discount("D")],
        preferred=[replace(_discount("P", appid=2), is_preferred=True)],
        mark_seen=lambda d: None,
    )
    titles = [e["title"] for e in _bodies(responses.calls)[0]["embeds"]]
    assert titles == ["F", "P", "W", "D"]  # free -> preferred (AAA) -> wishlist -> discount


@responses.activate
def test_mark_seen_only_after_successful_post():
    responses.add(responses.POST, WEBHOOK, status=500)
    seen: list[Deal] = []

    handled = _notifier().post([_free("F")], [], [], mark_seen=seen.append)

    assert handled == 0
    assert seen == []  # failed post -> not marked, retried next run


@responses.activate
def test_nothing_new_posts_nothing():
    handled = _notifier().post([], [], [], mark_seen=lambda d: None)
    assert handled == 0
    assert len(responses.calls) == 0


@responses.activate
def test_dry_run_prints_json_and_does_not_post(capsys):
    seen: list[Deal] = []
    handled = _notifier().post([_free("F")], [], [_discount("D")],
                               mark_seen=seen.append, dry_run=True)

    assert handled == 2
    assert len(responses.calls) == 0  # nothing posted
    assert seen == []                 # nothing marked in dry-run
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["embeds"][0]["title"] == "F"


def test_no_webhook_posts_nothing():
    notifier = DiscordNotifier(Settings(discord_webhook_url=None), sleeper=_noop_sleep)
    handled = notifier.post([_free("F")], [], [], mark_seen=lambda d: None)
    assert handled == 0
