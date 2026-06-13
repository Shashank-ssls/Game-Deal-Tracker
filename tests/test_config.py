"""Phase 0 tests: config defaults, missing-secret detection, no-secret-leak."""

from __future__ import annotations

import io
from contextlib import redirect_stdout

from src import main as main_module
from src.config import Settings


# Paths that intentionally do not exist, so load() uses defaults / env only.
def _missing(tmp_path):
    return tmp_path / "no.env", tmp_path / "no.yaml"


def test_defaults_without_yaml(tmp_path):
    env, cfg = _missing(tmp_path)
    s = Settings.load(env_path=env, config_path=cfg)
    assert s.region == "IN"
    assert s.currency == "INR"
    assert s.min_discount_pct == 70
    assert s.dedup_expiry_days == 30
    assert s.ratings_cache_days == 7
    assert s.wishlist_mention == ""
    assert s.max_deals_per_run == 30


def test_quality_defaults(tmp_path):
    env, cfg = _missing(tmp_path)
    s = Settings.load(env_path=env, config_path=cfg)
    assert s.preferred_publishers == []
    assert s.min_review_pct == 0
    assert s.min_metacritic == 0
    assert s.preferred_mention == ""


def test_quality_yaml_overrides(tmp_path):
    env, _ = _missing(tmp_path)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "preferred_publishers:\n  - Capcom\n  - SEGA\nmin_review_pct: 80\n", encoding="utf-8"
    )
    s = Settings.load(env_path=env, config_path=cfg)
    assert s.preferred_publishers == ["Capcom", "SEGA"]
    assert s.min_review_pct == 80


def test_min_review_pct_out_of_range_flagged():
    assert any("min_review_pct" in p for p in Settings(min_review_pct=150).validate())


def test_watchlist_default_and_override(tmp_path):
    env, _ = _missing(tmp_path)
    assert Settings.load(env_path=env, config_path=tmp_path / "no.yaml").watchlist == []
    cfg = tmp_path / "config.yaml"
    cfg.write_text("watchlist:\n  - ELDEN RING\n  - Cyberpunk 2077\n", encoding="utf-8")
    s = Settings.load(env_path=env, config_path=cfg)
    assert s.watchlist == ["ELDEN RING", "Cyberpunk 2077"]


def test_signal_coverage_defaults(tmp_path):
    env, cfg = _missing(tmp_path)
    s = Settings.load(env_path=env, config_path=cfg)
    assert s.use_appid_index is False
    assert s.appindex_cache_days == 7
    assert s.include_genres == []
    assert s.franchises == []


def test_signal_coverage_yaml_overrides(tmp_path):
    env, _ = _missing(tmp_path)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "use_appid_index: true\nappindex_cache_days: 3\n"
        "include_genres:\n  - RPG\nfranchises:\n  - Resident Evil\n",
        encoding="utf-8",
    )
    s = Settings.load(env_path=env, config_path=cfg)
    assert s.use_appid_index is True
    assert s.appindex_cache_days == 3
    assert s.include_genres == ["RPG"]
    assert s.franchises == ["Resident Evil"]


def test_appindex_cache_days_must_be_positive():
    assert any("appindex_cache_days" in p for p in Settings(appindex_cache_days=0).validate())


def test_catalogue_and_pricetrend_defaults(tmp_path):
    env, cfg = _missing(tmp_path)
    s = Settings.load(env_path=env, config_path=cfg)
    assert s.derive_watchlist is False
    assert s.catalogue_cache_days == 7
    assert s.max_derived_titles == 40
    assert s.all_store_low is False
    assert s.price_drop_window_days == 0


def test_catalogue_and_pricetrend_validation():
    assert any("catalogue_cache_days" in p for p in Settings(catalogue_cache_days=0).validate())
    assert any("max_derived_titles" in p for p in Settings(max_derived_titles=-1).validate())
    assert any(
        "price_drop_window_days" in p for p in Settings(price_drop_window_days=-1).validate()
    )


def test_feed_settings_defaults_and_overrides(tmp_path):
    env, cfg = _missing(tmp_path)
    s = Settings.load(env_path=env, config_path=cfg)
    assert s.feed_max_deals == 800
    assert s.feed_page_sleep == 0.5
    assert s.publisher_metadata_batch == 100

    cfg2 = tmp_path / "config.yaml"
    cfg2.write_text(
        "feed_max_deals: 1200\nfeed_page_sleep: 1.0\npublisher_metadata_batch: 50\n",
        encoding="utf-8",
    )
    s2 = Settings.load(env_path=env, config_path=cfg2)
    assert s2.feed_max_deals == 1200
    assert s2.feed_page_sleep == 1.0
    assert s2.publisher_metadata_batch == 50


