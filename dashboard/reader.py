"""Read-only data reader for V2 (v2/v2_journal.db) and preserved V1 archive (lab_journal.db)."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

NOT_AVAILABLE = "not yet available"
EXPERIMENT_DURATION_SEC = 72 * 3600
STARTING_SPOT_USDT = 25.0
STARTING_FUTURES_USDT = 25.0


def _iso_to_ts(iso_str: str | None) -> int | None:
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str.strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        return None


def _ts_to_iso(ts: int | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


def parse_status_md(status_path: Path) -> dict[str, Any]:
    """Parse key metadata fields from STATUS.md or V2_STATUS.md without failing on missing lines."""
    result: dict[str, Any] = {
        "exists": status_path.exists(),
        "mode": None,
        "neural_model": "UNTRAINED",
        "start_utc": None,
        "start_ts": None,
        "scheduled_end_utc": None,
        "scheduled_end_ts": None,
        "last_heartbeat_utc": None,
        "last_heartbeat_ts": None,
        "pid": None,
        "blockers": None,
        "spot_summary": None,
        "futures_summary": None,
        "universe_summary": None,
        "raw_lines": [],
    }
    if not status_path.exists():
        return result

    try:
        text = status_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        result["read_error"] = str(exc)
        return result

    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    result["raw_lines"] = lines

    patterns = {
        "mode": r"\*\*Mode\*\*:\s*`?([^`\n]+)`?",
        "neural_model": r"\*\*Neural Model\*\*:\s*`?([^`\n]+)`?",
        "start_utc": r"\*\*Start UTC\*\*:\s*`([^`]+)`",
        "scheduled_end_utc": r"\*\*Scheduled End UTC\*\*:\s*`([^`]+)`",
        "last_heartbeat_utc": r"\*\*Last Heartbeat UTC\*\*:\s*`([^`]+)`",
        "pid": r"\*\*PID\*\*:\s*`(\d+)`",
        "spot_summary": r"\*\*SPOT Ledger\*\*:\s*(.+)",
        "futures_summary": r"\*\*FUTURES Ledger\*\*:\s*(.+)",
        "universe_summary": r"\*\*Sampled Universe\*\*:\s*(.+)",
        "blockers": r"\*\*Blockers\*\*:\s*(.+)",
    }

    for key, pat in patterns.items():
        m = re.search(pat, text)
        if m:
            val = m.group(1).strip()
            if key == "pid":
                result["pid"] = int(val)
            elif key == "neural_model":
                result["neural_model_detail"] = val
                result["neural_model"] = "UNTRAINED" if "UNTRAINED" in val.upper() else val
            else:
                result[key] = val

    result["start_ts"] = _iso_to_ts(result["start_utc"])
    result["scheduled_end_ts"] = _iso_to_ts(result["scheduled_end_utc"])
    result["last_heartbeat_ts"] = _iso_to_ts(result["last_heartbeat_utc"])
    return result


def inspect_runner_process(pid: int | None, expected_module: str = "lab.runner") -> dict[str, Any]:
    """Check whether a paper runner PID is currently alive (read-only)."""
    if not pid or pid <= 0:
        return {"pid": pid, "alive": False, "state": "UNKNOWN", "detail": "No PID recorded"}

    proc_cmdline = Path(f"/proc/{pid}/cmdline")
    proc_stat = Path(f"/proc/{pid}/stat")
    alive = False
    cmdline = ""
    proc_state_code = None

    try:
        os.kill(pid, 0)
        alive = True
    except ProcessLookupError:
        alive = False
    except PermissionError:
        alive = True

    if alive and proc_cmdline.exists():
        try:
            raw = proc_cmdline.read_bytes()
            cmdline = raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
        except OSError:
            cmdline = ""

    if alive and proc_stat.exists():
        try:
            stat_txt = proc_stat.read_text(encoding="utf-8", errors="replace")
            rparen = stat_txt.rfind(")")
            if rparen != -1:
                parts = stat_txt[rparen + 2 :].split()
                if parts:
                    proc_state_code = parts[0]
        except OSError:
            pass

    if alive and proc_state_code == "Z":
        alive = False

    matches_runner = expected_module in cmdline if cmdline else None
    return {
        "pid": pid,
        "alive": alive,
        "state": "RUNNING" if alive else "STOPPED",
        "proc_state_code": proc_state_code,
        "matches_runner": matches_runner,
    }


def _read_tail_lines(path: Path, max_lines: int = 100) -> dict[str, Any]:
    """Safely read the tail of a log file without exposing host root paths."""
    if not path.exists():
        return {"exists": False, "size_bytes": 0, "total_lines": 0, "lines": [], "errors": 0, "warnings": 0}
    try:
        stat = path.stat()
        text = path.read_text(encoding="utf-8", errors="replace")
        all_lines = text.splitlines()
        tail = all_lines[-max_lines:] if max_lines > 0 else all_lines
        errors = sum(1 for ln in all_lines if "| ERROR |" in ln or "Traceback" in ln or "| CRITICAL |" in ln)
        warnings = sum(1 for ln in all_lines if "| WARNING |" in ln)
        error_lines = [
            ln for ln in all_lines[-500:]
            if "| ERROR |" in ln or "| WARNING |" in ln or "Traceback" in ln or "| CRITICAL |" in ln
        ][-20:]
        return {
            "exists": True,
            "size_bytes": stat.st_size,
            "modified_utc": _ts_to_iso(int(stat.st_mtime)),
            "total_lines": len(all_lines),
            "lines": tail,
            "errors": errors,
            "warnings": warnings,
            "recent_error_lines": error_lines,
        }
    except OSError as exc:
        return {
            "exists": True,
            "size_bytes": 0,
            "total_lines": 0,
            "lines": [],
            "errors": 1,
            "warnings": 0,
            "read_error": str(exc),
        }


def _compute_drawdown_from_series(values: list[float]) -> tuple[float | None, float | None]:
    """Return (max_drawdown_pct, current_drawdown_pct) from equity series; None if < 2 points."""
    if len(values) < 2:
        return None, None
    peak = values[0]
    max_dd = 0.0
    for val in values:
        if val > peak:
            peak = val
        if peak > 0:
            dd = (peak - val) / peak * 100.0
            if dd > max_dd:
                max_dd = dd
    current_dd = ((peak - values[-1]) / peak * 100.0) if peak > 0 else 0.0
    return round(max_dd, 4), round(current_dd, 4)


def _compute_ledger_metrics(
    ledger_name: str,
    starting_capital: float,
    latest_equity: float | None,
    equity_series: list[float],
    open_positions: list[dict[str, Any]],
    closed_trades: list[dict[str, Any]],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute metrics strictly from recorded events; return None where data is absent."""
    closed_count = len(closed_trades)
    open_count = len(open_positions)

    max_dd, cur_dd = _compute_drawdown_from_series(equity_series)
    equity_delta = round(latest_equity - starting_capital, 6) if latest_equity is not None else None
    equity_delta_pct = (
        round((latest_equity - starting_capital) / starting_capital * 100.0, 4)
        if latest_equity is not None and starting_capital > 0
        else None
    )

    if closed_count == 0:
        base = {
            "ledger": ledger_name,
            "starting_capital": starting_capital,
            "latest_equity": latest_equity,
            "equity_delta": equity_delta,
            "equity_delta_pct": equity_delta_pct,
            "open_count": open_count,
            "closed_count": 0,
            "wins": None,
            "losses": None,
            "win_rate_pct": None,
            "realized_pnl": None,
            "profit_factor": None,
            "profit_factor_display": NOT_AVAILABLE,
            "recorded_exit_fees": None,
            "avg_holding_sec": None,
            "max_drawdown_pct": max_dd,
            "current_drawdown_pct": cur_dd,
        }
        if extra:
            base.update(extra)
        return base

    pnls = [float(t.get("net_pnl", 0.0)) for t in closed_trades]
    fees = [float(t["fee"]) for t in closed_trades if t.get("fee") is not None]
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))

    if gross_loss > 0:
        pf_val: float | None = round(gross_profit / gross_loss, 4)
        pf_disp = f"{pf_val:.2f}"
    elif gross_profit > 0:
        pf_val = None
        pf_disp = "∞ (no losses yet)"
    else:
        pf_val = None
        pf_disp = NOT_AVAILABLE

    hold_secs = [
        int(t["closed_at_utc"]) - int(t["opened_at_utc"])
        for t in closed_trades
        if t.get("closed_at_utc") is not None and t.get("opened_at_utc") is not None
    ]

    res = {
        "ledger": ledger_name,
        "starting_capital": starting_capital,
        "latest_equity": latest_equity,
        "equity_delta": equity_delta,
        "equity_delta_pct": equity_delta_pct,
        "open_count": open_count,
        "closed_count": closed_count,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(wins / closed_count * 100.0, 2),
        "realized_pnl": round(sum(pnls), 6),
        "profit_factor": pf_val,
        "profit_factor_display": pf_disp,
        "recorded_exit_fees": round(sum(fees), 6) if fees else None,
        "avg_holding_sec": round(sum(hold_secs) / len(hold_secs), 1) if hold_secs else None,
        "max_drawdown_pct": max_dd,
        "current_drawdown_pct": cur_dd,
    }
    if extra:
        res.update(extra)
    return res


