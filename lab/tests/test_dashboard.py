"""Unit, endpoint, security, and UI smoke tests for the Neural Strategy Lab dashboard."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest

from dashboard.reader import NOT_AVAILABLE, build_dashboard_snapshot, query_events
from dashboard.server import create_server
from lab.journal import EventJournal


def _write_sample_status(work_dir: Path, pid: int, start_ts: int, hb_ts: int) -> None:
    end_ts = start_ts + 72 * 3600
    s_iso = datetime.fromtimestamp(start_ts, tz=timezone.utc).isoformat()
    e_iso = datetime.fromtimestamp(end_ts, tz=timezone.utc).isoformat()
    h_iso = datetime.fromtimestamp(hb_ts, tz=timezone.utc).isoformat()
    (work_dir / "STATUS.md").write_text(
        f"# Neural Strategy Lab — 72h Paper Experiment Status\n\n"
        f"- **Mode**: PAPER ONLY (no live keys, no real orders)\n"
        f"- **Neural Model**: `UNTRAINED` (deterministic rule-based baseline)\n"
        f"- **Start UTC**: `{s_iso}`\n"
        f"- **Scheduled End UTC**: `{e_iso}`\n"
        f"- **Last Heartbeat UTC**: `{h_iso}`\n"
        f"- **PID**: `{pid}`\n"
        f"- **SPOT Ledger**: equity=`25.0000 USDT`, open=`0`, closed=`0`\n"
        f"- **FUTURES Ledger**: equity=`24.9960 USDT`, open=`1`, closed=`0`\n"
        f"- **Sampled Universe**: SPOT=`50` symbols, FUTURES=`50` symbols\n"
        f"- **Blockers**: None\n",
        encoding="utf-8",
    )


def _http_get(url: str, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), err.read()


def test_no_fake_metrics_when_no_closed_trades(tmp_path: Path) -> None:
    now = 1_794_933_000
    _write_sample_status(tmp_path, pid=os.getpid(), start_ts=now - 300, hb_ts=now - 20)
    (tmp_path / "lab.log").write_text("2026-11-17 16:23:24,332 | INFO | Started\n", encoding="utf-8")
    (tmp_path / "dashboard_agy.log").write_text("", encoding="utf-8")

    j = EventJournal(tmp_path / "lab_journal.db")
    j.append("hb_1", "HEARTBEAT", "SYSTEM", "ALL", {"spot_equity": 25.0, "futures_equity": 25.0, "spot_universe": 50, "futures_universe": 50}, ts_utc=now - 20)
    j.append(
        "open_1", "POSITION_OPENED", "FUTURES", "BTCUSDT",
        {"symbol": "BTCUSDT", "ledger": "FUTURES", "side": "LONG", "qty": 0.001, "entry_price": 90000.0, "stop_loss": 89000.0, "take_profit": 92000.0, "leverage": 1.0, "margin_used": 90.0, "liquidation_price": 9000.0, "opened_at_utc": now - 40, "strategy": "breakout", "category": "major"},
        ts_utc=now - 40,
    )

    snap = build_dashboard_snapshot(tmp_path, now_utc=now)
    assert snap["neural_model"]["status"] == "UNTRAINED"
    assert snap["heartbeat"]["status"] == "HEALTHY"
    assert snap["heartbeat"]["age_sec"] == 20
    assert len(snap["positions"]["open"]) == 1
    assert len(snap["positions"]["closed"]) == 0

    comb = snap["ledgers"]["COMBINED"]
    assert comb["closed_count"] == 0
    assert comb["win_rate_pct"] is None
    assert comb["profit_factor"] is None
    assert comb["profit_factor_display"] == NOT_AVAILABLE
    assert comb["recorded_exit_fees"] is None
    assert comb["max_drawdown_pct"] is None  # only 1 heartbeat point
    assert snap["universe"]["full_list_persisted"] is False
    assert snap["universe"]["spot_count"] == 50


def test_real_metrics_computed_from_closed_trades_and_drawdown(tmp_path: Path) -> None:
    now = 1_794_935_000
    _write_sample_status(tmp_path, pid=os.getpid(), start_ts=now - 1000, hb_ts=now - 10)
    j = EventJournal(tmp_path / "lab_journal.db")
    j.append("hb_1", "HEARTBEAT", "SYSTEM", "ALL", {"spot_equity": 25.0, "futures_equity": 25.0, "spot_universe": 48, "futures_universe": 49}, ts_utc=now - 200)
    j.append("hb_2", "HEARTBEAT", "SYSTEM", "ALL", {"spot_equity": 24.5, "futures_equity": 25.0, "spot_universe": 48, "futures_universe": 49}, ts_utc=now - 100)
    j.append("hb_3", "HEARTBEAT", "SYSTEM", "ALL", {"spot_equity": 25.2, "futures_equity": 25.3, "spot_universe": 48, "futures_universe": 49}, ts_utc=now - 10)

    j.append("open_s1", "POSITION_OPENED", "SPOT", "ETHUSDT", {"symbol": "ETHUSDT", "ledger": "SPOT", "side": "LONG", "qty": 0.01, "entry_price": 3000.0, "opened_at_utc": now - 190, "strategy": "trend_pullback", "category": "major"}, ts_utc=now - 190)
    j.append("close_s1", "POSITION_CLOSED", "SPOT", "ETHUSDT", {"symbol": "ETHUSDT", "ledger": "SPOT", "side": "LONG", "qty": 0.01, "entry_price": 3000.0, "exit_price": 3050.0, "net_pnl": 0.45, "fee": 0.03, "reason": "take_profit", "opened_at_utc": now - 190, "closed_at_utc": now - 90, "strategy": "trend_pullback", "category": "major"}, ts_utc=now - 90)

    j.append("open_f1", "POSITION_OPENED", "FUTURES", "UNIUSDT", {"symbol": "UNIUSDT", "ledger": "FUTURES", "side": "SHORT", "qty": 1.0, "entry_price": 10.0, "opened_at_utc": now - 180, "strategy": "mean_reversion", "category": "defi"}, ts_utc=now - 180)
    j.append("close_f1", "POSITION_CLOSED", "FUTURES", "UNIUSDT", {"symbol": "UNIUSDT", "ledger": "FUTURES", "side": "SHORT", "qty": 1.0, "entry_price": 10.0, "exit_price": 10.15, "net_pnl": -0.15, "fee": 0.01, "reason": "stop_loss", "opened_at_utc": now - 180, "closed_at_utc": now - 80, "strategy": "mean_reversion", "category": "defi"}, ts_utc=now - 80)

    j.append("rej_1", "ORDER_REJECTED", "SPOT", "SOLUSDT", {"reason": "skip_below_min_notional", "strategy": "breakout"}, ts_utc=now - 50)
    j.append("stale_1", "STALE_DATA_SKIP", "FUTURES", "ADAUSDT", {"ts": now - 400}, ts_utc=now - 30)

    snap = build_dashboard_snapshot(tmp_path, now_utc=now)
    assert len(snap["positions"]["open"]) == 0
    assert len(snap["positions"]["closed"]) == 2
    comb = snap["ledgers"]["COMBINED"]
    assert comb["wins"] == 1 and comb["losses"] == 1
    assert comb["win_rate_pct"] == 50.0
    assert comb["profit_factor"] == pytest.approx(3.0, rel=1e-4)
    assert comb["recorded_exit_fees"] == pytest.approx(0.04, rel=1e-4)
    assert comb["max_drawdown_pct"] == pytest.approx(1.0, rel=1e-3)  # 50.0 -> 49.5 = 1%
    assert snap["decisions"]["rejection_count"] == 1
    assert snap["decisions"]["stale_skip_count"] == 1
    assert snap["universe"]["observed_categories"] == {"major": 2, "defi": 2}


def test_stale_and_offline_warnings(tmp_path: Path) -> None:
    now = 1_794_935_000
    _write_sample_status(tmp_path, pid=os.getpid(), start_ts=now - 1000, hb_ts=now - 200)
    j = EventJournal(tmp_path / "lab_journal.db")
    j.append("hb_1", "HEARTBEAT", "SYSTEM", "ALL", {"spot_equity": 25.0, "futures_equity": 25.0, "spot_universe": 50, "futures_universe": 50}, ts_utc=now - 200)

    stale_snap = build_dashboard_snapshot(tmp_path, now_utc=now)
    assert stale_snap["heartbeat"]["status"] == "STALE"
    assert "stale" in stale_snap["heartbeat"]["warning"].lower()

    _write_sample_status(tmp_path, pid=9999999, start_ts=now - 1000, hb_ts=now - 10)
    dead_snap = build_dashboard_snapshot(tmp_path, now_utc=now)
    assert dead_snap["heartbeat"]["status"] == "OFFLINE"
    assert not dead_snap["process"]["alive"]


def test_non_loopback_requires_token() -> None:
    with pytest.raises(ValueError):
        create_server(host="0.0.0.0", port=0, work_dir="/root/neural-strategy-lab", auth_token=None)


def test_http_endpoints_auth_and_ui_smoke(tmp_path: Path) -> None:
    now = int(time.time())
    _write_sample_status(tmp_path, pid=os.getpid(), start_ts=now - 120, hb_ts=now - 10)
    (tmp_path / "lab.log").write_text("2026-11-17 16:23:24 | INFO | Smoke ok\n", encoding="utf-8")
    (tmp_path / "dashboard_agy.log").write_text("AGY isolated session line\n", encoding="utf-8")
    j = EventJournal(tmp_path / "lab_journal.db")
    j.append("hb_smoke", "HEARTBEAT", "SYSTEM", "ALL", {"spot_equity": 25.0, "futures_equity": 25.0, "spot_universe": 50, "futures_universe": 50}, ts_utc=now - 10)

    token = "secret-test-token-123"
    srv = create_server(host="127.0.0.1", port=0, work_dir=tmp_path, auth_token=token)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{port}"

        # Health requires token when auth_token is configured and does not leak private root path
        st_h_unauth, _, _ = _http_get(f"{base}/api/health")
        assert st_h_unauth == 401
        st, hdrs, body = _http_get(f"{base}/api/health", headers={"Authorization": f"Bearer {token}"})
        assert st == 200
        assert hdrs.get("X-Content-Type-Options") == "nosniff"
        health = json.loads(body)
        assert health["status"] == "ok" and health["paper_only"] is True and health["auth_required"] is True
        assert "/root/" not in body.decode("utf-8")

        # Protected endpoints require token
        st_unauth, _, _ = _http_get(f"{base}/api/dashboard")
        assert st_unauth == 401
        st_bad, _, _ = _http_get(f"{base}/api/events", headers={"Authorization": "Bearer wrong"})
        assert st_bad == 401

        # Authorized requests succeed
        st_ok, _, dash_body = _http_get(f"{base}/api/dashboard", headers={"Authorization": f"Bearer {token}"})
        assert st_ok == 200
        dash = json.loads(dash_body)
        assert dash["neural_model"]["status"] == "UNTRAINED"
        assert dash["logs"]["agy_activity"]["isolated"] is True
        assert "/root/" not in dash_body.decode("utf-8")

        st_ev, _, ev_body = _http_get(f"{base}/api/events?type=HEARTBEAT&limit=5", headers={"X-Dashboard-Token": token})
        assert st_ev == 200
        assert len(json.loads(ev_body)["events"]) == 1

        st_lg, _, lg_body = _http_get(f"{base}/api/logs", headers={"Authorization": f"Bearer {token}"})
        assert st_lg == 200
        assert "Smoke ok" in "\n".join(json.loads(lg_body)["lab_log"]["lines"])

        # UI HTML smoke check (requires token when auth_token is configured)
        st_html_unauth, _, _ = _http_get(f"{base}/")
        assert st_html_unauth == 401
        st_html, _, html_bytes = _http_get(f"{base}/", headers={"Authorization": f"Bearer {token}"})
        assert st_html == 200
        html = html_bytes.decode("utf-8")
        for needle in (
            "Neural Strategy Lab",
            "UNTRAINED",
            "PAPER ONLY",
            "countdown-clock",
            "Positions &amp; Trades",
            "Universe &amp; Rejections",
            "Logs &amp; AGY",
            "Journal &amp; Security",
            "not yet available",
        ):
            assert needle in html
        assert "/root/" not in html

        # Read-only method enforcement and path traversal 404
        req_post = urllib.request.Request(f"{base}/api/dashboard", data=b"{}", method="POST")
        with pytest.raises(urllib.error.HTTPError) as exc_post:
            urllib.request.urlopen(req_post, timeout=5)
        assert exc_post.value.code == 405

        st_404, _, _ = _http_get(f"{base}/../../etc/passwd")
        assert st_404 == 404
    finally:
        srv.shutdown()
        srv.server_close()


def test_query_events_filtering(tmp_path: Path) -> None:
    j = EventJournal(tmp_path / "lab_journal.db")
    j.append("e1", "HEARTBEAT", "SYSTEM", "ALL", {}, ts_utc=100)
    j.append("e2", "ORDER_REJECTED", "SPOT", "BTCUSDT", {"reason": "min_notional"}, ts_utc=101)
    res = query_events(tmp_path, event_type="ORDER_REJECTED", ledger="SPOT", limit=10)
    assert res["readable"] is True
    assert res["total_matching"] == 1
    assert res["events"][0]["symbol"] == "BTCUSDT"
