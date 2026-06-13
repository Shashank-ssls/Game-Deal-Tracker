"""Tests for CLI plumbing: verbosity flag parsing and log-level mapping."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from src.config import Settings
from src.db import Database
from src.main import (
    _configure_logging,
    _ending_soon,
    _WarningCollector,
    build_parser,
    print_stats,
)
from src.models import Deal


def test_verbose_flag_counts():
    parser = build_parser()
    assert parser.parse_args([]).verbose == 0
    assert parser.parse_args(["-v"]).verbose == 1
    assert parser.parse_args(["-vv"]).verbose == 2


def _deal_ending_in(hours):
    ends = datetime.now(UTC) + timedelta(hours=hours)
    return Deal(title="T", store="Epic", url="u", source="epic", source_game_id="1",
                is_free=True, ends_at=ends)


def test_ending_soon_window():
    assert _ending_soon(_deal_ending_in(12), 48) is True
    assert _ending_soon(_deal_ending_in(72), 48) is False   # outside window
    assert _ending_soon(_deal_ending_in(12), 0) is False     # feature off
    assert _ending_soon(_deal_ending_in(-5), 48) is False    # already ended


def test_print_stats(tmp_path, capsys):
    db = Database(tmp_path / "t.db")
    db.log_run(deals_found=5, deals_new=2, errors=["epic timeout"])
    rc = print_stats(Settings(db_path=tmp_path / "t.db"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "Last 1 run" in out
    assert "5" in out and "2" in out


def test_print_stats_shows_source_counts(tmp_path, capsys):
    db = Database(tmp_path / "t.db")
    db.log_run(deals_found=49, deals_new=49,
               source_counts={"itad": 500, "cheapshark": 500, "steam": 0, "epic": 2})
    rc = print_stats(Settings(db_path=tmp_path / "t.db"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "itad=500" in out and "cs=500" in out and "st=0" in out and "ep=2" in out


def test_print_stats_handles_missing_source_counts(tmp_path, capsys):
    db = Database(tmp_path / "t.db")
    db.log_run(deals_found=1, deals_new=1)  # legacy run with no source_counts
    print_stats(Settings(db_path=tmp_path / "t.db"))
    assert "-" in capsys.readouterr().out  # renders a placeholder, never crashes


def test_print_stats_empty(tmp_path, capsys):
    Database(tmp_path / "empty.db")
    rc = print_stats(Settings(db_path=tmp_path / "empty.db"))
    assert rc == 0
    assert "No runs recorded" in capsys.readouterr().out


def test_configure_logging_levels():
    _configure_logging(0)
    assert logging.getLogger().level == logging.WARNING
    _configure_logging(1)
    assert logging.getLogger().level == logging.INFO
    _configure_logging(2)
    assert logging.getLogger().level == logging.DEBUG


def test_warning_collector_captures_source_warnings():
    collector = _WarningCollector()
    collector.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    src_logger = logging.getLogger("src")
    src_logger.addHandler(collector)
    try:
        logging.getLogger("src.sources.epic").warning("epic exploded")
    finally:
        src_logger.removeHandler(collector)
    assert any("epic exploded" in m for m in collector.messages)
