"""SQLite append-only event journal for idempotent resume and reporting."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any


class EventJournal:
    """Append-only SQLite event journal with unique event_id idempotency."""

    def __init__(self, db_path: str | Path = "lab_journal.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, timeout=15)

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
                "CREATE INDEX IF NOT EXISTS idx_events_type_ts ON events(event_type, ts_utc)"
            )
            conn.commit()

    def append(
        self,
        event_id: str,
        event_type: str,
        ledger: str,
        symbol: str,
        payload: dict[str, Any],
        ts_utc: int | None = None,
    ) -> bool:
        """Append event idempotently; returns True if inserted, False if duplicate."""
        ts = int(time.time()) if ts_utc is None else int(ts_utc)
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO events (event_id, ts_utc, event_type, ledger, symbol, payload_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (event_id, ts, event_type, ledger, symbol, json.dumps(payload)),
            )
            conn.commit()
            return cur.rowcount > 0

    def query(self, event_type: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if event_type:
                rows = conn.execute(
                    "SELECT event_id, ts_utc, event_type, ledger, symbol, payload_json "
                    "FROM events WHERE event_type = ? ORDER BY ts_utc DESC LIMIT ?",
                    (event_type, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT event_id, ts_utc, event_type, ledger, symbol, payload_json "
                    "FROM events ORDER BY ts_utc DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [
            {
                "event_id": r[0],
                "ts_utc": r[1],
                "event_type": r[2],
                "ledger": r[3],
                "symbol": r[4],
                "payload": json.loads(r[5]),
            }
            for r in rows
        ]
