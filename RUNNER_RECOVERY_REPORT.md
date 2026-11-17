# Runner Recovery Report

**As of:** 2026-11-17 17:12 UTC

## Verified state

- Status file records PID `77825`, start `2026-11-17T16:47:50Z`, scheduled end `2026-11-20T16:47:50Z`, cycle 17, heartbeat `17:10:50Z`.
- Host process listing did not expose the runner; `tmux ls` could not access `/tmp/tmux-0/default` (`Operation not permitted`). This is a visibility limitation, not evidence that the process is stopped.
- The journal was actively changing during the audit. Read-only checks found `integrity_check=ok`, latest checkpoint 10 seconds old, 17 heartbeats, 100 distinct symbols, 17 open positions, zero closed trades, and approximately 849.27 USDT exposure.
- Recent runner log entries show one-minute cycles through 17:10:57Z. No second matching runner was visible. The SQLite journal stayed consistent and the original experiment start/end timestamps were preserved.
- Existing WAL and SHM sidecars were left untouched. The database was opened read-only for inspection; no reset, checkpoint, restore, or migration was applied.

## Recovery safeguards implemented

- V2 runner now obtains a nonblocking OS file lock before journal initialization. A second process fails closed; the lock releases on normal shutdown and initialization failure.
- Existing restart tests verify saved experiment clock and open positions survive restart, and a second concurrent instance is rejected.

## Recovery decision

No restart was attempted because the database/log were actively advancing and tmux/process control was unavailable. Restarting could duplicate the active runner. The new singleton lock will take effect at the next operator-managed restart; the currently running process predates the lock and does not hold it.

## Continuity and phase policy

The recorded clock was not rewritten. Journal evidence establishes activity from the recorded start through the latest sampled heartbeat, but cannot prove uninterrupted process continuity between every event. If a gap is later established, keep the original timestamps and record exact gap bounds; apply a documented recovery policy with AGY/operator review before claiming a valid continuous 72-hour run.

## Operator action required

On the VPS with process visibility, inspect PID `77825`, tmux `neural_lab_v2_72h`, dashboard session, and the singleton lock before any restart. Use the existing checkpoint and preserve the journal. Do not start a second runner while the existing status/log/checkpoint is advancing.
