"""Tests for Phase 2 upgrade helper, live execution adapter gate, and alerts."""

from __future__ import annotations

from pathlib import Path

from v2.alerts import send_alert
from v2.journal import V2Journal
from v2.live_adapter import LiveExecutionAdapter
from v2.phase2_upgrade import run_phase2_upgrade


def test_live_adapter_is_gated_and_kill_switch_works() -> None:
    adapter = LiveExecutionAdapter("SPOT")
    ok, reason = adapter.is_ready()
    assert not ok
    assert reason == "paper_only_hard_rule"

    res = adapter.place_market_order("BTCUSDT", "BUY", 0.001)
    assert res["executed"] is False

    adapter.activate_kill_switch()
    ok2, reason2 = adapter.is_ready()
    assert not ok2
    assert reason2 == "paper_only_hard_rule"


def test_send_alert_noop_when_unconfigured() -> None:
    assert send_alert("TEST", "no webhook configured") is False


def test_alert_webhook_rejects_private_http_and_redirect_targets(monkeypatch) -> None:
    from v2 import alerts

    monkeypatch.setenv("ALERT_WEBHOOK_URL", "http://127.0.0.1/internal")
    assert alerts.send_alert("TEST", "message") is False
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://example.com/hook")
    called = {}

    class Response:
        ok = False

    def fake_post(url, **kwargs):
        called.update(kwargs)
        return Response()

    monkeypatch.setattr(alerts.requests, "post", fake_post)
    assert alerts.send_alert("TEST", "message") is False
    assert called["allow_redirects"] is False


def test_phase2_upgrade_reads_journal_and_writes_lessons(tmp_path: Path) -> None:
    j = V2Journal(tmp_path / "v2_journal.db")
    j.append("c1", "POSITION_CLOSED", "SPOT", "BTCUSDT", {"strategy": "breakout", "net_pnl": -2.0, "reason": "stop_loss"}, ts_utc=100)
    j.append("c2", "POSITION_CLOSED", "SPOT", "ETHUSDT", {"strategy": "breakout", "net_pnl": -1.5, "reason": "stop_loss"}, ts_utc=101)
    j.append("c3", "POSITION_CLOSED", "SPOT", "SOLUSDT", {"strategy": "breakout", "net_pnl": -1.0, "reason": "stop_loss"}, ts_utc=102)

    lessons = run_phase2_upgrade(tmp_path)
    assert lessons["total_trades"] == 3
    assert "breakout" in lessons["blocked_strategies"]
    assert (tmp_path / "phase2_lessons.json").exists()
