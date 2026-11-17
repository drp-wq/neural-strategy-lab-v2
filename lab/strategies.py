"""Deterministic rule-based baseline strategies (neural_model = UNTRAINED)."""

from __future__ import annotations

from dataclasses import dataclass

from lab.features import FeatureSet

NEURAL_MODEL_STATUS = "UNTRAINED"


@dataclass(frozen=True)
class StrategySignal:
    action: str  # "LONG" | "SHORT" | "HOLD"
    strategy: str
    confidence: float
    stop_loss: float
    take_profit: float
    reason: str
    neural_model: str = NEURAL_MODEL_STATUS


def evaluate_baseline(feat: FeatureSet, ledger_type: str) -> StrategySignal:
    """Route by regime using deterministic baseline rules; Spot is long-only."""
    px = feat.price
    sl_dist = max(feat.atr * 2.0, px * 0.005)
    tp_dist = sl_dist * 1.8

    # Volatility squeeze / breakout in high-volatility
    if feat.regime == "high-volatility":
        if px > feat.donchian_high and feat.vol_zscore >= 1.0:
            return StrategySignal(
                "LONG", "breakout", 0.75, px - sl_dist, px + tp_dist,
                "high_vol_donchian_breakout_long"
            )
        if ledger_type == "FUTURES" and px < feat.donchian_low and feat.vol_zscore >= 1.0:
            return StrategySignal(
                "SHORT", "breakout", 0.75, px + sl_dist, px - tp_dist,
                "high_vol_donchian_breakout_short"
            )
        return StrategySignal("HOLD", "flat", 0.0, 0.0, 0.0, "high_vol_no_confirmed_breakout")

    # Trending: trend pullback
    if feat.regime == "trending":
        if feat.ema_fast > feat.ema_slow and feat.rsi <= 55.0:
            return StrategySignal(
                "LONG", "trend_pullback", 0.70, px - sl_dist, px + tp_dist,
                "ema_uptrend_pullback"
            )
        if ledger_type == "FUTURES" and feat.ema_fast < feat.ema_slow and feat.rsi >= 45.0:
            return StrategySignal(
                "SHORT", "trend_pullback", 0.70, px + sl_dist, px - tp_dist,
                "ema_downtrend_pullback"
            )
        return StrategySignal("HOLD", "trend_pullback", 0.0, 0.0, 0.0, "trending_no_pullback")

    # Ranging: mean reversion
    if px <= feat.bb_lower and feat.rsi <= 35.0:
        return StrategySignal(
            "LONG", "mean_reversion", 0.65, px - sl_dist, px + tp_dist,
            "ranging_bb_lower_rsi_oversold"
        )
    if ledger_type == "FUTURES" and px >= feat.bb_upper and feat.rsi >= 65.0:
        return StrategySignal(
            "SHORT", "mean_reversion", 0.65, px + sl_dist, px - tp_dist,
            "ranging_bb_upper_rsi_overbought"
        )
    return StrategySignal("HOLD", "mean_reversion", 0.0, 0.0, 0.0, "ranging_neutral")
