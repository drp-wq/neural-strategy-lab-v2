"""Atomic SQLite event journal and ledger checkpoint store for idempotent V2 restarts."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any


class V2Journal:
    """Append-only SQLite event journal + atomic state snapshot for V2."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=20.0, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    ts_utc INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    ledger TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_v2_events_type_ts ON events(event_type, ts_utc)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runner_state (
                    state_key TEXT PRIMARY KEY,
                    updated_at_utc INTEGER NOT NULL,
                    state_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS universe_snapshot (
                    snapshot_key TEXT PRIMARY KEY,
                    updated_at_utc INTEGER NOT NULL,
                    universe_json TEXT NOT NULL
                )
                """
            )

    def commit_atomic(
        self,
        events: list[tuple[str, str, str, str, dict[str, Any], int]],
        runner_state: dict[str, Any] | None = None,
        universe_payload: list[dict[str, Any]] | None = None,
        ts_utc: int | None = None,
    ) -> int:
        """Persist events, runner_state, and universe_snapshot in one atomic transaction."""
        now = int(time.time()) if ts_utc is None else int(ts_utc)
        inserted = 0
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for event_id, event_type, ledger, symbol, payload, ev_ts in events:
                    cur = conn.execute(
                        """
                        INSERT OR IGNORE INTO events (event_id, ts_utc, event_type, ledger, symbol, payload_json)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (event_id, int(ev_ts), event_type, ledger, symbol, json.dumps(payload)),
                    )
                    if cur.rowcount > 0:
                        inserted += 1

                if runner_state is not None:
                    conn.execute(
                        """
                        INSERT INTO runner_state (state_key, updated_at_utc, state_json)
                        VALUES ('latest', ?, ?)
                        ON CONFLICT(state_key) DO UPDATE SET
                            updated_at_utc=excluded.updated_at_utc,
                            state_json=excluded.state_json
                        """,
                        (now, json.dumps(runner_state)),
                    )

                if universe_payload is not None:
                    conn.execute(
                        """
                        INSERT INTO universe_snapshot (snapshot_key, updated_at_utc, universe_json)
                        VALUES ('latest', ?, ?)
                        ON CONFLICT(snapshot_key) DO UPDATE SET
                            updated_at_utc=excluded.updated_at_utc,
                            universe_json=excluded.universe_json
                        """,
                        (now, json.dumps(universe_payload)),
                    )

                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return inserted

    def append(
        self,
        event_id: str,
        event_type: str,
        ledger: str,
        symbol: str,
        payload: dict[str, Any],
        ts_utc: int | None = None,
    ) -> bool:
        ts = int(time.time()) if ts_utc is None else int(ts_utc)
        return self.commit_atomic([(event_id, event_type, ledger, symbol, payload, ts)], ts_utc=ts) > 0

    def load_state(self) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state_json FROM runner_state WHERE state_key = 'latest'"
            ).fetchone()
            if row and row[0]:
                return json.loads(row[0])

            # Seamless fallback migration from legacy (meta, ledger_state) tables if present
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "ledger_state" not in tables:
                return None

            lrow = conn.execute(
                "SELECT ts_utc, state_json FROM ledger_state ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if not lrow or not lrow[1]:
                return None
            legacy = json.loads(lrow[1])
            meta = {}
            if "meta" in tables:
                meta = {r[0]: r[1] for r in conn.execute("SELECT * FROM meta").fetchall()}

            now_ts = int(lrow[0])
            start_utc = int(meta.get("start_utc", now_ts))
            end_utc = int(meta.get("end_utc", start_utc + 72 * 3600))

            def _convert_ledger(ltype: str, d: dict[str, Any]) -> dict[str, Any]:
                fee_rate = 0.001 if ltype == "SPOT" else 0.0004
                positions: dict[str, Any] = {}
                locked = 0.0
                for sym, p in (d.get("positions") or {}).items():
                    notional = float(p.get("actual_notional") or p.get("filled_notional") or 50.0)
                    margin = float(p.get("margin_used") or 0.0)
                    locked += margin
                    opened_at = int(p.get("opened_at_utc") or now_ts)
                    positions[sym] = {
                        "symbol": sym,
                        "ledger": ltype,
                        "side": p.get("side", "LONG"),
                        "qty": float(p.get("qty", 0.0)),
                        "entry_price": float(p.get("entry_price", 0.0)),
                        "target_notional": 50.0,
                        "actual_notional": notional,
                        "stop_loss": float(p.get("stop_loss", 0.0)),
                        "take_profit": float(p.get("take_profit", 0.0)),
                        "leverage": float(p.get("leverage", 1.0)),
                        "margin_used": margin,
                        "liquidation_price": float(p.get("liquidation_price", 0.0)),
                        "entry_fee": float(p.get("entry_fee") or notional * fee_rate),
                        "accumulated_funding": float(p.get("funding_paid", 0.0)),
                        "last_funding_bucket": opened_at // 28800,
                        "opened_at_utc": opened_at,
                        "strategy": str(p.get("strategy", "")),
                        "category": str(p.get("category", "")),
                        "selection_bucket": str(p.get("category", "")),
                        "route_reason": "migrated_v2_initial",
                        "mark_price": float(p.get("entry_price", 0.0)),
                        "unrealized_pnl": float(p.get("unrealized_pnl", 0.0)),
                    }
                cash = float(d.get("cash_usdt", 2500.0))
                starting = 2500.0
                realized = cash + locked - starting
                return {
                    "ledger_type": ltype,
                    "starting_capital": starting,
                    "cash_usdt": cash,
                    "daily_start_equity": float(d.get("daily_start_equity", starting)),
                    "trading_day": str(d.get("trading_day", "2026-11-17")),
                    "daily_halted": bool(d.get("daily_halted", False)),
                    "daily_dd_halt_pct": 0.05,
                    "max_leverage": 1.0 if ltype == "SPOT" else 2.0,
                    "maintenance_margin_rate": 0.005,
                    "max_risk_per_trade_pct": 0.005,
                    "fee_rate": fee_rate,
                    "realized_pnl": realized,
                    "total_fees": float(d.get("total_fees", 0.0)),
                    "total_funding": float(d.get("total_funding", 0.0)),
                    "positions": positions,
                    "closed_trades": list(d.get("closed_trades", [])),
                }

            hb_count = conn.execute(
                "SELECT COUNT(*) FROM events WHERE event_type = 'HEARTBEAT'"
            ).fetchone()[0]
            return {
                "version": "2.0.0",
                "mode": "PAPER_ONLY",
                "neural_model": "UNTRAINED",
                "start_utc": start_utc,
                "end_utc": end_utc,
                "cycle_count": int(hb_count),
                "last_hourly_report_hour": now_ts // 3600,
                "updated_at_utc": now_ts,
                "spot_ledger": _convert_ledger("SPOT", legacy.get("spot", {})),
                "futures_ledger": _convert_ledger("FUTURES", legacy.get("futures", {})),
            }

    def load_universe(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT universe_json FROM universe_snapshot WHERE snapshot_key = 'latest'"
            ).fetchone()
        if not row:
            return []
        return json.loads(row[0])

    def query(self, event_type: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if event_type:
                rows = conn.execute(
                    "SELECT event_id, ts_utc, event_type, ledger, symbol, payload_json "
                    "FROM events WHERE event_type = ? ORDER BY ts_utc DESC, rowid DESC LIMIT ?",
                    (event_type, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT event_id, ts_utc, event_type, ledger, symbol, payload_json "
                    "FROM events ORDER BY ts_utc DESC, rowid DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [
            {
                "event_id": r[0],
                "ts_utc": int(r[1]),
                "event_type": r[2],
                "ledger": r[3],
                "symbol": r[4],
                "payload": json.loads(r[5]),
            }
            for r in rows
        ]