def _downsample_points(points: list[dict[str, Any]], max_points: int = 360) -> list[dict[str, Any]]:
    if len(points) <= max_points:
        return points
    step = (len(points) - 1) / (max_points - 1)
    indices = sorted({int(round(i * step)) for i in range(max_points)})
    return [points[i] for i in indices]


def _read_db_events(db_path: Path) -> tuple[list[dict[str, Any]], str | None]:
    """Open SQLite DB strictly read-only via URI mode=ro and load events."""
    if not db_path.exists():
        return [], f"{db_path.name} not found"

    uri = f"file:{db_path.resolve()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        try:
            rows = conn.execute(
                "SELECT event_id, ts_utc, event_type, ledger, symbol, payload_json "
                "FROM events ORDER BY ts_utc ASC, rowid ASC"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return [], f"SQLite read error: {exc}"

    events: list[dict[str, Any]] = []
    for event_id, ts_utc, event_type, ledger, symbol, payload_json in rows:
        try:
            payload = json.loads(payload_json) if payload_json else {}
        except json.JSONDecodeError:
            payload = {"raw": payload_json}
        events.append(
            {
                "event_id": event_id,
                "ts_utc": int(ts_utc),
                "ts_iso": _ts_to_iso(int(ts_utc)),
                "event_type": str(event_type),
                "ledger": str(ledger),
                "symbol": str(symbol),
                "payload": payload,
            }
        )
    return events, None


def _read_v2_tables(db_path: Path) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Read V2 runner_state and universe_snapshot strictly read-only."""
    if not db_path.exists():
        return None, []
    uri = f"file:{db_path.resolve()}?mode=ro"
    runner_state = None
    universe: list[dict[str, Any]] = []
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        try:
            tables = {
                r[0]
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            if "runner_state" in tables:
                row = conn.execute(
                    "SELECT state_json FROM runner_state WHERE state_key = 'latest'"
                ).fetchone()
                if row and row[0]:
                    runner_state = json.loads(row[0])
            if "universe_snapshot" in tables:
                urow = conn.execute(
                    "SELECT universe_json FROM universe_snapshot WHERE snapshot_key = 'latest'"
                ).fetchone()
                if urow and urow[0]:
                    universe = json.loads(urow[0])
        finally:
            conn.close()
    except (sqlite3.Error, json.JSONDecodeError):
        return None, []
    return runner_state, universe


def _collect_local_agy_activity(work_dir: Path) -> dict[str, Any]:
    """Read ONLY project-local AGY logs/files inside work_dir; never global AGY sessions."""
    v2_agy = work_dir / "v2" / "agy_build.log"
    root_agy = work_dir / "dashboard_agy.log"
    primary = v2_agy if v2_agy.exists() and v2_agy.stat().st_size > 0 else root_agy
    log_info = _read_tail_lines(primary, max_lines=60)

    local_logs: list[dict[str, Any]] = []
    try:
        for base in (work_dir, work_dir / "v2"):
            if not base.exists():
                continue
            for p in sorted(base.glob("*agy*.log")):
                if p.is_file():
                    st = p.stat()
                    rel_name = p.name if base == work_dir else f"v2/{p.name}"
                    local_logs.append(
                        {
                            "name": rel_name,
                            "size_bytes": st.st_size,
                            "modified_utc": _ts_to_iso(int(st.st_mtime)),
                        }
                    )
    except OSError:
        pass

    note = (
        f"Isolated to project folder only (reading {primary.name})."
        if log_info["size_bytes"] > 0
        else "Isolated to project folder only. No local AGY log bytes recorded yet."
    )
    return {
        "isolated": True,
        "primary_log": primary.name,
        "log_files": local_logs,
        "tail": log_info,
        "note": note,
    }


def _reconstruct_from_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    heartbeats: list[dict[str, Any]] = []
    open_map: dict[tuple[str, str, int | None], dict[str, Any]] = {}
    closed_trades: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []
    stale_skips: list[dict[str, Any]] = []
    hourly_reports: list[dict[str, Any]] = []
    observed_symbols: dict[str, dict[str, Any]] = {}
    category_counts: dict[str, int] = {}

    for ev in events:
        etype = ev["event_type"]
        ledger = ev["ledger"]
        sym = ev["symbol"]
        payload = ev["payload"] if isinstance(ev["payload"], dict) else {}

        if sym and sym != "ALL":
            entry = observed_symbols.setdefault(
                f"{ledger}:{sym}",
                {
                    "ledger": ledger,
                    "symbol": sym,
                    "category": payload.get("category") or NOT_AVAILABLE,
                    "events": 0,
                    "last_event_type": etype,
                    "last_ts_utc": ev["ts_utc"],
                },
            )
            entry["events"] += 1
            entry["last_event_type"] = etype
            entry["last_ts_utc"] = ev["ts_utc"]
            if payload.get("category"):
                entry["category"] = payload["category"]

        cat = payload.get("category")
        if cat:
            category_counts[str(cat)] = category_counts.get(str(cat), 0) + 1

        if etype == "HEARTBEAT":
            spot_eq = payload.get("spot_equity")
            fut_eq = payload.get("futures_equity")
            tot_eq = payload.get("combined_equity")
            if tot_eq is None and spot_eq is not None and fut_eq is not None:
                tot_eq = round(float(spot_eq) + float(fut_eq), 6)
            heartbeats.append(
                {
                    "ts_utc": ev["ts_utc"],
                    "ts_iso": ev["ts_iso"],
                    "spot_equity": float(spot_eq) if spot_eq is not None else None,
                    "futures_equity": float(fut_eq) if fut_eq is not None else None,
                    "total_equity": float(tot_eq) if tot_eq is not None else None,
                    "spot_universe": payload.get("spot_universe") or payload.get("spot_eligible_count"),
                    "futures_universe": payload.get("futures_universe") or payload.get("futures_eligible_count"),
                    "universe_count": payload.get("universe_count"),
                    "global_exposure": payload.get("global_exposure"),
                    "open_positions": payload.get("open_positions"),
                    "universe_buckets": payload.get("universe_buckets"),
                    "signals_triggered": payload.get("signals_triggered"),
                    "opened_this_cycle": payload.get("opened_this_cycle"),
                    "hold_this_cycle": payload.get("hold_this_cycle"),
                }
            )
        elif etype == "HOURLY_REPORT":
            hourly_reports.append({"ts_utc": ev["ts_utc"], "ts_iso": ev["ts_iso"], **payload})
        elif etype == "POSITION_OPENED":
            opened_at = payload.get("opened_at_utc") or ev["ts_utc"]
            pos_record = {
                **payload,
                "symbol": payload.get("symbol", sym),
                "ledger": payload.get("ledger", ledger),
                "opened_at_utc": int(opened_at),
                "opened_at_iso": _ts_to_iso(int(opened_at)),
                "event_id": ev["event_id"],
            }
            open_map[(pos_record["ledger"], pos_record["symbol"], pos_record["opened_at_utc"])] = pos_record
        elif etype == "POSITION_CLOSED":
            opened_at = payload.get("opened_at_utc")
            key = (payload.get("ledger", ledger), payload.get("symbol", sym), int(opened_at) if opened_at else None)
            if key in open_map:
                open_map.pop(key)
            else:
                for k in list(open_map.keys()):
                    if k[0] == key[0] and k[1] == key[1]:
                        open_map.pop(k)
                        break
            closed_at = payload.get("closed_at_utc") or ev["ts_utc"]
            closed_trades.append(
                {
                    **payload,
                    "symbol": payload.get("symbol", sym),
                    "ledger": payload.get("ledger", ledger),
                    "closed_at_utc": int(closed_at),
                    "closed_at_iso": _ts_to_iso(int(closed_at)),
                    "opened_at_iso": _ts_to_iso(int(opened_at)) if opened_at else None,
                    "event_id": ev["event_id"],
                }
            )
        elif etype == "ORDER_REJECTED":
            rejections.append(
                {
                    "event_id": ev["event_id"],
                    "ts_utc": ev["ts_utc"],
                    "ts_iso": ev["ts_iso"],
                    "ledger": ledger,
                    "symbol": sym,
                    "reason": payload.get("reason", NOT_AVAILABLE),
                    "strategy": payload.get("strategy", NOT_AVAILABLE),
                }
            )
        elif etype == "STALE_DATA_SKIP":
            stale_skips.append(
                {
                    "event_id": ev["event_id"],
                    "ts_utc": ev["ts_utc"],
                    "ts_iso": ev["ts_iso"],
                    "ledger": ledger,
                    "symbol": sym,
                    "snapshot_ts": payload.get("ts") or payload.get("quote_ts"),
                    "reason": payload.get("reason"),
                }
            )

    return {
        "heartbeats": heartbeats,
        "open_positions": sorted(open_map.values(), key=lambda x: x.get("opened_at_utc", 0), reverse=True),
        "closed_trades": sorted(closed_trades, key=lambda x: x.get("closed_at_utc", 0), reverse=True),
        "rejections": sorted(rejections, key=lambda x: x["ts_utc"], reverse=True),
        "stale_skips": sorted(stale_skips, key=lambda x: x["ts_utc"], reverse=True),
        "hourly_reports": hourly_reports,
        "observed_symbols": observed_symbols,
        "category_counts": category_counts,
    }


def _evaluate_health(proc_info: dict[str, Any], hb_age_sec: int | None) -> tuple[str, str | None]:
    if not proc_info["alive"]:
        return "OFFLINE", f"Runner PID {proc_info['pid']} is not running"
    if hb_age_sec is None:
        return "WARNING", "No heartbeat timestamp recorded yet"
    if hb_age_sec > 300:
        return "OFFLINE", f"Heartbeat is critically stale ({hb_age_sec}s > 300s)"
    if hb_age_sec > 120:
        return "STALE", f"Heartbeat is stale ({hb_age_sec}s > 120s)"
    return "HEALTHY", None


def _build_v1_snapshot(work_dir: Path, now: int) -> dict[str, Any]:
    """Build complete V1 snapshot (used both when V2 is absent and as V1 archive)."""
    status = parse_status_md(work_dir / "STATUS.md")
    events, db_error = _read_db_events(work_dir / "lab_journal.db")
    lab_log = _read_tail_lines(work_dir / "lab.log", max_lines=100)
    agy_activity = _collect_local_agy_activity(work_dir)
    proc_info = inspect_runner_process(status.get("pid"), expected_module="lab.runner")

    rec = _reconstruct_from_events(events)
    heartbeats = rec["heartbeats"]
    open_positions = rec["open_positions"]
    closed_trades_desc = rec["closed_trades"]
    rejections_desc = rec["rejections"]
    stale_skips_desc = rec["stale_skips"]

    latest_hb = heartbeats[-1] if heartbeats else None
    last_hb_ts = latest_hb["ts_utc"] if latest_hb else status.get("last_heartbeat_ts")
    hb_age_sec = max(0, now - last_hb_ts) if last_hb_ts is not None else None
    health_status, health_warning = _evaluate_health(proc_info, hb_age_sec)

    start_ts = status.get("start_ts")
    end_ts = status.get("scheduled_end_ts")
    if start_ts is not None and end_ts is not None and end_ts > start_ts:
        total_dur = end_ts - start_ts
        elapsed_sec = max(0, min(total_dur, now - start_ts))
        remaining_sec = max(0, end_ts - now)
        progress_pct = round(elapsed_sec / total_dur * 100.0, 2)
    else:
        total_dur = EXPERIMENT_DURATION_SEC
        elapsed_sec = None
        remaining_sec = None
        progress_pct = None

    spot_series = [h["spot_equity"] for h in heartbeats if h.get("spot_equity") is not None]
    fut_series = [h["futures_equity"] for h in heartbeats if h.get("futures_equity") is not None]
    tot_series = [h["total_equity"] for h in heartbeats if h.get("total_equity") is not None]

    spot_open = [p for p in open_positions if p.get("ledger") == "SPOT"]
    fut_open = [p for p in open_positions if p.get("ledger") == "FUTURES"]
    spot_closed = [t for t in closed_trades_desc if t.get("ledger") == "SPOT"]
    fut_closed = [t for t in closed_trades_desc if t.get("ledger") == "FUTURES"]

    spot_metrics = _compute_ledger_metrics(
        "SPOT", STARTING_SPOT_USDT, spot_series[-1] if spot_series else None, spot_series, spot_open, spot_closed
    )
    fut_metrics = _compute_ledger_metrics(
        "FUTURES", STARTING_FUTURES_USDT, fut_series[-1] if fut_series else None, fut_series, fut_open, fut_closed
    )
    combined_metrics = _compute_ledger_metrics(
        "COMBINED",
        STARTING_SPOT_USDT + STARTING_FUTURES_USDT,
        tot_series[-1] if tot_series else None,
        tot_series,
        open_positions,
        closed_trades_desc,
    )

    spot_univ_count = latest_hb.get("spot_universe") if latest_hb else None
    fut_univ_count = latest_hb.get("futures_universe") if latest_hb else None

    return {
        "experiment_version": "v1",
        "generated_at_utc": _ts_to_iso(now),
        "generated_at_ts": now,
        "paper_only": True,
        "read_only": True,
        "neural_model": {
            "status": status.get("neural_model") or "UNTRAINED",
            "detail": status.get("neural_model_detail") or "UNTRAINED (deterministic rule-based baseline)",
        },
        "experiment": {
            "mode": status.get("mode") or "PAPER ONLY",
            "start_utc": status.get("start_utc"),
            "start_ts": start_ts,
            "scheduled_end_utc": status.get("scheduled_end_utc"),
            "scheduled_end_ts": end_ts,
            "duration_sec": total_dur,
            "elapsed_sec": elapsed_sec,
            "remaining_sec": remaining_sec,
            "progress_pct": progress_pct,
            "blockers": status.get("blockers") or "None",
        },
        "heartbeat": {
            "last_utc": _ts_to_iso(last_hb_ts),
            "last_ts": last_hb_ts,
            "age_sec": hb_age_sec,
            "status": health_status,
            "warning": health_warning,
            "count": len(heartbeats),
        },
        "process": proc_info,
        "ledgers": {
            "SPOT": spot_metrics,
            "FUTURES": fut_metrics,
            "COMBINED": combined_metrics,
        },
        "equity_curve": _downsample_points(heartbeats, max_points=360),
        "positions": {
            "open": open_positions,
            "closed": closed_trades_desc,
        },
        "decisions": {
            "rejections": rejections_desc[:100],
            "rejection_count": len(rejections_desc),
            "stale_skips": stale_skips_desc[:100],
            "stale_skip_count": len(stale_skips_desc),
        },
        "universe": {
            "spot_count": spot_univ_count,
            "futures_count": fut_univ_count,
            "full_list_persisted": False,
            "persisted_note": (
                "Only universe counts are persisted in HEARTBEAT events; "
                "individual symbols and categories below reflect persisted trade/decision events."
            ),
            "observed_symbols": sorted(
                rec["observed_symbols"].values(), key=lambda x: x["last_ts_utc"], reverse=True
            ),
            "observed_categories": rec["category_counts"],
        },
        "db": {
            "readable": db_error is None,
            "error": db_error,
            "total_events": len(events),
        },
        "logs": {
            "lab_log": lab_log,
            "agy_activity": agy_activity,
        },
        "status_md": {
            "exists": status["exists"],
            "raw_lines": status["raw_lines"],
        },
    }


def _build_v1_archive_summary(work_dir: Path, now: int) -> dict[str, Any] | None:
    """Build clearly separated read-only summary of archived V1 results."""
    v1_db = work_dir / "lab_journal.db"
    v1_status = work_dir / "STATUS.md"
    archive_file = work_dir / "v2" / "v1_archive_summary.json"
    if not v1_db.exists() and not v1_status.exists() and not archive_file.exists():
        return None

    v1_snap = _build_v1_snapshot(work_dir, now)
    retired_meta: dict[str, Any] = {}
    if archive_file.exists():
        try:
            retired_meta = json.loads(archive_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            retired_meta = {}

    is_retired = bool(retired_meta.get("retired")) or not v1_snap["process"]["alive"]
    return {
        "present": True,
        "version": "v1",
        "retired": is_retired,
        "status_label": "RETIRED / ARCHIVED" if is_retired else "RUNNING (CUTOVER PENDING)",
        "retired_at_utc": retired_meta.get("retired_at_utc"),
        "retirement_reason": retired_meta.get("reason"),
        "process": v1_snap["process"],
        "experiment": v1_snap["experiment"],
        "heartbeat": v1_snap["heartbeat"],
        "ledgers": v1_snap["ledgers"],
        "positions": v1_snap["positions"],
        "total_events": v1_snap["db"]["total_events"],
    }


def _compute_v2_attribution(open_positions: list[dict[str, Any]], closed_trades: list[dict[str, Any]]) -> dict[str, Any]:
    by_category: dict[str, dict[str, Any]] = {}
    by_strategy: dict[str, dict[str, Any]] = {}

    def _b(m: dict[str, dict[str, Any]], k: str) -> dict[str, Any]:
        return m.setdefault(
            k,
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

    for p in open_positions:
        cat = str(p.get("selection_bucket") or p.get("category") or "unknown")
        strat = str(p.get("strategy") or "unknown")
        for m, k in ((by_category, cat), (by_strategy, strat)):
            b = _b(m, k)
            b["open_count"] += 1
            b["unrealized_pnl"] = round(b["unrealized_pnl"] + float(p.get("unrealized_pnl") or 0.0), 6)
            b["fees"] = round(b["fees"] + float(p.get("entry_fee") or 0.0), 6)
            b["funding"] = round(b["funding"] + float(p.get("accumulated_funding") or 0.0), 6)
            b["open_notional"] = round(b["open_notional"] + float(p.get("actual_notional") or 0.0), 4)

    for t in closed_trades:
        cat = str(t.get("selection_bucket") or t.get("category") or "unknown")
        strat = str(t.get("strategy") or "unknown")
        pnl = float(t.get("net_pnl") or 0.0)
        fee = float(t.get("fee") or 0.0)
        fund = float(t.get("funding_paid") or 0.0)
        for m, k in ((by_category, cat), (by_strategy, strat)):
            b = _b(m, k)
            b["closed_count"] += 1
            if pnl > 0:
                b["wins"] += 1
            elif pnl < 0:
                b["losses"] += 1
            b["realized_net_pnl"] = round(b["realized_net_pnl"] + pnl, 6)
            b["fees"] = round(b["fees"] + fee, 6)
            b["funding"] = round(b["funding"] + fund, 6)

    return {"by_category": by_category, "by_strategy": by_strategy}


def _build_v2_snapshot(work_dir: Path, now: int) -> dict[str, Any]:
    v2_dir = work_dir / "v2"
    status = parse_status_md(v2_dir / "V2_STATUS.md")
    db_path = v2_dir / "v2_journal.db"
    events, db_error = _read_db_events(db_path)
    runner_state, universe_rows = _read_v2_tables(db_path)
    v2_log = _read_tail_lines(v2_dir / "v2.log", max_lines=100)
    agy_activity = _collect_local_agy_activity(work_dir)
    proc_info = inspect_runner_process(status.get("pid"), expected_module="v2.runner")

    cfg: dict[str, Any] = {}
    cfg_path = v2_dir / "v2_config.json"
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cfg = {}

    rec = _reconstruct_from_events(events)
    heartbeats = rec["heartbeats"]
    rejections_desc = rec["rejections"]
    stale_skips_desc = rec["stale_skips"]
    hourly_reports = rec["hourly_reports"]

    # Prefer atomic runner_state for live open positions (includes mark_price, unrealized_pnl, funding)
    if runner_state and isinstance(runner_state.get("spot_ledger"), dict) and isinstance(runner_state.get("futures_ledger"), dict):
        spot_st = runner_state["spot_ledger"]
        fut_st = runner_state["futures_ledger"]
        open_positions = []
        for l_st in (spot_st, fut_st):
            for p in (l_st.get("positions") or {}).values():
                opened_at = int(p.get("opened_at_utc") or 0)
                open_positions.append({**p, "opened_at_iso": _ts_to_iso(opened_at)})
        open_positions.sort(key=lambda x: x.get("opened_at_utc", 0), reverse=True)

        closed_trades_desc = []
        for l_st in (spot_st, fut_st):
            for t in l_st.get("closed_trades") or []:
                closed_at = int(t.get("closed_at_utc") or 0)
                opened_at = t.get("opened_at_utc")
                closed_trades_desc.append(
                    {
                        **t,
                        "closed_at_iso": _ts_to_iso(closed_at),
                        "opened_at_iso": _ts_to_iso(int(opened_at)) if opened_at else None,
                    }
                )
        closed_trades_desc.sort(key=lambda x: x.get("closed_at_utc", 0), reverse=True)

        spot_start = float(spot_st.get("starting_capital", 2500.0))
        fut_start = float(fut_st.get("starting_capital", 2500.0))
        spot_cash = float(spot_st.get("cash_usdt", spot_start))
        fut_cash = float(fut_st.get("cash_usdt", fut_start))
        spot_fees = float(spot_st.get("total_fees", 0.0))
        fut_fees = float(fut_st.get("total_fees", 0.0))
        fut_funding = float(fut_st.get("total_funding", 0.0))
        spot_halted = bool(spot_st.get("daily_halted", False))
        fut_halted = bool(fut_st.get("daily_halted", False))
    else:
        open_positions = rec["open_positions"]
        closed_trades_desc = rec["closed_trades"]
        spot_start = float(cfg.get("spot_starting_capital_usdt", 2500.0))
        fut_start = float(cfg.get("futures_starting_capital_usdt", 2500.0))
        spot_cash = fut_cash = spot_fees = fut_fees = fut_funding = None
        spot_halted = fut_halted = False

    latest_hb = heartbeats[-1] if heartbeats else None
    last_hb_ts = latest_hb["ts_utc"] if latest_hb else status.get("last_heartbeat_ts")
    hb_age_sec = max(0, now - last_hb_ts) if last_hb_ts is not None else None
    health_status, health_warning = _evaluate_health(proc_info, hb_age_sec)

    start_ts = int(runner_state["start_utc"]) if runner_state and runner_state.get("start_utc") else status.get("start_ts")
    end_ts = int(runner_state["end_utc"]) if runner_state and runner_state.get("end_utc") else status.get("scheduled_end_ts")
    if start_ts is not None and end_ts is not None and end_ts > start_ts:
        total_dur = end_ts - start_ts
        elapsed_sec = max(0, min(total_dur, now - start_ts))
        remaining_sec = max(0, end_ts - now)
        progress_pct = round(elapsed_sec / total_dur * 100.0, 2)
    else:
        total_dur = int(cfg.get("duration_sec", EXPERIMENT_DURATION_SEC))
        elapsed_sec = None
        remaining_sec = None
        progress_pct = None

    spot_series = [h["spot_equity"] for h in heartbeats if h.get("spot_equity") is not None]
    fut_series = [h["futures_equity"] for h in heartbeats if h.get("futures_equity") is not None]
    tot_series = [h["total_equity"] for h in heartbeats if h.get("total_equity") is not None]

    spot_open = [p for p in open_positions if p.get("ledger") == "SPOT"]
    fut_open = [p for p in open_positions if p.get("ledger") == "FUTURES"]
    spot_closed = [t for t in closed_trades_desc if t.get("ledger") == "SPOT"]
    fut_closed = [t for t in closed_trades_desc if t.get("ledger") == "FUTURES"]

    spot_exp = round(sum(float(p.get("actual_notional") or 0.0) for p in spot_open), 4)
    fut_exp = round(sum(float(p.get("actual_notional") or 0.0) for p in fut_open), 4)
    fut_margin = round(sum(float(p.get("margin_used") or 0.0) for p in fut_open), 4)
    comb_exp = round(spot_exp + fut_exp, 4)

    spot_metrics = _compute_ledger_metrics(
        "SPOT",
        spot_start,
        spot_series[-1] if spot_series else None,
        spot_series,
        spot_open,
        spot_closed,
        extra={
            "cash_usdt": spot_cash,
            "exposure_usdt": spot_exp,
            "total_fees": spot_fees,
            "daily_halted": spot_halted,
        },
    )
    fut_metrics = _compute_ledger_metrics(
        "FUTURES",
        fut_start,
        fut_series[-1] if fut_series else None,
        fut_series,
        fut_open,
        fut_closed,
        extra={
            "cash_usdt": fut_cash,
            "exposure_usdt": fut_exp,
            "locked_margin": fut_margin,
            "total_fees": fut_fees,
            "total_funding": fut_funding,
            "daily_halted": fut_halted,
        },
    )
    combined_metrics = _compute_ledger_metrics(
        "COMBINED",
        spot_start + fut_start,
        tot_series[-1] if tot_series else None,
        tot_series,
        open_positions,
        closed_trades_desc,
        extra={"exposure_usdt": comb_exp},
    )

    max_slots = int(cfg.get("max_concurrent_positions", 100))
    global_max_notional = float(cfg.get("global_max_notional_usdt", 5000.0))
    target_notional = float(cfg.get("target_notional_usdt", 50.0))
    univ_count = len(universe_rows) if universe_rows else (latest_hb.get("universe_count") if latest_hb else None)
    spot_elig_n = sum(1 for u in universe_rows if u.get("spot_eligible")) if universe_rows else (latest_hb.get("spot_universe") if latest_hb else None)
    fut_elig_n = sum(1 for u in universe_rows if u.get("futures_eligible")) if universe_rows else (latest_hb.get("futures_universe") if latest_hb else None)

    bucket_counts: dict[str, int] = {}
    for u in universe_rows:
        b = str(u.get("selection_bucket") or "alt")
        bucket_counts[b] = bucket_counts.get(b, 0) + 1
    if not bucket_counts and latest_hb and isinstance(latest_hb.get("universe_buckets"), dict):
        bucket_counts = latest_hb["universe_buckets"]

    attribution = _compute_v2_attribution(open_positions, closed_trades_desc)

    return {
        "experiment_version": "v2",
        "generated_at_utc": _ts_to_iso(now),
        "generated_at_ts": now,
        "paper_only": True,
        "read_only": True,
        "neural_model": {
            "status": status.get("neural_model") or "UNTRAINED",
            "detail": status.get("neural_model_detail") or "UNTRAINED (deterministic rule-based baseline; V2 100-market)",
        },
        "experiment": {
            "mode": status.get("mode") or "PAPER ONLY (V2 100-Market)",
            "start_utc": _ts_to_iso(start_ts) if start_ts else status.get("start_utc"),
            "start_ts": start_ts,
            "scheduled_end_utc": _ts_to_iso(end_ts) if end_ts else status.get("scheduled_end_utc"),
            "scheduled_end_ts": end_ts,
            "duration_sec": total_dur,
            "elapsed_sec": elapsed_sec,
            "remaining_sec": remaining_sec,
            "progress_pct": progress_pct,
            "blockers": status.get("blockers") or "None",
        },
        "heartbeat": {
            "last_utc": _ts_to_iso(last_hb_ts),
            "last_ts": last_hb_ts,
            "age_sec": hb_age_sec,
            "status": health_status,
            "warning": health_warning,
            "count": len(heartbeats),
        },
        "process": proc_info,
        "ledgers": {
            "SPOT": spot_metrics,
            "FUTURES": fut_metrics,
            "COMBINED": combined_metrics,
        },
        "v2": {
            "version": cfg.get("version", "2.0.0"),
            "target_notional_usdt": target_notional,
            "max_concurrent_positions": max_slots,
            "global_max_notional_usdt": global_max_notional,
            "open_slots_used": len(open_positions),
            "open_slots_pct": round(len(open_positions) / max_slots * 100.0, 2) if max_slots > 0 else 0.0,
            "global_notional_exposure": comb_exp,
            "global_exposure_pct": round(comb_exp / global_max_notional * 100.0, 2) if global_max_notional > 0 else 0.0,
            "cycle_count": runner_state.get("cycle_count") if runner_state else len(heartbeats),
            "latest_cycle": latest_hb,
            "attribution": attribution,
            "latest_hourly_report": hourly_reports[-1] if hourly_reports else None,
        },
        "equity_curve": _downsample_points(heartbeats, max_points=360),
        "positions": {
            "open": open_positions,
            "closed": closed_trades_desc,
        },
        "decisions": {
            "rejections": rejections_desc[:100],
            "rejection_count": len(rejections_desc),
            "stale_skips": stale_skips_desc[:100],
            "stale_skip_count": len(stale_skips_desc),
        },
        "universe": {
            "total_count": univ_count,
            "spot_count": spot_elig_n,
            "futures_count": fut_elig_n,
            "buckets": bucket_counts,
            "full_list_persisted": bool(universe_rows),
            "symbols": universe_rows,
            "persisted_note": (
                "Full 100-market category-stratified universe persisted atomically in v2_journal.db."
                if universe_rows
                else "Universe snapshot not yet persisted."
            ),
            "observed_symbols": sorted(
                rec["observed_symbols"].values(), key=lambda x: x["last_ts_utc"], reverse=True
            ),
            "observed_categories": bucket_counts or rec["category_counts"],
        },
        "db": {
            "readable": db_error is None,
            "error": db_error,
            "total_events": len(events),
        },
        "logs": {
            "lab_log": v2_log,
            "agy_activity": agy_activity,
        },
        "status_md": {
            "exists": status["exists"],
            "raw_lines": status["raw_lines"],
        },
        "v1_archive": _build_v1_archive_summary(work_dir, now),
    }


def build_dashboard_snapshot(work_dir: Path, now_utc: int | None = None) -> dict[str, Any]:
    """Build complete read-only dashboard JSON snapshot (V2 primary when present, else V1)."""
    now = int(time.time()) if now_utc is None else int(now_utc)
    v2_db = work_dir / "v2" / "v2_journal.db"
    v2_status = work_dir / "v2" / "V2_STATUS.md"
    if v2_db.exists() or v2_status.exists():
        return _build_v2_snapshot(work_dir, now)
    return _build_v1_snapshot(work_dir, now)


def query_events(
    work_dir: Path,
    event_type: str | None = None,
    ledger: str | None = None,
    limit: int = 100,
    version: str | None = None,
) -> dict[str, Any]:
    """Query events read-only from V2 or V1 SQLite journal with optional filters."""
    limit = max(1, min(int(limit), 500))
    v2_db = work_dir / "v2" / "v2_journal.db"
    if version == "v1":
        db_path = work_dir / "lab_journal.db"
    elif version == "v2" or v2_db.exists():
        db_path = v2_db
    else:
        db_path = work_dir / "lab_journal.db"

    events, db_error = _read_db_events(db_path)
    if db_error:
        return {"readable": False, "error": db_error, "events": [], "total_matching": 0}

    filtered = events
    if event_type and event_type.upper() != "ALL":
        et = event_type.upper()
        filtered = [e for e in filtered if e["event_type"].upper() == et]
    if ledger and ledger.upper() != "ALL":
        lg = ledger.upper()
        filtered = [e for e in filtered if e["ledger"].upper() == lg]

    filtered_desc = list(reversed(filtered))
    return {
        "readable": True,
        "error": None,
        "version": "v2" if db_path == v2_db else "v1",
        "total_matching": len(filtered_desc),
        "events": filtered_desc[:limit],
    }
