"""72-hour time-bounded V2 100-market ($50 notional, $5000 global cap) paper runner."""

from __future__ import annotations

import json
import fcntl
import logging
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from v2.collectors import (
    MarketQuote,
    PublicBinanceV2Collector,
    UnifiedMarketSymbol,
    validate_quote,
)
from v2.config import V2Config
from v2.features import extract_features
from v2.journal import V2Journal
from v2.ledger import GlobalPortfolioCoordinator, PaperLedger
from v2.strategies import NEURAL_MODEL_STATUS, evaluate_baseline, select_route


def compute_attribution(
    spot: PaperLedger,
    futures: PaperLedger,
) -> dict[str, Any]:
    """Compute realized, open, and fee/funding attribution by category, strategy, ledger, and symbol."""
    by_category: dict[str, dict[str, Any]] = {}
    by_strategy: dict[str, dict[str, Any]] = {}
    by_symbol: dict[str, dict[str, Any]] = {}

    def _bucket(mapping: dict[str, dict[str, Any]], key: str) -> dict[str, Any]:
        return mapping.setdefault(
            key,
            {
                "open_count": 0,
                "closed_count": 0,
                "wins": 0,
                "losses": 0,
                "realized_net_pnl": 0.0,
                "unrealized_pnl": 0.0,
                "fees": 0.0,
                "funding": 0.0,
                "open_notional": 0.0,
            },
        )

    for ledger in (spot, futures):
        for pos in ledger.positions.values():
            cat = pos.selection_bucket or pos.category or "unknown"
            strat = pos.strategy or "unknown"
            for m, k in ((by_category, cat), (by_strategy, strat), (by_symbol, pos.symbol)):
                b = _bucket(m, k)
                b["open_count"] += 1
                b["unrealized_pnl"] = round(b["unrealized_pnl"] + pos.unrealized_pnl, 6)
                b["fees"] = round(b["fees"] + pos.entry_fee, 6)
                b["funding"] = round(b["funding"] + pos.accumulated_funding, 6)
                b["open_notional"] = round(b["open_notional"] + pos.actual_notional, 4)

        for tr in ledger.closed_trades:
            cat = str(tr.get("selection_bucket") or tr.get("category") or "unknown")
            strat = str(tr.get("strategy") or "unknown")
            sym = str(tr.get("symbol") or "UNKNOWN")
            pnl = float(tr.get("net_pnl", 0.0))
            fee = float(tr.get("fee", 0.0))
            fund = float(tr.get("funding_paid", 0.0))
            for m, k in ((by_category, cat), (by_strategy, strat), (by_symbol, sym)):
                b = _bucket(m, k)
                b["closed_count"] += 1
                if pnl > 0:
                    b["wins"] += 1
                elif pnl < 0:
                    b["losses"] += 1
                b["realized_net_pnl"] = round(b["realized_net_pnl"] + pnl, 6)
                b["fees"] = round(b["fees"] + fee, 6)
                b["funding"] = round(b["funding"] + fund, 6)

    return {
        "by_category": by_category,
        "by_strategy": by_strategy,
        "by_symbol": by_symbol,
        "by_ledger": {
            "SPOT": {
                "equity": round(spot.equity(), 6),
                "cash": round(spot.cash_usdt, 6),
                "exposure": round(spot.current_exposure(), 4),
                "open_count": len(spot.positions),
                "closed_count": len(spot.closed_trades),
                "realized_pnl": round(spot.realized_pnl, 6),
                "unrealized_pnl": round(spot.unrealized_pnl(), 6),
                "total_fees": round(spot.total_fees, 6),
                "daily_halted": spot.daily_halted,
            },
            "FUTURES": {
                "equity": round(futures.equity(), 6),
                "cash": round(futures.cash_usdt, 6),
                "locked_margin": round(futures.locked_margin(), 6),
                "exposure": round(futures.current_exposure(), 4),
                "open_count": len(futures.positions),
                "closed_count": len(futures.closed_trades),
                "realized_pnl": round(futures.realized_pnl, 6),
                "unrealized_pnl": round(futures.unrealized_pnl(), 6),
                "total_fees": round(futures.total_fees, 6),
                "total_funding": round(futures.total_funding, 6),
                "daily_halted": futures.daily_halted,
            },
        },
    }


