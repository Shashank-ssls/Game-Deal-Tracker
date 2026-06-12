"""Phase 2 tests: Epic source — happy path, malformed JSON, HTTP 500."""

from __future__ import annotations

from pathlib import Path

import responses

from src.config import Settings
from src.sources.epic import EPIC_URL, EpicSource

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@responses.activate
def test_fetch_returns_currently_free_only():
    responses.add(responses.GET, EPIC_URL, body=_fixture("epic_free.json"),
                  content_type="application/json")
    deals = EpicSource(Settings()).fetch()

    assert len(deals) == 1
    deal = deals[0]
    assert deal.title == "Free Game Now"
    assert deal.is_free is True
    assert deal.is_temporary is False
    assert deal.discount_pct == 100
    assert deal.price_old == 999.0
    assert deal.price_new == 0.0
    assert deal.currency == "INR"
    assert deal.store == "Epic Games"
    assert deal.image_url == "https://img/wide.jpg"  # wide preferred over tall
    assert deal.url == "https://store.epicgames.com/en-US/p/free-game-now"
    assert deal.ends_at is not None


@responses.activate
def test_malformed_json_returns_empty():
    responses.add(responses.GET, EPIC_URL, body="<<not json>>",
                  content_type="application/json")
    assert EpicSource(Settings()).fetch() == []


@responses.activate
def test_http_500_returns_empty():
    responses.add(responses.GET, EPIC_URL, json={"error": "boom"}, status=500)
    assert EpicSource(Settings()).fetch() == []
