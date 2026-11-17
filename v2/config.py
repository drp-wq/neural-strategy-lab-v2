"""Versioned configuration constants for Neural Strategy Lab V2."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class V2Config:
    version: str = "2.0.0"
    mode: str = "PAPER_ONLY"
    duration_sec: int = 72 * 3600
    poll_seconds: int = 60

    # Capital & exposure parameters (USER CONFIRMED)
    target_notional_usdt: float = 50.0
    notional_min_tolerance_usdt: float = 45.0
    notional_max_tolerance_usdt: float = 55.0
    max_concurrent_positions: int = 100
    global_max_notional_usdt: float = 5000.0

    # Explicit virtual capital allocations (total = 5000 USDT)
    spot_starting_capital_usdt: float = 2500.0
    futures_starting_capital_usdt: float = 2500.0

    # Risk & leverage limits
    futures_max_leverage: float = 2.0
    futures_default_leverage: float = 2.0
    futures_maintenance_margin_rate: float = 0.005
    max_risk_per_trade_pct_of_ledger: float = 0.005  # 0.5% of $2500 = $12.50 max SL risk cap
    daily_dd_halt_pct: float = 0.05  # 5% per-ledger daily drawdown halt

    # Execution & fees
    spot_fee_rate: float = 0.001  # 10 bps
    futures_fee_rate: float = 0.0004  # 4 bps
    slippage_bps: float = 5.0

    # Market universe & data freshness
    universe_size: int = 100
    min_quote_volume_24h_usdt: float = 5_000_000.0
    max_spread_pct: float = 0.005
    stale_ticker_sec: int = 120
    stale_candle_sec: int = 1800  # 30m max age for 15m candle close
    exchange_info_ttl_sec: int = 6 * 3600
    new_listing_window_days: int = 90

    # Rate-limiting & polling
    kline_interval: str = "15m"
    kline_limit: int = 60
    kline_workers: int = 6
    requests_per_second: float = 12.0
    spot_weight_soft_cap: int = 4200  # Binance Spot limit 6000/min
    futures_weight_soft_cap: int = 1600  # Binance Futures limit 2400/min

    def to_dict(self) -> dict[str, object]:
        return asdict(self)
