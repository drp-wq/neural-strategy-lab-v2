"""Modified Kronos-inspired probabilistic prediction engine for V2."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from v2.features import FeatureSet, extract_features


@dataclass(frozen=True)
class KronosForecast:
    """Multi-horizon probabilistic forecast and recommended next step."""

    symbol: str
    horizon_bars: int
    predicted_return: float
    up_prob: float
    down_prob: float
    predicted_volatility: float
    regime: str
    confidence: float
    recommended_step: str  # "LONG" | "SHORT" | "HOLD"
    model_status: str  # "UNTRAINED" | "TRAINED"
    reason: str


class ModifiedKronosPredictor:
    """Chronological time-split predictor with online loss-lesson calibration."""

    def __init__(self, horizon_bars: int = 4) -> None:
        self.horizon_bars = horizon_bars
        self.model_status: str = "UNTRAINED"
        self.weights: np.ndarray | None = None
        self.bias: float = 0.0
        self.val_directional_acc: float = 0.0
        self.regime_confidence_mult: dict[str, float] = {
            "trending": 1.0,
            "ranging": 1.0,
            "high-volatility": 1.0,
        }
        self.min_prob_threshold: float = 0.58

    @staticmethod
    def _featurize_row(feat: FeatureSet) -> np.ndarray:
        ema_diff = (feat.ema_fast - feat.ema_slow) / feat.price if feat.price > 0 else 0.0
        rsi_norm = (feat.rsi - 50.0) / 50.0
        bb_pos = (
            (feat.price - feat.bb_lower) / max(feat.bb_upper - feat.bb_lower, 1e-9) - 0.5
        )
        return np.array(
            [feat.ret_1, ema_diff, rsi_norm, feat.vol_zscore * 0.1, bb_pos, feat.realized_vol],
            dtype=float,
        )

    def fit_time_split(self, df: pd.DataFrame, train_ratio: float = 0.7) -> dict[str, Any]:
        """Train on chronological split (no lookahead) and validate out-of-sample."""
        if len(df) < 80:
            return {"trained": False, "reason": "insufficient_history"}

        xs: list[np.ndarray] = []
        ys: list[float] = []
        close = df["close"].astype(float).reset_index(drop=True)
        for i in range(40, len(df) - self.horizon_bars):
            sub = df.iloc[: i + 1]
            feat = extract_features(sub)
            if feat is None or close.iloc[i] <= 0:
                continue
            fwd_ret = (close.iloc[i + self.horizon_bars] - close.iloc[i]) / close.iloc[i]
            xs.append(self._featurize_row(feat))
            ys.append(float(fwd_ret))

        if len(xs) < 30:
            return {"trained": False, "reason": "insufficient_samples"}

        X = np.vstack(xs)
        y = np.array(ys, dtype=float)
        split = int(len(X) * train_ratio)
        X_train, y_train = X[:split], y[:split]
        X_val, y_val = X[split:], y[split:]

        # Ridge regression: (X^T X + lambda I)^-1 X^T y
        X_tr_b = np.column_stack([X_train, np.ones(len(X_train))])
        reg = 1e-2 * np.eye(X_tr_b.shape[1])
        reg[-1, -1] = 0.0
        theta = np.linalg.solve(X_tr_b.T @ X_tr_b + reg, X_tr_b.T @ y_train)
        self.weights = theta[:-1]
        self.bias = float(theta[-1])

        val_pred = X_val @ self.weights + self.bias
        acc = float(np.mean((val_pred > 0) == (y_val > 0))) if len(y_val) > 0 else 0.0
        self.val_directional_acc = acc
        self.model_status = "TRAINED"
        return {
            "trained": True,
            "train_samples": len(X_train),
            "val_samples": len(X_val),
            "val_directional_acc": acc,
        }

    def apply_lesson_calibration(self, lessons: dict[str, Any]) -> None:
        """Upgrade predictor thresholds & regime multipliers from post-trade loss lessons."""
        regime_penalties = lessons.get("regime_penalties", {})
        for reg, mult in regime_penalties.items():
            if reg in self.regime_confidence_mult:
                self.regime_confidence_mult[reg] = max(0.3, min(1.2, float(mult)))
        if lessons.get("raise_threshold", False):
            self.min_prob_threshold = min(0.72, self.min_prob_threshold + 0.04)

    def predict(self, symbol: str, feat: FeatureSet, allow_short: bool = True) -> KronosForecast:
        """Generate probabilistic forecast and recommended next step."""
        x = self._featurize_row(feat)
        if self.model_status == "TRAINED" and self.weights is not None:
            pred_ret = float(x @ self.weights + self.bias)
        else:
            # Deterministic baseline score when UNTRAINED
            pred_ret = float(0.4 * x[1] - 0.2 * x[2] + 0.2 * x[3])

        vol = max(feat.realized_vol, 1e-4)
        z_score = max(-4.0, min(4.0, pred_ret / vol))
        up_prob = float(1.0 / (1.0 + np.exp(-z_score)))
        down_prob = 1.0 - up_prob

        mult = self.regime_confidence_mult.get(feat.regime, 1.0)
        raw_conf = abs(up_prob - 0.5) * 2.0
        conf = max(0.0, min(1.0, raw_conf * mult))

        if up_prob >= self.min_prob_threshold and conf >= 0.15:
            step = "LONG"
            reason = f"kronos_up_prob_{up_prob:.2f}"
        elif allow_short and down_prob >= self.min_prob_threshold and conf >= 0.15:
            step = "SHORT"
            reason = f"kronos_down_prob_{down_prob:.2f}"
        else:
            step = "HOLD"
            reason = f"kronos_neutral_prob_{up_prob:.2f}"

        return KronosForecast(
            symbol=symbol,
            horizon_bars=self.horizon_bars,
            predicted_return=pred_ret,
            up_prob=up_prob,
            down_prob=down_prob,
            predicted_volatility=vol,
            regime=feat.regime,
            confidence=conf,
            recommended_step=step,
            model_status=self.model_status,
            reason=reason,
        )
