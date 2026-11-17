"""Post-trade loss-lesson analyzer and Master Strategy combining baseline + Modified Kronos."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from v2.features import FeatureSet
from v2.kronos_engine import KronosForecast, ModifiedKronosPredictor
from v2.strategies import StrategySignal, evaluate_baseline


class LossLessonAnalyzer:
    """Extracts actionable lessons from closed paper trades, focusing on losing trades."""

    @staticmethod
    def analyze(closed_trades: list[dict[str, Any]]) -> dict[str, Any]:
        if not closed_trades:
            return {
                "total_trades": 0,
                "losing_trades": 0,
                "win_rate": 0.0,
                "blocked_strategies": [],
                "sl_multiplier": 2.0,
                "tp_multiplier": 3.6,
                "raise_threshold": False,
                "regime_penalties": {},
                "summary": "no_closed_trades_yet",
            }

        total = len(closed_trades)
        losses = [t for t in closed_trades if float(t.get("net_pnl", 0.0)) < 0]
        wins = total - len(losses)
        win_rate = wins / total if total > 0 else 0.0

        by_strat: dict[str, list[float]] = defaultdict(list)
        sl_losses = 0
        for t in closed_trades:
            strat = str(t.get("strategy") or "unknown")
            pnl = float(t.get("net_pnl", 0.0))
            by_strat[strat].append(pnl)
            if pnl < 0 and str(t.get("reason", "")) == "stop_loss":
                sl_losses += 1

        blocked_strategies: list[str] = []
        for strat, pnls in by_strat.items():
            if len(pnls) >= 3 and sum(pnls) < 0 and (sum(1 for p in pnls if p > 0) / len(pnls)) < 0.35:
                blocked_strategies.append(strat)

        # If >60% of losses are stop-outs, widen SL slightly to avoid noise whipsaws
        sl_ratio = (sl_losses / len(losses)) if losses else 0.0
        sl_mult = 2.4 if sl_ratio > 0.60 else 2.0
        tp_mult = sl_mult * 1.8

        return {
            "total_trades": total,
            "losing_trades": len(losses),
            "win_rate": win_rate,
            "blocked_strategies": blocked_strategies,
            "sl_multiplier": sl_mult,
            "tp_multiplier": tp_mult,
            "raise_threshold": total >= 5 and win_rate < 0.45,
            "regime_penalties": {},
            "summary": f"analyzed_{total}_trades_win_rate_{win_rate:.2f}",
        }


class MasterStrategy:
    """Upgraded Master Strategy fusing baseline rules, Modified Kronos, and loss lessons."""

    def __init__(self, predictor: ModifiedKronosPredictor | None = None) -> None:
        self.predictor = predictor or ModifiedKronosPredictor()
        self.lessons: dict[str, Any] = LossLessonAnalyzer.analyze([])

    def upgrade_from_trades(self, closed_trades: list[dict[str, Any]]) -> dict[str, Any]:
        """Learn from closed paper trades and upgrade predictor + strategy parameters."""
        self.lessons = LossLessonAnalyzer.analyze(closed_trades)
        self.predictor.apply_lesson_calibration(self.lessons)
        return self.lessons

    def evaluate(
        self, symbol: str, feat: FeatureSet, ledger_type: str
    ) -> tuple[StrategySignal, KronosForecast]:
        """Produce final action, SL/TP, and Kronos forecast."""
        allow_short = ledger_type == "FUTURES"
        forecast = self.predictor.predict(symbol, feat, allow_short=allow_short)
        base_sig = evaluate_baseline(feat, ledger_type)

        blocked = set(self.lessons.get("blocked_strategies", []))
        if base_sig.strategy in blocked:
            base_sig = StrategySignal(
                "HOLD", base_sig.strategy, 0.0, 0.0, 0.0,
                f"blocked_by_loss_lesson:{base_sig.strategy}",
                neural_model=forecast.model_status,
            )

        px = feat.price
        sl_mult = float(self.lessons.get("sl_multiplier", 2.0))
        tp_mult = float(self.lessons.get("tp_multiplier", 3.6))
        sl_dist = max(feat.atr * sl_mult, px * 0.005)
        tp_dist = max(feat.atr * tp_mult, sl_dist * 1.8)

        # Consensus: if Kronos is TRAINED and has high confidence, or agrees with baseline
        if base_sig.action != "HOLD" and (
            forecast.recommended_step == base_sig.action or forecast.recommended_step == "HOLD"
        ):
            sl = px - sl_dist if base_sig.action == "LONG" else px + sl_dist
            tp = px + tp_dist if base_sig.action == "LONG" else px - tp_dist
            return (
                StrategySignal(
                    action=base_sig.action,
                    strategy=f"master_{base_sig.strategy}",
                    confidence=max(base_sig.confidence, forecast.confidence),
                    stop_loss=sl,
                    take_profit=tp,
                    reason=f"{base_sig.reason}|{forecast.reason}",
                    neural_model=forecast.model_status,
                ),
                forecast,
            )

        if forecast.model_status == "TRAINED" and forecast.recommended_step != "HOLD":
            action = forecast.recommended_step
            sl = px - sl_dist if action == "LONG" else px + sl_dist
            tp = px + tp_dist if action == "LONG" else px - tp_dist
            return (
                StrategySignal(
                    action=action,
                    strategy="master_kronos",
                    confidence=forecast.confidence,
                    stop_loss=sl,
                    take_profit=tp,
                    reason=forecast.reason,
                    neural_model=forecast.model_status,
                ),
                forecast,
            )

        return (
            StrategySignal(
                "HOLD", "master_hold", 0.0, 0.0, 0.0,
                f"no_consensus:{base_sig.reason}|{forecast.reason}",
                neural_model=forecast.model_status,
            ),
            forecast,
        )
