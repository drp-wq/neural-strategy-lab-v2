# Neural Strategy Lab — Final Codex Handoff

**Project Scope**: `/root/neural-strategy-lab` ONLY (Paper-only during Phases 1–3; no live keys active).
**Final Audit Timestamp**: `2026-11-17T17:01:28Z`

---

## 1. Mandatory Lifecycle Rule (Immutable)
- **[HARD_RULE.md](file:///root/neural-strategy-lab/HARD_RULE.md)** (`chmod 444`, read-only):
  1. **Phase 1 — 72-Hour Paper Trading** (currently running)
  2. **Phase 2 — 6-Hour Re-Evaluation, Upgrade & Fix**
  3. **Phase 3 — 12-Hour Paper Re-Emphasis**
  4. **Phase 4 — Activate Live Trading** (only after Phases 1–3 complete & pass)

---

## 2. Live Processes & Dashboard
- **V2 72h Paper Runner**:
  - Tmux: `neural_lab_v2_72h` | PID: `77353` (`.venv/bin/python -m v2.runner`)
  - Journal: `/root/neural-strategy-lab/v2/v2_journal.db`
  - Status: `/root/neural-strategy-lab/v2/V2_STATUS.md`
  - Log: `/root/neural-strategy-lab/v2/v2_lab.log`
- **Read-Only Dashboard**:
  - Tmux: `neural_dashboard` | PID: `77399` (`.venv/bin/python -m dashboard.server --host 127.0.0.1 --port 8765`)
  - URL: `http://127.0.0.1:8765` (integrated with V2; V1 archived separately in `lab_journal.db` & `STATUS.md`).

---

## 3. Implemented V2 & Upgrade Modules
- Core V2: `v2/config.py`, `v2/collectors.py`, `v2/features.py`, `v2/strategies.py`, `v2/ledger.py`, `v2/journal.py`, `v2/runner.py`.
- Prediction & Master Strategy:
  - `v2/kronos_engine.py`: `ModifiedKronosPredictor` (chronological `fit_time_split()`, `UNTRAINED` until trained, `apply_lesson_calibration()`).
  - `v2/master_strategy.py`: `LossLessonAnalyzer` + `MasterStrategy`.
- Phase 2–4 Upgrades:
  - `v2/phase2_upgrade.py`: `run_phase2_upgrade()` reads `POSITION_CLOSED` events and writes `phase2_lessons.json`.
  - `v2/live_adapter.py`: `LiveExecutionAdapter` (gated behind `LIVE_TRADING_ENABLED=1` & API credentials, with `activate_kill_switch()`).
  - `v2/alerts.py`: `send_alert()` (optional `ALERT_WEBHOOK_URL` or Telegram env vars).

---

## 4. Verified Tests
- `.venv/bin/python -m pytest -v v2/tests/` → **18 passed in 1.58s** (`test_v2.py`, `test_v2_dashboard.py`, `test_kronos_master.py`, `test_upgrades.py`).

---

## 5. Next Actions for Codex
1. Let **Phase 1** (`neural_lab_v2_72h`, PID `77353`) complete its 72h paper run (`2026-11-20T16:47:50+00:00`).
2. Execute **Phase 2 (6h)**: Run `v2/phase2_upgrade.py`, train `ModifiedKronosPredictor.fit_time_split()`, and wire `MasterStrategy.evaluate()` into `v2/runner.py`.
3. Execute **Phase 3 (12h)**: Run the upgraded `MasterStrategy` on paper for 12 hours and verify stability.
4. Execute **Phase 4**: Only after Phases 1–3 pass, enable `LiveExecutionAdapter` (`LIVE_TRADING_ENABLED=1`).
