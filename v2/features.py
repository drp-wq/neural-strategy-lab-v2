"""Modular technical and volatility feature extraction for V2."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class FeatureSet:
    price: float
    ret_1: float
    realized_vol: float
    atr: float
    vol_zscore: float
    ema_fast: float
    ema_slow: float
    rsi: float
    bb_upper: float
    bb_lower: float
    bb_width: float
    donchian_high: float
    donchian_low: float
    regime: str  # "trending" | "ranging" | "high-volatility"
    last_candle_close_ms: int


def extract_features(df: pd.DataFrame) -> FeatureSet | None:
    """Compute technical & volatility features from OHLCV DataFrame."""
    if df is None or len(df) < 35:
        return None

    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    vol = df["volume"].astype(float)
    if close.isna().any() or high.isna().any() or low.isna().any() or float(close.iloc[-1]) <= 0:
        return None

    rets = close.pct_change().dropna()
    ret_1 = float(rets.iloc[-1]) if len(rets) else 0.0
    realized_vol = float(rets.tail(20).std(ddof=0)) if len(rets) >= 20 else 0.0

    prev_c = close.shift(1)
    tr = pd.concat([high - low, (high - prev_c).abs(), (low - prev_c).abs()], axis=1).max(axis=1)
    atr_s = tr.ewm(alpha=1.0 / 14.0, min_periods=14, adjust=False).mean()
    atr_val = float(atr_s.iloc[-1])

    v_mean = float(vol.tail(20).mean())
    v_std = float(vol.tail(20).std(ddof=0))
    vol_z = (float(vol.iloc[-1]) - v_mean) / v_std if v_std > 0 else 0.0

    ema_fast = float(close.ewm(span=12, adjust=False).mean().iloc[-1])
    ema_slow = float(close.ewm(span=26, adjust=False).mean().iloc[-1])

    delta = close.diff()
    gain = delta.clip(lower=0.0).ewm(alpha=1.0 / 14.0, min_periods=14, adjust=False).mean()
    loss = (-delta.clip(upper=0.0)).ewm(alpha=1.0 / 14.0, min_periods=14, adjust=False).mean()
    gain_last = float(gain.iloc[-1])
    loss_last = float(loss.iloc[-1])
    if gain_last == 0.0 and loss_last == 0.0:
        rsi_val = 50.0
    elif loss_last == 0.0:
        rsi_val = 100.0
    else:
        rs = gain_last / loss_last
        rsi_val = 100.0 - (100.0 / (1.0 + rs))

    sma20 = float(close.tail(20).mean())
    std20 = float(close.tail(20).std(ddof=0))
    bb_upper = sma20 + 2.0 * std20
    bb_lower = sma20 - 2.0 * std20
    bb_width = (bb_upper - bb_lower) / sma20 if sma20 > 0 else 0.0

    donchian_high = float(high.iloc[-21:-1].max())
    donchian_low = float(low.iloc[-21:-1].min())

    last_px = float(close.iloc[-1])
    if bb_width >= 0.06 or realized_vol >= 0.02:
        regime = "high-volatility"
    elif abs(ema_fast - ema_slow) / last_px >= 0.003:
        regime = "trending"
    else:
        regime = "ranging"

    last_close_ms = int(df["close_time"].iloc[-1]) if "close_time" in df.columns else 0

    return FeatureSet(
        price=last_px,
        ret_1=ret_1,
        realized_vol=realized_vol,
        atr=atr_val,
        vol_zscore=vol_z,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        rsi=rsi_val,
        bb_upper=bb_upper,
        bb_lower=bb_lower,
        bb_width=bb_width,
        donchian_high=donchian_high,
        donchian_low=donchian_low,
        regime=regime,
        last_candle_close_ms=last_close_ms,
    )
