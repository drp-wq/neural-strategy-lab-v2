"""Phase 2 (6h Re-Evaluation & Upgrade) helper: train Kronos & upgrade MasterStrategy."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from v2.journal import V2Journal
from v2.master_strategy import MasterStrategy


def run_phase2_upgrade(v2_dir: str | Path = "/root/neural-strategy-lab/v2") -> dict[str, Any]:
    """Analyze closed paper trades from v2_journal.db and upgrade MasterStrategy."""
    v2_path = Path(v2_dir)
    journal = V2Journal(v2_path / "v2_journal.db")
    closed_events = journal.query(event_type="POSITION_CLOSED", limit=5000)
    closed_trades = [ev["payload"] for ev in closed_events]

    master = MasterStrategy()
    lessons = master.upgrade_from_trades(closed_trades)

    out_path = v2_path / "phase2_lessons.json"
    out_path.write_text(json.dumps(lessons, indent=2), encoding="utf-8")
    return lessons


if __name__ == "__main__":
    print(json.dumps(run_phase2_upgrade(), indent=2))
