"""Comprehensive test suite for Neural Strategy Lab V2."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from v2.collectors import (
    ALLOWED_PUBLIC_PATHS,
    MarketQuote,
    PublicBinanceV2Collector,
    parse_exchange_rules,
    validate_quote,
)
from v2.config import V2Config
from v2.features import extract_features
from v2.journal import V2Journal
from v2.ledger import GlobalPortfolioCoordinator, PaperLedger, SymbolRules, compute_target_qty
from v2.runner import NeuralLabV2Runner
from v2.strategies import NEURAL_MODEL_STATUS, evaluate_baseline, select_route


def _make_flat_df(price: float = 100.0, n: int = 60, close_ms: int = 1_795_000_000_000) -> pd.DataFrame:
    """Create a completely neutral sideways OHLCV series that triggers HOLD."""
    idx = np.arange(n)
    close = np.full(n, price, dtype=float)
    high = close * 1.001
    low = close * 0.999
    vol = np.full(n, 1000.0, dtype=float)
    open_ms = close_ms - (n - 1 - idx) * 900_000 - 899_999
    c_ms = close_ms - (n - 1 - idx) * 900_000
    return pd.DataFrame(
        {
            "open_time": open_ms,
            "open": close,
            "high": high,
            "low": low,
            "close": close,
            "volume": vol,
            "close_time": c_ms,
        }
    )


def _make_uptrend_pullback_df(n: int = 60, close_ms: int = 1_795_000_000_000) -> pd.DataFrame:
    """Create an uptrend followed by a mild pullback (EMA12 > EMA26, RSI <= 55)."""
    # Strong rise for 50 bars then 10-bar gentle dip so RSI <= 55 while EMA12 > EMA26
    base = np.concatenate(
        [
            np.linspace(100.0, 120.0, n - 10),
            np.linspace(120.0, 117.0, 10),
        ]
    )
    high = base * 1.003
    low = base * 0.997
    vol = np.full(n, 5000.0, dtype=float)
    open_ms = close_ms - (n - 1 - np.arange(n)) * 900_000 - 899_999
    c_ms = close_ms - (n - 1 - np.arange(n)) * 900_000
    return pd.DataFrame(
        {
            "open_time": open_ms,
            "open": base,
            "high": high,
            "low": low,
            "close": base,
            "volume": vol,
            "close_time": c_ms,
        }
    )


def test_50_notional_and_actual_exchange_filters() -> None:
    # Real Binance Spot BTCUSDT payload vs Futures BTCUSDT payload
    spot_btc_payload = {
        "symbol": "BTCUSDT",
        "filters": [
            {"filterType": "LOT_SIZE", "minQty": "0.00001000", "maxQty": "9000.0", "stepSize": "0.00001000"},
            {"filterType": "NOTIONAL", "minNotional": "5.00000000", "maxNotional": "9000000.0"},
        ],
    }
    fut_btc_payload = {
        "symbol": "BTCUSDT",
        "filters": [
            {"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "1000", "stepSize": "0.001"},
            {"filterType": "MIN_NOTIONAL", "notional": "50"},
        ],
    }

    spot_rules = parse_exchange_rules(spot_btc_payload)
    fut_rules = parse_exchange_rules(fut_btc_payload)
    assert spot_rules is not None and spot_rules.step_size == pytest.approx(0.00001)
    assert fut_rules is not None and fut_rules.min_notional == pytest.approx(50.0)

    # At $90,000 BTC, Spot allows ~0.00056 BTC = ~$50.40
    ok_s, _, qty_s, not_s = compute_target_qty(50.0, 90_000.0, spot_rules, 45.0, 55.0)
    assert ok_s and 45.0 <= not_s <= 55.0
    assert qty_s == pytest.approx(0.00056)

    # At $90,000 BTC, Futures minQty=0.001 -> $90 notional -> must SKIP outside tolerance
    ok_f, reason_f, _, _ = compute_target_qty(50.0, 90_000.0, fut_rules, 45.0, 55.0)
    assert not ok_f and reason_f == "skip_outside_notional_tolerance"

    # If minNotional is 100.0 -> must SKIP below min_notional
    high_min = SymbolRules(min_qty=0.01, step_size=0.01, min_notional=100.0)
    ok_m, reason_m, _, _ = compute_target_qty(50.0, 10.0, high_min, 45.0, 55.0)
    assert not ok_m and reason_m == "skip_below_min_notional"


def test_100_slots_5000_cap_and_no_duplicate_symbols() -> None:
    # Give enough cash in both ledgers so 100-slot / 5000-cap are the binding constraints
    spot = PaperLedger("SPOT", starting_capital=3000.0)
    fut = PaperLedger("FUTURES", starting_capital=3000.0)
    coord = GlobalPortfolioCoordinator(
        spot, fut, max_concurrent_positions=100, global_max_notional=5000.0, target_notional=50.0
    )
    rules = SymbolRules(min_qty=0.1, step_size=0.1, min_notional=5.0)

    # Open 49 Spot + 51 Futures = 100 concurrent $50 positions = $5000 gross notional
    for i in range(49):
        ok, reason, pos = coord.try_open(
            "SPOT", f"SYM{i}USDT", "LONG", 10.0, 10.0, 9.7, 10.6, rules, ts_utc=1000
        )
        assert ok and reason == "filled" and pos is not None
        assert 49.5 <= pos.actual_notional <= 50.5

    # Duplicate symbol on Futures must fail
    ok_dup, reason_dup, _ = coord.try_open(
        "FUTURES", "SYM0USDT", "SHORT", 10.0, 10.0, 10.3, 9.4, rules, ts_utc=1000, leverage=2.0
    )
    assert not ok_dup and reason_dup == "duplicate_symbol_exposure"

    for i in range(49, 100):
        ok, reason, pos = coord.try_open(
            "FUTURES", f"SYM{i}USDT", "SHORT", 10.0, 10.0, 10.3, 9.4, rules, ts_utc=1000, leverage=2.0
        )
        assert ok and reason == "filled" and pos is not None

    assert coord.total_open_positions() == 100
    assert coord.total_exposure() == pytest.approx(5000.0, abs=10.0)

    # 101st slot must be rejected
    ok_101, reason_101, _ = coord.try_open(
        "FUTURES", "EXTRAUSDT", "LONG", 10.0, 10.0, 9.7, 10.6, rules, ts_utc=1000, leverage=2.0
    )
    assert not ok_101 and reason_101 == "max_100_positions_reached"


def test_global_5000_notional_cap_blocks_even_under_100_slots() -> None:
    spot = PaperLedger("SPOT", starting_capital=3000.0)
    fut = PaperLedger("FUTURES", starting_capital=3000.0)
    # Set max_concurrent_positions=200 to isolate global 5000 cap
    coord = GlobalPortfolioCoordinator(
        spot, fut, max_concurrent_positions=200, global_max_notional=5000.0, target_notional=50.0
    )
    rules = SymbolRules(min_qty=0.1, step_size=0.1, min_notional=5.0)

    opened = 0
    for i in range(105):
        ledger = "SPOT" if i < 50 else "FUTURES"
        ok, reason, _ = coord.try_open(
            ledger,  # type: ignore[arg-type]
            f"CAP{i}USDT",
            "LONG",
            10.0,
            10.0,
            9.7,
            10.6,
            rules,
            ts_utc=1000,
            leverage=2.0 if ledger == "FUTURES" else 1.0,
        )
        if ok:
            opened += 1
        else:
            assert reason == "global_exposure_cap_reached"
            break
    assert coord.total_exposure() <= 5000.0 + 1e-6
    assert opened in (99, 100)


def test_concurrent_global_cap_attempts_are_serialized() -> None:
    from concurrent.futures import ThreadPoolExecutor

    spot = PaperLedger("SPOT", starting_capital=10000.0)
    fut = PaperLedger("FUTURES", starting_capital=10000.0)
    coord = GlobalPortfolioCoordinator(spot, fut, max_concurrent_positions=100, global_max_notional=5000.0)
    rules = SymbolRules(min_qty=0.1, step_size=0.1, min_notional=5.0)

    def open_one(i: int):
        return coord.try_open("SPOT", f"RACE{i}USDT", "LONG", 10.0, 10.0, 9.7, 10.6, rules, ts_utc=1000)

    with ThreadPoolExecutor(max_workers=40) as pool:
        results = list(pool.map(open_one, range(140)))
    assert sum(1 for ok, _, _ in results if ok) <= 100
    assert coord.total_open_positions() == sum(1 for ok, _, _ in results if ok)
    assert coord.total_open_positions() >= 99
    assert coord.total_exposure() <= 5000.0 + 1e-8


def test_invalid_exchange_filters_fail_closed() -> None:
    for rules in (
        SymbolRules(min_qty=0.1, step_size=0.0, min_notional=5.0),
        SymbolRules(min_qty=0.1, step_size=float("nan"), min_notional=5.0),
        SymbolRules(min_qty=0.1, step_size=0.1, min_notional=5.0, max_qty=0.0),
    ):
        ok, reason, qty, notional = compute_target_qty(50.0, 10.0, rules, 45.0, 55.0)
        assert not ok and reason == "invalid_exchange_rules"
        assert qty == notional == 0.0


def test_spot_futures_independence_and_daily_dd_halt() -> None:
    spot = PaperLedger("SPOT", starting_capital=2500.0, daily_dd_halt_pct=0.05, trading_day="1970-01-01")
    fut = PaperLedger("FUTURES", starting_capital=2500.0, daily_dd_halt_pct=0.05, max_leverage=2.0, trading_day="1970-01-01")
    rules = SymbolRules(min_qty=0.01, step_size=0.01, min_notional=5.0)

    # Spot is long-only
    ok_s, r_s, _ = spot.open_position("ETHUSDT", "SHORT", 100.0, 100.0, 102.0, 96.0, rules, ts_utc=1000)
    assert not ok_s and r_s == "spot_long_only"

    # Simulate >= 5% daily loss on Spot ONLY
    spot.cash_usdt -= 130.0
    spot.realized_pnl -= 130.0
    ok_halt, reason_halt, _ = spot.open_position("ETHUSDT", "LONG", 100.0, 100.0, 98.0, 104.0, rules, ts_utc=1001)
    assert not ok_halt and "daily_drawdown_halt" in reason_halt
    assert spot.daily_halted is True

    # Futures must remain completely unaffected and able to open SHORT at 2x
    assert fut.daily_halted is False
    ok_f, reason_f, pos_f = fut.open_position(
        "ETHUSDT", "SHORT", 100.0, 100.0, 102.0, 96.0, rules, ts_utc=1001, leverage=2.0
    )
    assert ok_f and reason_f == "filled" and pos_f is not None
    assert pos_f.leverage == 2.0


def test_exact_pnl_accounting_fees_funding_and_liquidation() -> None:
    fut = PaperLedger("FUTURES", starting_capital=2500.0, fee_rate=0.0004, max_leverage=2.0)
    rules = SymbolRules(min_qty=0.01, step_size=0.01, min_notional=5.0)

    def _check_invariant() -> None:
        expected_eq = fut.starting_capital + fut.realized_pnl + fut.unrealized_pnl()
        assert fut.equity() == pytest.approx(expected_eq, abs=1e-9)

    _check_invariant()
    t0 = 1_700_006_400  # exact 8h boundary + offset
    ok, _, pos = fut.open_position(
        "SOLUSDT", "LONG", 100.0, 100.0, 95.0, 110.0, rules, ts_utc=t0, leverage=2.0, slippage_bps=0.0
    )
    assert ok and pos is not None
    assert pos.actual_notional == pytest.approx(50.0)
    assert pos.margin_used == pytest.approx(25.0)
    assert pos.entry_fee == pytest.approx(0.02)
    _check_invariant()

    # Mark goes up to 104 -> +$2 unrealized
    pos.update_mark(104.0)
    assert pos.unrealized_pnl == pytest.approx(2.0)
    _check_invariant()

    # Cross one 8h funding boundary with funding_rate = +0.001 (LONG pays 0.1% of mark notional)
    t_fund = t0 + 28800
    f_cost = fut.apply_funding_if_due("SOLUSDT", funding_rate=0.001, mark_price=104.0, ts_utc=t_fund)
    assert f_cost == pytest.approx(0.5 * 104.0 * 0.001)
    # Same bucket must not charge twice
    assert fut.apply_funding_if_due("SOLUSDT", funding_rate=0.001, mark_price=104.0, ts_utc=t_fund + 60) is None
    _check_invariant()

    # Close at 104 -> exit fee = 0.5 * 104 * 0.0004 = 0.0208
    rec = fut.close_position("SOLUSDT", 104.0, 104.0, "take_profit", ts_utc=t_fund + 120, slippage_bps=0.0)
    assert rec is not None
    expected_net = 2.0 - 0.02 - (0.5 * 104.0 * 0.0004) - (0.5 * 104.0 * 0.001)
    assert rec["net_pnl"] == pytest.approx(expected_net, abs=1e-9)
    assert fut.equity() == pytest.approx(2500.0 + expected_net, abs=1e-9)
    _check_invariant()


def test_idempotent_restart_preserves_clock_and_positions(tmp_path: Path) -> None:
    now = 1_795_000_000
    df_sig = _make_uptrend_pullback_df(close_ms=now * 1000)

    class MockCollector(PublicBinanceV2Collector):
        def fetch_market_quotes(self, min_vol_usdt=5_000_000.0, max_spread_pct=0.005, now_utc=None):
            ts = now if now_utc is None else now_utc
            r = SymbolRules(min_qty=0.01, step_size=0.01, min_notional=5.0)
            sq = {
                "ETHUSDT": MarketQuote("SPOT", "ETHUSDT", 117.0, 117.0, 117.0, 117.0, 5e7, 2.0, 0.0, r, ts)
            }
            fq = {
                "ETHUSDT": MarketQuote("FUTURES", "ETHUSDT", 117.0, 117.0, 117.0, 117.0, 6e7, 2.0, 0.0001, r, ts)
            }
            return sq, fq

        def fetch_klines_batch(self, items, interval="15m", limit=60, max_workers=6):
            return {sym: df_sig for _, sym in items}

    r1 = NeuralLabV2Runner(v2_dir=tmp_path, collector=MockCollector(), now_utc=now)
    hb1 = r1.run_cycle(now_utc=now)
    assert hb1["open_positions"] == 1
    orig_start = r1.start_utc
    orig_end = r1.end_utc
    orig_cash = r1.spot_ledger.cash_usdt

    # A simultaneously alive runner must fail instead of sharing the journal.
    with pytest.raises(RuntimeError, match="singleton lock"):
        NeuralLabV2Runner(v2_dir=tmp_path, collector=MockCollector(), now_utc=now + 1)

    # Simulate process exit, then restart 5 minutes later; clock & position remain intact.
    r1.close()
    r2 = NeuralLabV2Runner(v2_dir=tmp_path, collector=MockCollector(), now_utc=now + 300)
    assert r2.restored_from_checkpoint is True
    assert r2.start_utc == orig_start
    assert r2.end_utc == orig_end
    assert r2.coordinator.total_open_positions() == 1
    assert r2.spot_ledger.cash_usdt == pytest.approx(orig_cash, abs=1e-9)

    # Duplicate journal append is idempotent
    assert not r2.journal.append(f"v2_hb_{now}_1", "HEARTBEAT", "SYSTEM", "ALL", {}, ts_utc=now)

    r2.close()


def test_stale_data_and_invalid_exit_mark_fail_closed(tmp_path: Path) -> None:
    now = 1_795_000_000
    r = SymbolRules(min_qty=0.01, step_size=0.01, min_notional=5.0)
    stale_q = MarketQuote("SPOT", "ADAUSDT", 1.0, 1.0, 1.0, 1.0, 1e7, 1.0, 0.0, r, ts_utc=now - 500)
    ok_s, reason_s = validate_quote(stale_q, now_utc=now, stale_sec=120)
    assert not ok_s and reason_s == "stale_quote"

    crossed_q = MarketQuote("SPOT", "ADAUSDT", 1.05, 1.00, 1.02, 1.02, 1e7, 1.0, 0.0, r, ts_utc=now)
    ok_c, reason_c = validate_quote(crossed_q, now_utc=now, stale_sec=120)
    assert not ok_c and reason_c == "crossed_book"

    divergent_mark = MarketQuote("FUTURES", "ADAUSDT", 1.0, 1.0, 1.0, 2.0, 1e7, 1.0, 0.0, r, ts_utc=now)
    ok_d, reason_d = validate_quote(divergent_mark, now_utc=now, stale_sec=120)
    assert not ok_d and reason_d == "invalid_mark_divergence"

    # Runner must NOT close an open position on a stale or missing quote
    runner = NeuralLabV2Runner(v2_dir=tmp_path, now_utc=now)
    runner.spot_ledger.open_position(
        "ADAUSDT", "LONG", 1.0, 1.0, 0.9, 1.2, r, ts_utc=now, slippage_bps=0.0
    )
    events: list = []
    # Corrupt / stale quote with price below SL must be skipped, NOT closed!
    bad_q = MarketQuote("SPOT", "ADAUSDT", 0.5, 0.5, 0.5, 0.5, 1e7, -50.0, 0.0, r, ts_utc=now - 500)
    runner._update_open_positions(runner.spot_ledger, {"ADAUSDT": bad_q}, now, events)
    assert "ADAUSDT" in runner.spot_ledger.positions
    assert any(e[1] == "STALE_DATA_SKIP" for e in events)


def test_no_live_endpoints_and_untrained_label() -> None:
    col = PublicBinanceV2Collector()
    for bad_path in ("/api/v3/order", "/fapi/v1/order", "/api/v3/account", "/fapi/v2/balance"):
        with pytest.raises(ValueError, match="Blocked non-public endpoint"):
            col._get_json("SPOT", bad_path)
    assert all("order" not in p and "account" not in p for p in ALLOWED_PUBLIC_PATHS)
    assert NEURAL_MODEL_STATUS == "UNTRAINED"

    # Static check over v2/ python files for live order endpoint strings
    v2_root = Path(__file__).resolve().parent.parent
    for py_file in v2_root.glob("*.py"):
        txt = py_file.read_text(encoding="utf-8")
        assert "/api/v3/order" not in txt
        assert "/fapi/v1/order" not in txt
        assert "API_SECRET" not in txt


def test_72h_expiry_closes_all_positions_and_stops_entries(tmp_path: Path) -> None:
    now = 1_795_000_000
    r = SymbolRules(min_qty=0.01, step_size=0.01, min_notional=5.0)
    df_sig = _make_uptrend_pullback_df(close_ms=(now + 72 * 3600) * 1000)

    class MockCol(PublicBinanceV2Collector):
        def fetch_market_quotes(self, min_vol_usdt=5_000_000.0, max_spread_pct=0.005, now_utc=None):
            ts = now if now_utc is None else now_utc
            sq = {"SOLUSDT": MarketQuote("SPOT", "SOLUSDT", 117.0, 117.0, 117.0, 117.0, 5e7, 1.0, 0.0, r, ts)}
            return sq, {}

        def fetch_klines_batch(self, items, interval="15m", limit=60, max_workers=6):
            return {sym: df_sig for _, sym in items}

    runner = NeuralLabV2Runner(v2_dir=tmp_path, collector=MockCol(), now_utc=now)
    assert not runner.is_expired(now + 72 * 3600 - 1)
    assert runner.is_expired(now + 72 * 3600)

    # Open before expiry
    hb_before = runner.run_cycle(now_utc=now)
    assert hb_before["open_positions"] == 1

    # Expired cycle must NOT open any new trades
    runner.spot_ledger.close_position("SOLUSDT", 117.0, 117.0, "test", now)
    hb_after = runner.run_cycle(now_utc=now + 72 * 3600)
    assert hb_after["opened_this_cycle"] == 0

    # close_all_at_mark closes remaining open positions
    runner.spot_ledger.open_position("SOLUSDT", "LONG", 117.0, 117.0, 112.0, 125.0, r, ts_utc=now)
    closed_n = runner.close_all_at_mark(now_utc=now + 72 * 3600 + 1)
    assert closed_n == 1
    assert len(runner.spot_ledger.positions) == 0


def test_no_forced_signals_on_neutral_market(tmp_path: Path) -> None:
    now = 1_795_000_000
    df_flat = _make_flat_df(close_ms=now * 1000)
    feat = extract_features(df_flat)
    assert feat is not None
    sig = evaluate_baseline(feat, allow_short=True)
    assert sig.action == "HOLD"

    r = SymbolRules(min_qty=0.01, step_size=0.01, min_notional=5.0)

    class NeutralCol(PublicBinanceV2Collector):
        def fetch_market_quotes(self, min_vol_usdt=5_000_000.0, max_spread_pct=0.005, now_utc=None):
            ts = now if now_utc is None else now_utc
            sq = {f"N{i}USDT": MarketQuote("SPOT", f"N{i}USDT", 10.0, 10.0, 10.0, 10.0, 1e7, 0.0, 0.0, r, ts) for i in range(10)}
            return sq, {}

        def fetch_klines_batch(self, items, interval="15m", limit=60, max_workers=6):
            return {sym: df_flat for _, sym in items}

    runner = NeuralLabV2Runner(v2_dir=tmp_path, collector=NeutralCol(), now_utc=now)
    hb = runner.run_cycle(now_utc=now)
    assert hb["signals_triggered"] == 0
    assert hb["opened_this_cycle"] == 0
    assert hb["hold_this_cycle"] == 10


def test_universe_100_distinct_stratified_and_route_selection() -> None:
    now = 1_795_000_000
    r = SymbolRules(min_qty=0.01, step_size=0.01, min_notional=5.0)
    col = PublicBinanceV2Collector()

    spot_q = {}
    fut_q = {}
    for i in range(130):
        sym = f"COIN{i}USDT"
        vol = 50_000_000.0 - i * 100_000.0
        chg = float((i % 30) - 15)
        onboard = (now - 10 * 86400) * 1000 if i in (119, 121) else (now - 400 * 86400) * 1000
        if i % 2 == 0:
            spot_q[sym] = MarketQuote("SPOT", sym, 10.0, 10.0, 10.0, 10.0, vol, chg, 0.0, r, now)
        if i % 3 != 0:
            fut_q[sym] = MarketQuote("FUTURES", sym, 10.0, 10.0, 10.0, 10.0, vol, chg, 0.0001, r, now, onboard_date_ms=onboard)

    u = col.build_100_market_universe(spot_q, fut_q, max_symbols=100, now_utc=now)
    assert len(u) == 100
    assert len({x.symbol for x in u}) == 100
    new_syms = {x.symbol for x in u if x.is_verified_new_listing}
    assert new_syms == {"COIN119USDT", "COIN121USDT"}

    # Route selection: SHORT -> FUTURES, LONG with high positive funding -> SPOT
    route_short, _ = select_route("SHORT", "breakout", True, True, True, True, 0.0, 0.0)
    assert route_short == "FUTURES"
    route_long_hi_fund, reason_hi = select_route(
        "LONG", "breakout", True, True, True, True, 0.0, 0.0, funding_rate=0.001
    )
    assert route_long_hi_fund == "SPOT" and "funding" in reason_hi


def test_legacy_ledger_state_migration(tmp_path: Path) -> None:
    import json
    import sqlite3

    db_path = tmp_path / "v2_journal.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE events (event_id TEXT PRIMARY KEY, ts_utc INTEGER NOT NULL, "
        "event_type TEXT NOT NULL, ledger TEXT NOT NULL, symbol TEXT NOT NULL, payload_json TEXT NOT NULL)"
    )
    conn.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
    conn.execute("CREATE TABLE ledger_state (id INTEGER PRIMARY KEY, ts_utc INTEGER NOT NULL, state_json TEXT NOT NULL)")
    conn.execute("INSERT INTO meta VALUES ('start_utc', '1794934070'), ('end_utc', '1795193270')")
    conn.execute(
        "INSERT INTO events VALUES ('hb_1', 1794934446, 'HEARTBEAT', 'SYSTEM', 'ALL', '{}')"
    )
    legacy = {
        "spot": {
            "cash_usdt": 2449.95,
            "daily_start_equity": 2500.0,
            "trading_day": "2026-11-17",
            "daily_halted": False,
            "total_fees": 0.05,
            "total_funding": 0.0,
            "positions": {
                "BNBUSDT": {
                    "symbol": "BNBUSDT",
                    "ledger": "SPOT",
                    "side": "LONG",
                    "qty": 0.1,
                    "entry_price": 500.0,
                    "filled_notional": 50.0,
                    "stop_loss": 490.0,
                    "take_profit": 520.0,
                    "leverage": 1.0,
                    "margin_used": 50.0,
                    "liquidation_price": 0.0,
                    "opened_at_utc": 1794934070,
                    "strategy": "mean_reversion",
                    "category": "major",
                    "unrealized_pnl": 0.0,
                    "funding_paid": 0.0,
                }
            },
            "closed_trades": [],
        },
        "futures": {
            "cash_usdt": 2500.0,
            "daily_start_equity": 2500.0,
            "trading_day": "2026-11-17",
            "daily_halted": False,
            "total_fees": 0.0,
            "total_funding": 0.0,
            "positions": {},
            "closed_trades": [],
        },
    }
    conn.execute("INSERT INTO ledger_state VALUES (1, 1794934446, ?)", (json.dumps(legacy),))
    conn.commit()
    conn.close()

    runner = NeuralLabV2Runner(v2_dir=tmp_path)
    assert runner.restored_from_checkpoint is True
    assert runner.start_utc == 1794934070
    assert runner.end_utc == 1795193270
    assert "BNBUSDT" in runner.spot_ledger.positions
    assert runner.spot_ledger.equity() == pytest.approx(2499.95, abs=1e-9)
    assert runner.spot_ledger.equity() == pytest.approx(
        runner.spot_ledger.starting_capital + runner.spot_ledger.realized_pnl + runner.spot_ledger.unrealized_pnl(),
        abs=1e-9,
    )
