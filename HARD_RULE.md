# HARD RULE — MANDATORY EXPERIMENT-TO-LIVE LIFECYCLE (IMMUTABLE)

**STATUS: LOCKED / READ-ONLY (`chmod 444`). NO AGENT MAY MODIFY, BYPASS, OR OVERRIDE THIS RULE.**

Every agent working on `/root/neural-strategy-lab` MUST strictly follow this exact 4-phase lifecycle in order:

1. **Phase 1 — 72-Hour Paper Trading**:
   - Run the V2 100-market paper-only experiment for **72 hours**.
   - Collect full market data, heartbeats, and closed paper trades (`POSITION_CLOSED` events).

2. **Phase 2 — 6-Hour Re-Evaluation, Upgrade & Fix**:
   - Immediately after the 72-hour paper run completes, perform a **6-hour** re-evaluation, upgrade, and fix window (instead of 1 day):
     - Analyze all losing and winning paper trades via `LossLessonAnalyzer`.
     - Train `ModifiedKronosPredictor.fit_time_split()` chronologically and validate out-of-sample.
     - Upgrade `MasterStrategy` and fix any identified bugs or edge cases.

3. **Phase 3 — 12-Hour Paper Re-Emphasis**:
   - Run the upgraded `MasterStrategy` + `ModifiedKronosPredictor` on **paper trading for 12 hours** to re-emphasize and verify stability, risk controls, and performance.

4. **Phase 4 — Activate Live Trading**:
   - Only after Phase 1 (72h paper) → Phase 2 (6h re-evaluation/upgrade/fix) → Phase 3 (12h paper re-emphasis) are all completed and verified, **activate live trading**.
