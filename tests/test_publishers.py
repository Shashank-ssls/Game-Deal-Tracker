"""Stage 3 tests: live-feed publisher attachment + preferred classification."""

from __future__ import annotations

import responses

from src.config import Settings
from src.db import Database
from src.enrich.publishers import ITAD_INFO_URL, PublisherTagger
from src.models import Deal
from src.quality import split_quality


def _db(tmp_path) -> Database:
    return Database(tmp_path / "t.db")


def _gog_deal(title: str, *, gid: str, appid: int | None = None, discount: int = 34) -> Deal:
    return Deal(
        title=title, store="GOG", url=f"https://gog.com/{title}", source="itad",
        source_game_id=gid, itad_game_id=gid, steam_appid=appid,
        price_old=1000.0, price_new=660.0, currency="INR", discount_pct=discount,
    )


def _no_sleep(_seconds: float) -> None:
    return None


@responses.activate
def test_attaches_publisher_via_itad_then_classifies_preferred(tmp_path):
    # A Capcom game at 34% off from the enlarged GOG feed (no appid, no publisher).
    responses.add(responses.GET, ITAD_INFO_URL, json={
        "id": "g-capcom", "title": "Some Capcom Game",
        "publishers": [{"id": 1, "name": "CAPCOM Co., Ltd."}],
        "developers": [{"id": 2, "name": "CAPCOM"}],
    })
    settings = Settings(
        itad_api_key="k", preferred_publishers=["Capcom"],
        min_discount_pct=50, discovery_discount_pct=1,
    )
    deal = _gog_deal("Some Capcom Game", gid="g-capcom")
    tagged = PublisherTagger(settings, _db(tmp_path)).tag([deal])

    assert tagged[0].publisher == "CAPCOM Co., Ltd."
    # split_quality (gate-exempt preferred rule) pulls it into the preferred section
    # even at 34% off — the case that was impossible before.
    preferred, remaining = split_quality(tagged, settings)
    assert [d.title for d in preferred] == ["Some Capcom Game"]
    assert remaining == []


@responses.activate
def test_cache_hit_makes_zero_info_calls(tmp_path):
    db = _db(tmp_path)
    db.upsert_game(555, publisher="Square Enix", developer="Square Enix")
    settings = Settings(itad_api_key="k", preferred_publishers=["Square Enix"])
    deal = _gog_deal("Cached Game", gid="g-se", appid=555)
    tagged = PublisherTagger(settings, db).tag([deal])

    assert tagged[0].publisher == "Square Enix"
    assert len(responses.calls) == 0  # served entirely from the games cache


@responses.activate
def test_info_lookups_are_capped_per_run(tmp_path):
    responses.add(responses.GET, ITAD_INFO_URL, json={
        "publishers": [{"name": "Indie Co"}], "developers": [{"name": "Indie Co"}],
    })
    settings = Settings(
        itad_api_key="k", preferred_publishers=["Capcom"], publisher_metadata_batch=100,
    )
    deals = [_gog_deal(f"G{i}", gid=f"g{i}") for i in range(250)]
    PublisherTagger(settings, _db(tmp_path), sleeper=_no_sleep).tag(deals)
    # Bounded by publisher_metadata_batch (ITAD has no batch info endpoint).
    assert len(responses.calls) == 100


def test_no_op_without_preferred_publishers(tmp_path):
    settings = Settings(itad_api_key="k", preferred_publishers=[])
    deals = [_gog_deal("X", gid="gx")]
    # No HTTP and the deals come back unchanged.
    assert PublisherTagger(settings, _db(tmp_path)).tag(deals) == deals


@responses.activate
def test_word_boundary_prevents_false_publisher_match(tmp_path):
    # "EA" must not match "Creative Assembly" (substring) once attached.
    responses.add(responses.GET, ITAD_INFO_URL, json={
        "publishers": [{"name": "SEGA"}], "developers": [{"name": "Creative Assembly"}],
    })
    settings = Settings(itad_api_key="k", preferred_publishers=["EA"], discovery_discount_pct=1,
                        min_discount_pct=50)
    deal = _gog_deal("Total War", gid="g-tw")
    tagged = PublisherTagger(settings, _db(tmp_path)).tag([deal])
    assert tagged[0].developer == "Creative Assembly"
    preferred, _ = split_quality(tagged, settings)
    assert preferred == []  # EA does not word-boundary-match "Creative Assembly"
