"""Unit and smoke tests for /root/neural-strategy-lab."""

from __future__ import annotations

from pathlib import Path

import pytest

from lab.collectors import MarketSnapshot, PublicBinanceCollector
from lab.journal import EventJournal
from lab.ledger import PaperLedger, SymbolRules
from lab.runner import NeuralLabRunner


def test_separate_ledgers_and_accounting() -> None:
    spot = PaperLedger("SPOT", starting_capital=25.0)
    fut = PaperLedger("FUTURES", starting_capital=25.0)
    assert spot.equity() == 25.0
    assert fut.equity() == 25.0
    # Spot cannot short
    ok, reason, _ = spot.open_position(
        "BTCUSDT", "SHORT", 100.0, 100.1, 102.0, 96.0, SymbolRules(), ts_utc=1
    )
    assert not ok and reason == "spot_long_only"
    # Futures max leverage capped at 2x
    assert fut.max_leverage == 2.0


def test_min_notional_skip_never_fakes_fill() -> None:
    spot = PaperLedger("SPOT", starting_capital=25.0)
    # 1% of 25 = 0.25 risk; with 2% stop, notional = 12.5; with min_notional=50, must SKIP
    ok, reason, pos = spot.open_position(
        "BTCUSDT", "LONG", 100.0, 100.0, 98.0, 104.0,
        SymbolRules(min_qty=0.001, step_size=0.001, min_notional=50.0),
        ts_utc=1,
    )
    assert not ok and reason == "skip_below_min_notional" and pos is None
    assert spot.equity() == 25.0


def test_stale_feed_fail_closed() -> None:
    col = PublicBinanceCollector("SPOT", stale_sec=60)
    snap = MarketSnapshot("BTCUSDT", 100.0, 100.1, 100.05, 1e7, 1.0, "major", ts_utc=1000)
    assert col.is_stale(snap, now_utc=1100)
    assert not col.is_stale(snap, now_utc=1030)


def test_journal_duplicate_idempotency(tmp_path: Path) -> None:
    j = EventJournal(tmp_path / "j.db")
    assert j.append("evt_1", "HEARTBEAT", "SYSTEM", "ALL", {"ok": True}, ts_utc=100)
    assert not j.append("evt_1", "HEARTBEAT", "SYSTEM", "ALL", {"ok": True}, ts_utc=100)
    assert len(j.query("HEARTBEAT")) == 1


def test_time_bound_72h_stop(tmp_path: Path) -> None:
    runner = NeuralLabRunner(work_dir=tmp_path, duration_sec=72 * 3600)
    assert not runner.is_expired(runner.start_utc + 100)
    assert runner.is_expired(runner.start_utc + 72 * 3600)
