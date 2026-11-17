"""Tests for Modified Kronos Prediction Engine & Loss-Lesson Master Strategy."""

from __future__ import annotations

import numpy as np
import pandas as pd

from v2.features import extract_features
from v2.kronos_engine import ModifiedKronosPredictor
from v2.master_strategy import LossLessonAnalyzer, MasterStrategy


def _synthetic_ohlcv(n: int = 150, trend: float = 0.002) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    rets = trend + rng.normal(0.0, 0.005, size=n)
    close = 100.0 * np.exp(np.cumsum(rets))
    high = close * (1.0 + rng.uniform(0.001, 0.006, size=n))
    low = close * (1.0 - rng.uniform(0.001, 0.006, size=n))
    vol = 1000.0 + rng.uniform(0.0, 500.0, size=n)
    return pd.DataFrame(
        {
            "open_time": np.arange(n) * 900_000,
            "open": close,
            "high": high,
            "low": low,
            "close": close,
            "volume": vol,
            "close_time": np.arange(n) * 900_000 + 899_999,
        }
    )


def test_kronos_untrained_vs_time_split_trained() -> None:
    df = _synthetic_ohlcv(150, trend=0.002)
    feat = extract_features(df)
    assert feat is not None

    pred = ModifiedKronosPredictor(horizon_bars=4)
    assert pred.model_status == "UNTRAINED"
    f1 = pred.predict("BTCUSDT", feat, allow_short=True)
    assert f1.model_status == "UNTRAINED"
    assert 0.0 <= f1.up_prob <= 1.0

    metrics = pred.fit_time_split(df, train_ratio=0.7)
    assert metrics["trained"] is True
    assert pred.model_status == "TRAINED"
    f2 = pred.predict("BTCUSDT", feat, allow_short=True)
    assert f2.model_status == "TRAINED"
    assert f2.recommended_step in ("LONG", "SHORT", "HOLD")


def test_loss_lesson_analyzer_and_master_strategy_upgrade() -> None:
    closed_trades = [
        {"strategy": "breakout", "net_pnl": -2.5, "reason": "stop_loss"},
        {"strategy": "breakout", "net_pnl": -1.8, "reason": "stop_loss"},
        {"strategy": "breakout", "net_pnl": -3.0, "reason": "stop_loss"},
        {"strategy": "trend_pullback", "net_pnl": 4.2, "reason": "take_profit"},
        {"strategy": "trend_pullback", "net_pnl": -1.0, "reason": "stop_loss"},
    ]
    lessons = LossLessonAnalyzer.analyze(closed_trades)
    assert "breakout" in lessons["blocked_strategies"]
    assert lessons["sl_multiplier"] == 2.4
    assert lessons["raise_threshold"] is True

    master = MasterStrategy()
    master.upgrade_from_trades(closed_trades)
    assert master.predictor.min_prob_threshold > 0.58

    df = _synthetic_ohlcv(150, trend=0.002)
    feat = extract_features(df)
    assert feat is not None
    sig, forecast = master.evaluate("ETHUSDT", feat, "FUTURES")
    assert sig.action in ("LONG", "SHORT", "HOLD")
    assert forecast.symbol == "ETHUSDT"
