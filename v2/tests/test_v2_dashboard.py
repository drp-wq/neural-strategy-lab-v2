"""Tests for V2 dashboard integration and V1 archive separation."""

from __future__ import annotations

import json
import os
import threading
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from dashboard.reader import build_dashboard_snapshot, query_events
from dashboard.server import create_server
from lab.journal import EventJournal
from v2.collectors import MarketQuote, PublicBinanceV2Collector
from v2.ledger import SymbolRules
from v2.runner import NeuralLabV2Runner
from v2.tests.test_v2 import _make_uptrend_pullback_df


def test_dashboard_reads_v2_and_separates_v1_archive(tmp_path: Path) -> None:
    now = 1_795_000_000
    # 1) Create preserved V1 files
    v1_j = EventJournal(tmp_path / "lab_journal.db")
    v1_j.append("v1_hb_1", "HEARTBEAT", "SYSTEM", "ALL", {"spot_equity": 24.98, "futures_equity": 25.07, "spot_universe": 50, "futures_universe": 50}, ts_utc=now - 600)
    (tmp_path / "STATUS.md").write_text(
        f"# Neural Strategy Lab\n- **PID**: `9999999`\n- **Start UTC**: `{datetime.fromtimestamp(now - 1200, tz=timezone.utc).isoformat()}`\n"
        f"- **Scheduled End UTC**: `{datetime.fromtimestamp(now + 70 * 3600, tz=timezone.utc).isoformat()}`\n"
        f"- **Last Heartbeat UTC**: `{datetime.fromtimestamp(now - 600, tz=timezone.utc).isoformat()}`\n",
        encoding="utf-8",
    )

    # 2) Run a V2 cycle under tmp_path / v2
    df_sig = _make_uptrend_pullback_df(close_ms=now * 1000)

    class MockCol(PublicBinanceV2Collector):
        def fetch_market_quotes(self, min_vol_usdt=5_000_000.0, max_spread_pct=0.005, now_utc=None):
            ts = now if now_utc is None else now_utc
            r = SymbolRules(min_qty=0.01, step_size=0.01, min_notional=5.0)
            sq = {"ETHUSDT": MarketQuote("SPOT", "ETHUSDT", 117.0, 117.0, 117.0, 117.0, 5e7, 2.0, 0.0, r, ts)}
            fq = {"SOLUSDT": MarketQuote("FUTURES", "SOLUSDT", 117.0, 117.0, 117.0, 117.0, 4e7, 1.5, 0.0001, r, ts)}
            return sq, fq

        def fetch_klines_batch(self, items, interval="15m", limit=60, max_workers=6):
            return {sym: df_sig for _, sym in items}

    v2_dir = tmp_path / "v2"
    runner = NeuralLabV2Runner(v2_dir=v2_dir, collector=MockCol(), now_utc=now)
    runner.run_cycle(now_utc=now)
    runner.write_status(2, 1, 1, now)
    # Patch PID in V2_STATUS.md to current process for HEALTHY check
    st_path = v2_dir / "V2_STATUS.md"
    assert str(os.getpid()) in st_path.read_text(encoding="utf-8")

    snap = build_dashboard_snapshot(tmp_path, now_utc=now + 5)
    assert snap["experiment_version"] == "v2"
    assert snap["neural_model"]["status"] == "UNTRAINED"
    assert snap["heartbeat"]["status"] == "HEALTHY"
    assert snap["ledgers"]["SPOT"]["starting_capital"] == 2500.0
    assert snap["ledgers"]["FUTURES"]["starting_capital"] == 2500.0
    assert snap["ledgers"]["COMBINED"]["starting_capital"] == 5000.0
    assert snap["v2"]["open_slots_used"] == 2
    assert snap["v2"]["global_notional_exposure"] > 90.0
    assert snap["universe"]["full_list_persisted"] is True
    assert len(snap["universe"]["symbols"]) == 2

    # V1 archive must be clearly separated and marked retired (PID 9999999 not running)
    v1 = snap["v1_archive"]
    assert v1 is not None and v1["present"] is True and v1["retired"] is True
    assert v1["ledgers"]["COMBINED"]["starting_capital"] == 50.0
    assert v1["ledgers"]["COMBINED"]["latest_equity"] == 50.05

    # Network binding is covered by deployment verification; request handler tests below
    # avoid opening sockets in restricted test environments.


def test_dashboard_auth_covers_ui_health_and_nonlocal_override_is_blocked(tmp_path: Path) -> None:
    import urllib.error
    import pytest

    from dashboard.server import DashboardRequestHandler

    server = type("FakeServer", (), {"auth_token": "test-secret"})()
    for path in ("/", "/api/health", "/api/dashboard"):
        handler = object.__new__(DashboardRequestHandler)
        handler.server = server
        handler.path = path
        handler.headers = {}
        handler.wfile = __import__("io").BytesIO()
        handler.response = None
        handler.send_response = lambda status, h=handler: setattr(h, "response", status)
        handler.send_header = lambda *_args: None
        handler.end_headers = lambda: None
        handler.do_GET()
        assert handler.response == 401
    with pytest.raises(ValueError, match="permanently disabled"):
        create_server(host="0.0.0.0", port=0, work_dir=tmp_path, allow_unauth_nonlocal=True)
