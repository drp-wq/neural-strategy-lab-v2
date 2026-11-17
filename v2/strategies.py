"""Deterministic rule-based baseline strategies and market route selection for V2.

NOTE: NEURAL_MODEL_STATUS remains strictly UNTRAINED unless a genuine
time-split trained model artifact is loaded and verified.
"""

from __future__ import annotations

from dataclasses import dataclass

from v2.features import FeatureSet

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


def evaluate_baseline(feat: FeatureSet, allow_short: bool | str = True) -> StrategySignal:
    """Evaluate deterministic baseline rules; returns HOLD when no setup triggers."""
    can_short = allow_short.upper() != "SPOT" if isinstance(allow_short, str) else bool(allow_short)
    px = feat.price
    sl_dist = max(feat.atr * 2.0, px * 0.005)
    # Clamp SL distance to sensible [0.5%, 8%] range for $50 notional risk control
    sl_dist = min(sl_dist, px * 0.08)
    tp_dist = sl_dist * 1.8

    if feat.regime == "high-volatility":
        if px > feat.donchian_high and feat.vol_zscore >= 1.0:
            return StrategySignal(
                "LONG",
                "breakout",
                0.75,
                px - sl_dist,
                px + tp_dist,
                "high_vol_donchian_breakout_long",
            )
        if can_short and px < feat.donchian_low and feat.vol_zscore >= 1.0:
            return StrategySignal(
                "SHORT",
                "breakout",
                0.75,
                px + sl_dist,
                px - tp_dist,
                "high_vol_donchian_breakout_short",
            )
        return StrategySignal("HOLD", "breakout", 0.0, 0.0, 0.0, "high_vol_no_confirmed_breakout")

    if feat.regime == "trending":
        if feat.ema_fast > feat.ema_slow and feat.rsi <= 55.0:
            return StrategySignal(
                "LONG",
                "trend_pullback",
                0.70,
                px - sl_dist,
                px + tp_dist,
                "ema_uptrend_pullback",
            )
        if can_short and feat.ema_fast < feat.ema_slow and feat.rsi >= 45.0:
            return StrategySignal(
                "SHORT",
                "trend_pullback",
                0.70,
                px + sl_dist,
                px - tp_dist,
                "ema_downtrend_pullback",
            )
        return StrategySignal("HOLD", "trend_pullback", 0.0, 0.0, 0.0, "trending_no_pullback")

    # Ranging regime: mean reversion (require non-zero band width)
    if feat.bb_width > 1e-6:
        if px <= feat.bb_lower and feat.rsi <= 35.0:
            return StrategySignal(
                "LONG",
                "mean_reversion",
                0.65,
                px - sl_dist,
                px + tp_dist,
                "ranging_bb_lower_rsi_oversold",
            )
        if can_short and px >= feat.bb_upper and feat.rsi >= 65.0:
            return StrategySignal(
                "SHORT",
                "mean_reversion",
                0.65,
                px + sl_dist,
                px - tp_dist,
                "ranging_bb_upper_rsi_overbought",
            )
    return StrategySignal("HOLD", "mean_reversion", 0.0, 0.0, 0.0, "ranging_neutral")


def select_route(
    side: str,
    strategy: str,
    spot_eligible: bool,
    futures_eligible: bool,
    spot_can_open: bool,
    futures_can_open: bool,
    spot_exposure: float,
    futures_exposure: float,
    funding_rate: float = 0.0,
) -> tuple[str | None, str]:
    """Independently choose SPOT or FUTURES route per symbol without duplicate exposure."""
    if side == "SHORT":
        if futures_eligible and futures_can_open:
            return "FUTURES", "short_requires_futures"
        if not futures_eligible:
            return None, "short_futures_ineligible"
        return None, "short_futures_unavailable"

    # LONG signal: evaluate availability, funding, strategy fit, and ledger balance
    if spot_eligible and not futures_eligible:
        return ("SPOT", "spot_only_eligible") if spot_can_open else (None, "spot_only_ledger_blocked")
    if futures_eligible and not spot_eligible:
        return ("FUTURES", "futures_only_eligible") if futures_can_open else (None, "futures_only_ledger_blocked")
    if not spot_eligible and not futures_eligible:
        return None, "neither_market_eligible"

    # Both Spot and Futures are eligible
    if not spot_can_open and not futures_can_open:
        return None, "both_ledgers_blocked"
    if not spot_can_open:
        return "FUTURES", "spot_blocked_fallback_futures"
    if not futures_can_open:
        return "SPOT", "futures_blocked_fallback_spot"

    # Avoid paying high positive funding on LONG when Spot is available
    if funding_rate > 0.0005:
        return "SPOT", "high_positive_funding_prefer_spot"

    # Trend holds prefer Spot (zero funding drag) unless Spot is heavier than Futures
    if strategy == "trend_pullback" and spot_exposure <= futures_exposure + 200.0:
        return "SPOT", "trend_long_prefer_spot"

    # Breakout / mean_reversion or balancing exposure across ledgers
    if spot_exposure <= futures_exposure:
        return "SPOT", "balance_exposure_spot"
    return "FUTURES", "balance_exposure_futures"