def test_feed_settings_validation():
    assert any("feed_max_deals" in p for p in Settings(feed_max_deals=10).validate())
    assert any("feed_max_deals" in p for p in Settings(feed_max_deals=6000).validate())
    assert any("feed_page_sleep" in p for p in Settings(feed_page_sleep=6.0).validate())
    assert any(
        "publisher_metadata_batch" in p for p in Settings(publisher_metadata_batch=0).validate()
    )


def test_deprecated_keys_warn_and_still_load(tmp_path, caplog):
    env, _ = _missing(tmp_path)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "derive_watchlist: true\nuse_appid_index: true\nresolve_steam_appids: true\n"
        "max_derived_titles: 99\nmin_discount_pct: 55\n",
        encoding="utf-8",
    )
    with caplog.at_level("WARNING", logger="src.config"):
        s = Settings.load(env_path=env, config_path=cfg)
    # Config still loads (non-deprecated keys honoured).
    assert s.min_discount_pct == 55
    # A deprecation warning is emitted for each deprecated key present.
    warned = caplog.text
    assert "derive_watchlist" in warned
    assert "use_appid_index" in warned
    assert "resolve_steam_appids" in warned


def test_real_config_yaml_shape_loads_cleanly(tmp_path):
    """My real config.yaml shape (no 'discounts' section, discovery_discount_pct: 1)
    loads without error and validates."""
    env, _ = _missing(tmp_path)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "sections:\n  - free\n  - wishlist\n  - preferred\n"
        "min_discount_pct: 50\ndiscovery_discount_pct: 1\n"
        "max_deals_per_run: 200\nderive_watchlist: true\nuse_appid_index: true\n"
        "stores:\n  - Steam\n  - Epic\n  - GOG\n",
        encoding="utf-8",
    )
    s = Settings.load(env_path=env, config_path=cfg)
    assert s.sections == ["free", "wishlist", "preferred"]
    assert s.discovery_discount_pct == 1
    assert s.stores == ["Steam", "Epic", "GOG"]
    # Only secret-related problems should remain (no schema errors from this shape).
    assert all("must be" not in p and "unknown sections" not in p for p in s.validate())


def test_yaml_overrides_defaults(tmp_path):
    env, _ = _missing(tmp_path)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("min_discount_pct: 80\nregion: US\n", encoding="utf-8")
    s = Settings.load(env_path=env, config_path=cfg)
    assert s.min_discount_pct == 80
    assert s.region == "US"
    assert s.currency == "INR"  # untouched default


def test_missing_required_secret_detected(monkeypatch, tmp_path):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("ITAD_API_KEY", raising=False)
    env, cfg = _missing(tmp_path)
    s = Settings.load(env_path=env, config_path=cfg)
    problems = s.validate()
    assert any("DISCORD_WEBHOOK_URL" in p for p in problems)
    assert any("ITAD_API_KEY" in p for p in problems)


def test_present_required_secrets_pass_validation(monkeypatch, tmp_path):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.test/webhook")
    monkeypatch.setenv("ITAD_API_KEY", "dummy")
    env, cfg = _missing(tmp_path)
    s = Settings.load(env_path=env, config_path=cfg)
    assert s.validate() == []


def test_optional_secrets_not_required(monkeypatch, tmp_path):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.test/webhook")
    monkeypatch.setenv("ITAD_API_KEY", "dummy")
    monkeypatch.delenv("STEAM_ID64", raising=False)
    env, cfg = _missing(tmp_path)
    s = Settings.load(env_path=env, config_path=cfg)
    assert s.validate() == []
    assert s.steam_id64 is None


def test_no_secret_value_in_check_config_output(monkeypatch, tmp_path):
    secret_webhook = "https://discord.com/api/webhooks/SECRET123/TOKENXYZ"
    secret_key = "ITAD_SECRET_VALUE_999"
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", secret_webhook)
    monkeypatch.setenv("ITAD_API_KEY", secret_key)
    # Steer config loading away from any real repo-root .env / config.yaml.
    monkeypatch.setattr("src.config.ENV_PATH", tmp_path / "no.env")
    monkeypatch.setattr("src.config.CONFIG_PATH", tmp_path / "no.yaml")

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main_module.main(["--check-config"])
    out = buf.getvalue()

    assert rc == 0
    assert secret_webhook not in out
    assert secret_key not in out
    assert "DISCORD_WEBHOOK_URL: set" in out
    assert "ITAD_API_KEY: set" in out


def test_check_config_reports_missing_and_exits_nonzero(monkeypatch, tmp_path):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("ITAD_API_KEY", raising=False)
    monkeypatch.setattr("src.config.ENV_PATH", tmp_path / "no.env")
    monkeypatch.setattr("src.config.CONFIG_PATH", tmp_path / "no.yaml")

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main_module.main(["--check-config"])
    out = buf.getvalue()

    assert rc == 1
    assert "DISCORD_WEBHOOK_URL: MISSING" in out
