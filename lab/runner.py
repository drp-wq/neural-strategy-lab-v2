"""72-hour time-bounded paper-only experiment runner."""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from lab.collectors import MarketSnapshot, PublicBinanceCollector
from lab.features import extract_features
from lab.journal import EventJournal
from lab.ledger import PaperLedger, SymbolRules
from lab.strategies import NEURAL_MODEL_STATUS, evaluate_baseline

DURATION_SECONDS = 72 * 3600
POLL_SECONDS = 60


class NeuralLabRunner:
    """Runs the isolated 72-hour SPOT (25 USDT) + FUTURES (25 USDT) paper experiment."""

    def __init__(
        self,
        work_dir: str | Path = "/root/neural-strategy-lab",
        duration_sec: int = DURATION_SECONDS,
    ) -> None:
        self.work_dir = Path(work_dir)
        self.journal = EventJournal(self.work_dir / "lab_journal.db")
        self.spot_ledger = PaperLedger("SPOT", starting_capital=25.0)
        self.futures_ledger = PaperLedger("FUTURES", starting_capital=25.0)
        self.spot_collector = PublicBinanceCollector("SPOT")
        self.futures_collector = PublicBinanceCollector("FUTURES")
        self.duration_sec = duration_sec
        self.start_utc = int(time.time())
        self.end_utc = self.start_utc + self.duration_sec
        self.last_snapshot_hour = -1
        self.logger = logging.getLogger("neural_lab")

    def is_expired(self, now_utc: int | None = None) -> bool:
        now = int(time.time()) if now_utc is None else now_utc
        return now >= self.end_utc

    def _update_positions(
        self, ledger: PaperLedger, snapshots: dict[str, MarketSnapshot], now: int
    ) -> None:
        for sym in list(ledger.positions.keys()):
            snap = snapshots.get(sym)
            if snap is None or self.spot_collector.is_stale(snap, now):
                continue
            pos = ledger.positions[sym]
            pos.update_mark(snap.mark_price)

            hit_sl = (
                (pos.side == "LONG" and snap.mark_price <= pos.stop_loss)
                or (pos.side == "SHORT" and snap.mark_price >= pos.stop_loss)
            )
            hit_tp = (
                (pos.side == "LONG" and snap.mark_price >= pos.take_profit)
                or (pos.side == "SHORT" and snap.mark_price <= pos.take_profit)
            )
            hit_liq = pos.liquidation_price > 0 and (
                (pos.side == "LONG" and snap.mark_price <= pos.liquidation_price)
                or (pos.side == "SHORT" and snap.mark_price >= pos.liquidation_price)
            )
            if hit_sl or hit_tp or hit_liq:
                reason = "liquidation" if hit_liq else ("stop_loss" if hit_sl else "take_profit")
                rec = ledger.close_position(sym, snap.bid, snap.ask, reason, now)
                if rec:
                    self.journal.append(
                        f"close_{ledger.ledger_type}_{sym}_{now}",
                        "POSITION_CLOSED",
                        ledger.ledger_type,
                        sym,
                        rec,
                        ts_utc=now,
                    )

    def _process_ledger(
        self,
        ledger: PaperLedger,
        collector: PublicBinanceCollector,
        now: int,
    ) -> list[MarketSnapshot]:
        universe = collector.fetch_universe(max_symbols=50)
        snap_map = {s.symbol: s for s in universe}
        self._update_positions(ledger, snap_map, now)

        if self.is_expired(now):
            return universe

        for snap in universe[:15]:  # Evaluate top 15 liquid candidates per cycle
            if collector.is_stale(snap, now):
                self.journal.append(
                    f"stale_{ledger.ledger_type}_{snap.symbol}_{now}",
                    "STALE_DATA_SKIP",
                    ledger.ledger_type,
                    snap.symbol,
                    {"ts": snap.ts_utc},
                    ts_utc=now,
                )
                continue
            try:
                df = collector.fetch_klines(snap.symbol, interval="15m", limit=60)
                feat = extract_features(df)
                if feat is None:
                    continue
                sig = evaluate_baseline(feat, ledger.ledger_type)
                if sig.action == "HOLD":
                    continue

                ok, reason, pos = ledger.open_position(
                    symbol=snap.symbol,
                    side=sig.action,  # type: ignore[arg-type]
                    bid=snap.bid,
                    ask=snap.ask,
                    stop_loss=sig.stop_loss,
                    take_profit=sig.take_profit,
                    rules=SymbolRules(min_qty=0.0001, step_size=0.0001, min_notional=5.0),
                    ts_utc=now,
                    leverage=1.0,
                    strategy=sig.strategy,
                    category=snap.category,
                )
                if ok and pos is not None:
                    self.journal.append(
                        f"open_{ledger.ledger_type}_{snap.symbol}_{now}",
                        "POSITION_OPENED",
                        ledger.ledger_type,
                        snap.symbol,
                        pos.to_dict(),
                        ts_utc=now,
                    )
                else:
                    self.journal.append(
                        f"rej_{ledger.ledger_type}_{snap.symbol}_{now}",
                        "ORDER_REJECTED",
                        ledger.ledger_type,
                        snap.symbol,
                        {"reason": reason, "strategy": sig.strategy},
                        ts_utc=now,
                    )
            except Exception as exc:
                self.logger.warning("Error evaluating %s %s: %s", ledger.ledger_type, snap.symbol, exc)

        return universe

    def write_status(self, spot_n: int, fut_n: int, now: int) -> None:
        start_iso = datetime.fromtimestamp(self.start_utc, tz=timezone.utc).isoformat()
        end_iso = datetime.fromtimestamp(self.end_utc, tz=timezone.utc).isoformat()
        now_iso = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()
        status_md = (
            f"# Neural Strategy Lab — 72h Paper Experiment Status\n\n"
            f"- **Mode**: PAPER ONLY (no live keys, no real orders)\n"
            f"- **Neural Model**: `{NEURAL_MODEL_STATUS}` (deterministic rule-based baseline)\n"
            f"- **Start UTC**: `{start_iso}`\n"
            f"- **Scheduled End UTC**: `{end_iso}`\n"
            f"- **Last Heartbeat UTC**: `{now_iso}`\n"
            f"- **PID**: `{os.getpid()}`\n"
            f"- **SPOT Ledger**: equity=`{self.spot_ledger.equity():.4f} USDT`, "
            f"open=`{len(self.spot_ledger.positions)}`, closed=`{len(self.spot_ledger.closed_trades)}`\n"
            f"- **FUTURES Ledger**: equity=`{self.futures_ledger.equity():.4f} USDT`, "
            f"open=`{len(self.futures_ledger.positions)}`, closed=`{len(self.futures_ledger.closed_trades)}`\n"
            f"- **Sampled Universe**: SPOT=`{spot_n}` symbols, FUTURES=`{fut_n}` symbols\n"
            f"- **Blockers**: None\n"
        )
        (self.work_dir / "STATUS.md").write_text(status_md, encoding="utf-8")

    def run_cycle(self) -> None:
        now = int(time.time())
        spot_u = self._process_ledger(self.spot_ledger, self.spot_collector, now)
        fut_u = self._process_ledger(self.futures_ledger, self.futures_collector, now)

        self.journal.append(
            f"hb_{now}",
            "HEARTBEAT",
            "SYSTEM",
            "ALL",
            {
                "spot_equity": self.spot_ledger.equity(),
                "futures_equity": self.futures_ledger.equity(),
                "spot_universe": len(spot_u),
                "futures_universe": len(fut_u),
            },
            ts_utc=now,
        )
        self.write_status(len(spot_u), len(fut_u), now)

    def close_all_at_mark(self) -> None:
        now = int(time.time())
        for ledger, collector in (
            (self.spot_ledger, self.spot_collector),
            (self.futures_ledger, self.futures_collector),
        ):
            if not ledger.positions:
                continue
            snaps = {s.symbol: s for s in collector.fetch_universe(max_symbols=50)}
            for sym in list(ledger.positions.keys()):
                snap = snaps.get(sym)
                bid = snap.bid if snap else ledger.positions[sym].entry_price
                ask = snap.ask if snap else ledger.positions[sym].entry_price
                rec = ledger.close_position(sym, bid, ask, "experiment_72h_end", now)
                if rec:
                    self.journal.append(
                        f"end_close_{ledger.ledger_type}_{sym}_{now}",
                        "POSITION_CLOSED",
                        ledger.ledger_type,
                        sym,
                        rec,
                        ts_utc=now,
                    )

    def run_forever(self) -> None:
        logging.basicConfig(
            filename=str(self.work_dir / "lab.log"),
            level=logging.INFO,
            format="%(asctime)s | %(levelname)s | %(message)s",
        )
        self.logger.info("Starting 72h paper-only experiment (PID=%d)", os.getpid())
        while not self.is_expired():
            try:
                self.run_cycle()
            except Exception as exc:
                self.logger.exception("Cycle error: %s", exc)
            time.sleep(POLL_SECONDS)

        self.close_all_at_mark()
        self.write_status(0, 0, int(time.time()))
        self.logger.info("72h experiment completed and all positions closed.")


if __name__ == "__main__":
    NeuralLabRunner().run_forever()