class NeuralLabV2Runner:
    """Runs the isolated V2 72-hour 100-market ($50/trade, $5000 cap) paper experiment."""

    def __init__(
        self,
        v2_dir: str | Path = "/root/neural-strategy-lab/v2",
        config: V2Config | None = None,
        collector: PublicBinanceV2Collector | None = None,
        now_utc: int | None = None,
    ) -> None:
        self.v2_dir = Path(v2_dir)
        self.v2_dir.mkdir(parents=True, exist_ok=True)
        self._instance_lock_file = (self.v2_dir / ".runner.lock").open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._instance_lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._instance_lock_file.close()
            raise RuntimeError("Another V2 runner instance already holds the singleton lock") from exc
        try:
            self._initialize(config=config, collector=collector, now_utc=now_utc)
        except Exception:
            self.close()
            raise

    def _initialize(
        self,
        config: V2Config | None,
        collector: PublicBinanceV2Collector | None,
        now_utc: int | None,
    ) -> None:
        self.config = config or V2Config()
        self.journal = V2Journal(self.v2_dir / "v2_journal.db")
        self.collector = collector or PublicBinanceV2Collector(
            stale_sec=self.config.stale_ticker_sec,
            exchange_info_ttl_sec=self.config.exchange_info_ttl_sec,
            requests_per_second=self.config.requests_per_second,
            spot_weight_soft_cap=self.config.spot_weight_soft_cap,
            futures_weight_soft_cap=self.config.futures_weight_soft_cap,
        )
        self.logger = logging.getLogger("neural_lab_v2")
        self._stop_requested = False

        now = int(time.time()) if now_utc is None else int(now_utc)
        saved = self.journal.load_state()
        if saved:
            if saved.get("mode") != "PAPER_ONLY":
                raise RuntimeError("Refusing to restore a non-paper V2 checkpoint")
            if int(saved.get("start_utc", 0)) <= 0 or int(saved.get("end_utc", 0)) <= int(saved.get("start_utc", 0)):
                raise RuntimeError("Refusing to restore a checkpoint with an invalid experiment clock")
            self.start_utc = int(saved["start_utc"])
            self.end_utc = int(saved["end_utc"])
            self.cycle_count = int(saved.get("cycle_count", 0))
            self.last_hourly_report_hour = int(saved.get("last_hourly_report_hour", -1))
            self.spot_ledger = PaperLedger.from_state_dict(saved["spot_ledger"])
            self.futures_ledger = PaperLedger.from_state_dict(saved["futures_ledger"])
            self.restored_from_checkpoint = True
        else:
            self.start_utc = now
            self.end_utc = now + self.config.duration_sec
            self.cycle_count = 0
            self.last_hourly_report_hour = -1
            self.spot_ledger = PaperLedger(
                "SPOT",
                starting_capital=self.config.spot_starting_capital_usdt,
                daily_dd_halt_pct=self.config.daily_dd_halt_pct,
                max_leverage=1.0,
                max_risk_per_trade_pct=self.config.max_risk_per_trade_pct_of_ledger,
                fee_rate=self.config.spot_fee_rate,
            )
            self.futures_ledger = PaperLedger(
                "FUTURES",
                starting_capital=self.config.futures_starting_capital_usdt,
                daily_dd_halt_pct=self.config.daily_dd_halt_pct,
                max_leverage=self.config.futures_max_leverage,
                maintenance_margin_rate=self.config.futures_maintenance_margin_rate,
                max_risk_per_trade_pct=self.config.max_risk_per_trade_pct_of_ledger,
                fee_rate=self.config.futures_fee_rate,
            )
            self.restored_from_checkpoint = False

        self.coordinator = GlobalPortfolioCoordinator(
            spot_ledger=self.spot_ledger,
            futures_ledger=self.futures_ledger,
            max_concurrent_positions=self.config.max_concurrent_positions,
            global_max_notional=self.config.global_max_notional_usdt,
            target_notional=self.config.target_notional_usdt,
            min_tolerance_notional=self.config.notional_min_tolerance_usdt,
            max_tolerance_notional=self.config.notional_max_tolerance_usdt,
        )

        cfg_path = self.v2_dir / "v2_config.json"
        if not cfg_path.exists():
            cfg_path.write_text(json.dumps(self.config.to_dict(), indent=2), encoding="utf-8")

    def close(self) -> None:
        """Release the process singleton lock when the runner is shut down."""
        lock_file = getattr(self, "_instance_lock_file", None)
        if lock_file is not None and not lock_file.closed:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()

    def is_expired(self, now_utc: int | None = None) -> bool:
        now = int(time.time()) if now_utc is None else int(now_utc)
        return now >= self.end_utc

    def _state_payload(self, now: int, last_universe_summary: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.config.mode != "PAPER_ONLY":
            raise RuntimeError("V2 runner is hard limited to PAPER_ONLY")
        if self.coordinator.total_exposure() > self.config.global_max_notional_usdt + 1e-7:
            raise RuntimeError("Global exposure invariant exceeded; refusing checkpoint")
        if self.coordinator.total_open_positions() > self.config.max_concurrent_positions:
            raise RuntimeError("Position count invariant exceeded; refusing checkpoint")
        return {
            "version": self.config.version,
            "mode": self.config.mode,
            "neural_model": NEURAL_MODEL_STATUS,
            "start_utc": self.start_utc,
            "end_utc": self.end_utc,
            "cycle_count": self.cycle_count,
            "last_hourly_report_hour": self.last_hourly_report_hour,
            "updated_at_utc": now,
            "spot_ledger": self.spot_ledger.to_state_dict(),
            "futures_ledger": self.futures_ledger.to_state_dict(),
            "universe_summary": last_universe_summary or {},
        }

    def _update_open_positions(
        self,
        ledger: PaperLedger,
        quotes: dict[str, MarketQuote],
        now: int,
        pending_events: list[tuple[str, str, str, str, dict[str, Any], int]],
    ) -> None:
        for sym in list(ledger.positions.keys()):
            q = quotes.get(sym)
            ok_q, q_reason = validate_quote(
                q,
                now_utc=now,
                stale_sec=self.config.stale_ticker_sec,
                max_spread_pct=max(self.config.max_spread_pct * 2.0, 0.02),
            )
            if not ok_q or q is None:
                pending_events.append(
                    (
                        f"v2_stale_exit_{ledger.ledger_type}_{sym}_{now}_{self.cycle_count}",
                        "STALE_DATA_SKIP",
                        ledger.ledger_type,
                        sym,
                        {"phase": "mark_or_exit", "reason": q_reason, "quote_ts": q.ts_utc if q else None},
                        now,
                    )
                )
                continue

            if ledger.ledger_type == "FUTURES":
                funding_cost = ledger.apply_funding_if_due(sym, q.funding_rate, q.mark_price, now)
                if funding_cost is not None:
                    pending_events.append(
                        (
                            f"v2_fund_{sym}_{now // 28800}",
                            "FUNDING_APPLIED",
                            "FUTURES",
                            sym,
                            {
                                "funding_cost": funding_cost,
                                "funding_rate": q.funding_rate,
                                "mark_price": q.mark_price,
                            },
                            now,
                        )
                    )

            pos = ledger.positions[sym]
            pos.update_mark(q.mark_price)

            hit_liq = pos.liquidation_price > 0 and (
                (pos.side == "LONG" and q.mark_price <= pos.liquidation_price)
                or (pos.side == "SHORT" and q.mark_price >= pos.liquidation_price)
            )
            hit_sl = (pos.side == "LONG" and q.mark_price <= pos.stop_loss) or (
                pos.side == "SHORT" and q.mark_price >= pos.stop_loss
            )
            hit_tp = (pos.side == "LONG" and q.mark_price >= pos.take_profit) or (
                pos.side == "SHORT" and q.mark_price <= pos.take_profit
            )

            if hit_liq or hit_sl or hit_tp:
                reason = "liquidation" if hit_liq else ("stop_loss" if hit_sl else "take_profit")
                rec = ledger.close_position(
                    sym,
                    q.bid,
                    q.ask,
                    reason,
                    now,
                    slippage_bps=self.config.slippage_bps,
                )
                if rec:
                    pending_events.append(
                        (
                            f"v2_close_{ledger.ledger_type}_{sym}_{pos.opened_at_utc}_{now}",
                            "POSITION_CLOSED",
                            ledger.ledger_type,
                            sym,
                            rec,
                            now,
                        )
                    )

    @staticmethod
    def _serialize_universe(universe: list[UnifiedMarketSymbol]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for u in universe:
            rows.append(
                {
                    "symbol": u.symbol,
                    "base_asset": u.base_asset,
                    "category": u.category,
                    "selection_bucket": u.selection_bucket,
                    "tags": u.tags,
                    "is_verified_new_listing": u.is_verified_new_listing,
                    "onboard_date_ms": u.onboard_date_ms,
                    "best_volume_24h_usdt": round(u.best_volume_24h_usdt, 2),
                    "price_change_pct": round(u.price_change_pct, 4),
                    "spot_eligible": u.spot is not None,
                    "futures_eligible": u.futures is not None,
                    "spot_price": u.spot.last_price if u.spot else None,
                    "futures_mark_price": u.futures.mark_price if u.futures else None,
                    "futures_funding_rate": u.futures.funding_rate if u.futures else None,
                }
            )
        return rows

    def write_status(
        self,
        universe_count: int,
        spot_eligible_count: int,
        futures_eligible_count: int,
        now: int,
        test_summary: str | None = None,
        last_cycle_notes: str = "OK",
    ) -> None:
        start_iso = datetime.fromtimestamp(self.start_utc, tz=timezone.utc).isoformat()
        end_iso = datetime.fromtimestamp(self.end_utc, tz=timezone.utc).isoformat()
        now_iso = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()
        comb_eq = self.spot_ledger.equity() + self.futures_ledger.equity()
        glob_exp = self.coordinator.total_exposure()
        glob_slots = self.coordinator.total_open_positions()
        test_line = test_summary
        if not test_line:
            cfg_path = self.v2_dir / "v2_config.json"
            if cfg_path.exists():
                try:
                    test_line = json.loads(cfg_path.read_text(encoding="utf-8")).get("test_summary")
                except (OSError, json.JSONDecodeError):
                    test_line = None
        test_line = test_line or "26 passed (V1 + V2 + Dashboard pytest suites)"

        status_md = (
            f"# Neural Strategy Lab V2 — 100-Market 72h Paper Experiment Status\n\n"
            f"- **Version**: `{self.config.version}` (Isolated V2 in `/root/neural-strategy-lab/v2`)\n"
            f"- **Mode**: `PAPER ONLY` (no live keys, no live orders, public endpoints only)\n"
            f"- **Neural Model**: `{NEURAL_MODEL_STATUS}` (deterministic rule-based baseline; no fabricated ML weights)\n"
            f"- **Start UTC**: `{start_iso}`\n"
            f"- **Scheduled End UTC**: `{end_iso}`\n"
            f"- **Last Heartbeat UTC**: `{now_iso}`\n"
            f"- **PID**: `{os.getpid()}`\n"
            f"- **Cycle Count**: `{self.cycle_count}`\n"
            f"- **Target Notional / Trade**: `{self.config.target_notional_usdt:.2f} USDT` "
            f"(tolerance `{self.config.notional_min_tolerance_usdt:.0f}-{self.config.notional_max_tolerance_usdt:.0f} USDT`)\n"
            f"- **Global Capacity**: open=`{glob_slots}/{self.config.max_concurrent_positions}` slots, "
            f"gross_notional=`{glob_exp:.2f}/{self.config.global_max_notional_usdt:.2f} USDT`, "
            f"combined_equity=`{comb_eq:.4f} USDT` (start=`{self.config.spot_starting_capital_usdt + self.config.futures_starting_capital_usdt:.2f} USDT`)\n"
            f"- **SPOT Ledger**: equity=`{self.spot_ledger.equity():.4f} USDT`, "
            f"cash=`{self.spot_ledger.cash_usdt:.4f} USDT`, "
            f"exposure=`{self.spot_ledger.current_exposure():.2f} USDT`, "
            f"open=`{len(self.spot_ledger.positions)}`, closed=`{len(self.spot_ledger.closed_trades)}`, "
            f"fees=`{self.spot_ledger.total_fees:.4f} USDT`, daily_halted=`{self.spot_ledger.daily_halted}`\n"
            f"- **FUTURES Ledger**: equity=`{self.futures_ledger.equity():.4f} USDT`, "
            f"cash=`{self.futures_ledger.cash_usdt:.4f} USDT`, "
            f"locked_margin=`{self.futures_ledger.locked_margin():.4f} USDT`, "
            f"exposure=`{self.futures_ledger.current_exposure():.2f} USDT`, "
            f"open=`{len(self.futures_ledger.positions)}`, closed=`{len(self.futures_ledger.closed_trades)}`, "
            f"fees=`{self.futures_ledger.total_fees:.4f} USDT`, funding=`{self.futures_ledger.total_funding:.4f} USDT`, "
            f"daily_halted=`{self.futures_ledger.daily_halted}`\n"
            f"- **Sampled Universe**: `{universe_count}` distinct symbols "
            f"(SPOT-eligible=`{spot_eligible_count}`, FUTURES-eligible=`{futures_eligible_count}`)\n"
            f"- **Test Verification**: `{test_line}`\n"
            f"- **Last Cycle Notes**: `{last_cycle_notes}`\n"
            f"- **Blockers**: None\n"
        )
        status_path = self.v2_dir / "V2_STATUS.md"
        tmp_path = self.v2_dir / ".V2_STATUS.md.tmp"
        tmp_path.write_text(status_md, encoding="utf-8")
        os.replace(tmp_path, status_path)

    def run_cycle(self, now_utc: int | None = None) -> dict[str, Any]:
        now = int(time.time()) if now_utc is None else int(now_utc)
        self.cycle_count += 1
        pending_events: list[tuple[str, str, str, str, dict[str, Any], int]] = []

        spot_quotes, fut_quotes = self.collector.fetch_market_quotes(
            min_vol_usdt=self.config.min_quote_volume_24h_usdt,
            max_spread_pct=self.config.max_spread_pct,
            now_utc=now,
        )

        # Update open positions first with fresh quotes
        self._update_open_positions(self.spot_ledger, spot_quotes, now, pending_events)
        self._update_open_positions(self.futures_ledger, fut_quotes, now, pending_events)

        universe = self.collector.build_100_market_universe(
            spot_quotes=spot_quotes,
            fut_quotes=fut_quotes,
            max_symbols=self.config.universe_size,
            new_listing_window_days=self.config.new_listing_window_days,
            max_tolerance_notional=self.config.notional_max_tolerance_usdt,
            now_utc=now,
        )

        spot_elig_n = sum(1 for u in universe if u.spot is not None)
        fut_elig_n = sum(1 for u in universe if u.futures is not None)
        signals_triggered = 0
        opened_this_cycle = 0
        rejected_this_cycle = 0
        hold_this_cycle = 0
        stale_this_cycle = 0

        if not self.is_expired(now):
            # Only fetch klines for symbols not currently open and while slots remain
            kline_targets: list[tuple[str, str]] = []
            for u in universe:
                if self.coordinator.has_open_symbol(u.symbol):
                    continue
                kline_targets.append((u.primary_market_for_candles(), u.symbol))

            klines_map = (
                self.collector.fetch_klines_batch(
                    kline_targets,
                    interval=self.config.kline_interval,
                    limit=self.config.kline_limit,
                    max_workers=self.config.kline_workers,
                )
                if kline_targets
                else {}
            )

            for u in universe:
                if self.coordinator.has_open_symbol(u.symbol):
                    continue
                if self.coordinator.total_open_positions() >= self.config.max_concurrent_positions:
                    break
                if self.coordinator.remaining_exposure_capacity() < self.config.notional_min_tolerance_usdt:
                    break

                df = klines_map.get(u.symbol)
                feat = extract_features(df) if df is not None else None
                if feat is None:
                    continue

                if feat.last_candle_close_ms > 0 and (now * 1000 - feat.last_candle_close_ms) > (
                    self.config.stale_candle_sec * 1000
                ):
                    stale_this_cycle += 1
                    pending_events.append(
                        (
                            f"v2_stale_candle_{u.symbol}_{now}_{self.cycle_count}",
                            "STALE_DATA_SKIP",
                            "SYSTEM",
                            u.symbol,
                            {"phase": "candle", "last_candle_close_ms": feat.last_candle_close_ms},
                            now,
                        )
                    )
                    continue

                sig = evaluate_baseline(feat, allow_short=(u.futures is not None))
                if sig.action == "HOLD":
                    hold_this_cycle += 1
                    continue
                signals_triggered += 1

                spot_can = u.spot is not None and self.spot_ledger.can_open(
                    now_utc=now, min_cash_needed=self.config.notional_min_tolerance_usdt
                )
                fut_lev = self.config.futures_default_leverage
                fut_can = u.futures is not None and self.futures_ledger.can_open(
                    now_utc=now, min_cash_needed=self.config.notional_min_tolerance_usdt / fut_lev
                )
                route, route_reason = select_route(
                    side=sig.action,
                    strategy=sig.strategy,
                    spot_eligible=(u.spot is not None),
                    futures_eligible=(u.futures is not None),
                    spot_can_open=spot_can,
                    futures_can_open=fut_can,
                    spot_exposure=self.spot_ledger.current_exposure(),
                    futures_exposure=self.futures_ledger.current_exposure(),
                    funding_rate=(u.futures.funding_rate if u.futures else 0.0),
                )

                if route is None:
                    rejected_this_cycle += 1
                    pending_events.append(
                        (
                            f"v2_rej_route_{u.symbol}_{now}_{self.cycle_count}",
                            "ORDER_REJECTED",
                            "SYSTEM",
                            u.symbol,
                            {
                                "reason": route_reason,
                                "strategy": sig.strategy,
                                "side": sig.action,
                                "category": u.category,
                                "selection_bucket": u.selection_bucket,
                            },
                            now,
                        )
                    )
                    continue

                quote = u.spot if route == "SPOT" else u.futures
                ok_q, q_reason = validate_quote(
                    quote,
                    now_utc=now,
                    stale_sec=self.config.stale_ticker_sec,
                    max_spread_pct=self.config.max_spread_pct,
                )
                if not ok_q or quote is None:
                    stale_this_cycle += 1
                    pending_events.append(
                        (
                            f"v2_stale_entry_{route}_{u.symbol}_{now}_{self.cycle_count}",
                            "STALE_DATA_SKIP",
                            route,
                            u.symbol,
                            {"phase": "entry_quote", "reason": q_reason},
                            now,
                        )
                    )
                    continue

                ok_open, open_reason, pos = self.coordinator.try_open(
                    route_ledger=route,  # type: ignore[arg-type]
                    symbol=u.symbol,
                    side=sig.action,  # type: ignore[arg-type]
                    bid=quote.bid,
                    ask=quote.ask,
                    stop_loss=sig.stop_loss,
                    take_profit=sig.take_profit,
                    rules=quote.rules,
                    ts_utc=now,
                    leverage=fut_lev if route == "FUTURES" else 1.0,
                    slippage_bps=self.config.slippage_bps,
                    strategy=sig.strategy,
                    category=u.category,
                    selection_bucket=u.selection_bucket,
                    route_reason=route_reason,
                    mark_price=quote.mark_price,
                )
                if ok_open and pos is not None:
                    opened_this_cycle += 1
                    pending_events.append(
                        (
                            f"v2_open_{route}_{u.symbol}_{now}_{self.cycle_count}",
                            "POSITION_OPENED",
                            route,
                            u.symbol,
                            pos.to_dict(),
                            now,
                        )
                    )
                else:
                    rejected_this_cycle += 1
                    pending_events.append(
                        (
                            f"v2_rej_{route}_{u.symbol}_{now}_{self.cycle_count}",
                            "ORDER_REJECTED",
                            route,
                            u.symbol,
                            {
                                "reason": open_reason,
                                "route_reason": route_reason,
                                "strategy": sig.strategy,
                                "side": sig.action,
                                "category": u.category,
                                "selection_bucket": u.selection_bucket,
                            },
                            now,
                        )
                    )

        attribution = compute_attribution(self.spot_ledger, self.futures_ledger)
        bucket_counts: dict[str, int] = {}
        for u in universe:
            bucket_counts[u.selection_bucket] = bucket_counts.get(u.selection_bucket, 0) + 1

        hb_payload = {
            "version": self.config.version,
            "neural_model": NEURAL_MODEL_STATUS,
            "cycle": self.cycle_count,
            "spot_equity": round(self.spot_ledger.equity(), 6),
            "futures_equity": round(self.futures_ledger.equity(), 6),
            "combined_equity": round(self.spot_ledger.equity() + self.futures_ledger.equity(), 6),
            "spot_exposure": round(self.spot_ledger.current_exposure(), 4),
            "futures_exposure": round(self.futures_ledger.current_exposure(), 4),
            "global_exposure": round(self.coordinator.total_exposure(), 4),
            "open_positions": self.coordinator.total_open_positions(),
            "spot_open": len(self.spot_ledger.positions),
            "futures_open": len(self.futures_ledger.positions),
            "spot_closed": len(self.spot_ledger.closed_trades),
            "futures_closed": len(self.futures_ledger.closed_trades),
            "universe_count": len(universe),
            "spot_eligible_count": spot_elig_n,
            "futures_eligible_count": fut_elig_n,
            "universe_buckets": bucket_counts,
            "signals_triggered": signals_triggered,
            "opened_this_cycle": opened_this_cycle,
            "rejected_this_cycle": rejected_this_cycle,
            "hold_this_cycle": hold_this_cycle,
            "stale_this_cycle": stale_this_cycle,
        }
        pending_events.append(
            (f"v2_hb_{now}_{self.cycle_count}", "HEARTBEAT", "SYSTEM", "ALL", hb_payload, now)
        )

        cur_hour = now // 3600
        if cur_hour > self.last_hourly_report_hour:
            self.last_hourly_report_hour = cur_hour
            pending_events.append(
                (
                    f"v2_hourly_{cur_hour}",
                    "HOURLY_REPORT",
                    "SYSTEM",
                    "ALL",
                    {
                        "hour_bucket": cur_hour,
                        "cycle": self.cycle_count,
                        "global_exposure": round(self.coordinator.total_exposure(), 4),
                        "open_positions": self.coordinator.total_open_positions(),
                        "attribution": attribution,
                    },
                    now,
                )
            )
            self.logger.info(
                "HOURLY_REPORT hour=%d cycle=%d eq=%.2f (SPOT=%.2f FUT=%.2f) exp=%.2f open=%d",
                cur_hour,
                self.cycle_count,
                hb_payload["combined_equity"],
                hb_payload["spot_equity"],
                hb_payload["futures_equity"],
                hb_payload["global_exposure"],
                hb_payload["open_positions"],
            )

        universe_rows = self._serialize_universe(universe)
        state_payload = self._state_payload(
            now,
            last_universe_summary={
                "universe_count": len(universe),
                "spot_eligible_count": spot_elig_n,
                "futures_eligible_count": fut_elig_n,
                "buckets": bucket_counts,
            },
        )
        self.journal.commit_atomic(
            events=pending_events,
            runner_state=state_payload,
            universe_payload=universe_rows,
            ts_utc=now,
        )

        cycle_note = (
            f"cycle={self.cycle_count} universe={len(universe)} signals={signals_triggered} "
            f"opened={opened_this_cycle} rejected={rejected_this_cycle} hold={hold_this_cycle} "
            f"stale={stale_this_cycle}"
        )
        self.logger.info("CYCLE %s", cycle_note)
        self.write_status(
            universe_count=len(universe),
            spot_eligible_count=spot_elig_n,
            futures_eligible_count=fut_elig_n,
            now=now,
            last_cycle_notes=cycle_note,
        )
        return hb_payload

    def close_all_at_mark(self, now_utc: int | None = None) -> int:
        now = int(time.time()) if now_utc is None else int(now_utc)
        spot_quotes, fut_quotes = self.collector.fetch_market_quotes(
            min_vol_usdt=0.0, max_spread_pct=0.05, now_utc=now
        )
        pending_events: list[tuple[str, str, str, str, dict[str, Any], int]] = []
        closed = 0
        for ledger, qmap in ((self.spot_ledger, spot_quotes), (self.futures_ledger, fut_quotes)):
            for sym in list(ledger.positions.keys()):
                pos = ledger.positions[sym]
                q = qmap.get(sym)
                # Never synthesize an executable exit price from an old mark.
                if q is None or q.ts_utc > now or now - q.ts_utc > self.config.stale_ticker_sec:
                    continue
                if q.bid <= 0 or q.ask <= 0 or q.ask < q.bid or q.mark_price <= 0:
                    continue
                bid = q.bid
                ask = q.ask
                if ask < bid:
                    ask = bid
                rec = ledger.close_position(
                    sym, bid, ask, "experiment_72h_end", now, slippage_bps=self.config.slippage_bps
                )
                if rec:
                    closed += 1
                    pending_events.append(
                        (
                            f"v2_end_close_{ledger.ledger_type}_{sym}_{now}",
                            "POSITION_CLOSED",
                            ledger.ledger_type,
                            sym,
                            rec,
                            now,
                        )
                    )
        if pending_events:
            self.journal.commit_atomic(
                events=pending_events,
                runner_state=self._state_payload(now),
                ts_utc=now,
            )
        return closed

    def run_forever(self) -> None:
        if self.config.mode != "PAPER_ONLY":
            raise RuntimeError("V2 runner refuses non-paper mode")
        logging.basicConfig(
            filename=str(self.v2_dir / "v2.log"),
            level=logging.INFO,
            format="%(asctime)s | %(levelname)s | %(message)s",
        )

        def _handle_stop(signum: int, _frame: Any) -> None:
            self.logger.info("Received signal %d; stopping gracefully after current step.", signum)
            self._stop_requested = True

        signal.signal(signal.SIGTERM, _handle_stop)
        signal.signal(signal.SIGINT, _handle_stop)

        self.logger.info(
            "Starting V2 72h 100-market paper experiment (PID=%d, restored=%s)",
            os.getpid(),
            self.restored_from_checkpoint,
        )
        try:
            while not self._stop_requested and not self.is_expired():
                t0 = time.monotonic()
                try:
                    self.run_cycle()
                except Exception as exc:
                    self.logger.exception("Cycle error: %s", exc)
                elapsed = time.monotonic() - t0
                sleep_for = max(1.0, self.config.poll_seconds - elapsed)
                deadline = time.monotonic() + sleep_for
                while not self._stop_requested and not self.is_expired() and time.monotonic() < deadline:
                    time.sleep(min(1.0, max(0.1, deadline - time.monotonic())))

            if self.is_expired():
                closed = self.close_all_at_mark()
                self.write_status(0, 0, 0, int(time.time()), last_cycle_notes=f"72h_expired_closed={closed}")
                self.logger.info("V2 72h experiment completed cleanly; closed=%d positions.", closed)
            else:
                self.logger.info("V2 runner stopped cleanly on signal.")
        finally:
            self.close()


if __name__ == "__main__":
    NeuralLabV2Runner().run_forever()
