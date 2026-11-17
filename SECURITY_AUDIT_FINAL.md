# Security Audit Final — Neural Strategy Lab V2

**Audit timestamp:** 2026-11-17 17:12 UTC  
**Scope:** `/root/neural-strategy-lab` only. `HARD_RULE.md` was read and left unchanged (mode `0444`).  
**Execution mode:** Paper only. Live execution remains disabled and requires a separate future operator authorization after every AGY phase.

## Verified results

- The V2 journal passed SQLite `PRAGMA integrity_check` (`ok`) in read-only mode. The latest sampled checkpoint was 10 seconds old, cycle 17, with the original experiment clock `1794934070` to `1795193270` (2026-11-17 16:47:50Z to 2026-11-20 16:47:50Z).
- Latest sampled journal: 100 distinct universe symbols; 17 open simulated positions (3 Spot, 14 Futures); 17 `POSITION_OPENED`, zero `POSITION_CLOSED`; gross exposure about 849.27 USDT, below the 5,000 USDT ceiling. Independent ledger equity reconciled exactly to cash + margin + unrealized PnL and to realized + unrealized PnL.
- No V2 source files contain Binance order-submission endpoints. `v2/live_adapter.py` is a hard-disabled stub with no signing, credential access, or network write implementation. `UNTRAINED` remains the model label.
- Added a cross-ledger lock around global capacity checks, fail-closed validation for non-finite/invalid exchange rules, invalid route rejection, and a process singleton file lock for safe future runner restarts.
- Dashboard now requires configured token authentication for the UI and health endpoint as well as data endpoints. The unauthenticated non-loopback override is rejected. The dashboard remains read-only.
- Optional alerts now reject non-HTTPS/private webhook destinations, reject malformed Telegram identifiers, and disable redirects.
- Regression coverage added for global-cap concurrent attempts, invalid filters, singleton lock, dashboard route authentication, and alert destination safeguards.
- V2 suite: **22 passed**. Full repository suite: **32 passed, 1 failed**. The failure is the existing V1 dashboard HTTP smoke test, blocked because this execution sandbox denies socket creation (`PermissionError: Operation not permitted`).

## Findings and fixes

| Severity | Finding | Evidence | Status |
|---|---|---|---|
| High | Global exposure and slot checks were unsynchronized across simultaneous open attempts. | Coordinator previously checked and opened without a lock. | Fixed in source; concurrent regression passes. Running process has not loaded this patch yet. |
| High | Runner startup lacked singleton protection. | No lock file was held by the currently writing process; no host process/tmux visibility available. | Fixed for subsequent starts; do not restart from this audit session. |
| High | Dashboard static UI and `/api/health` were unauthenticated even when a token was configured. | Handler routes served those paths before `_require_auth()`. | Fixed and handler-level regression passes. Existing dashboard process has not loaded this patch. |
| Medium | Invalid/non-finite exchange filters and zero quantity steps could be accepted. | `compute_target_qty` did not require finite fields or positive step size. | Fixed; regression passes. |
| Medium | Alert webhook could use arbitrary schemes/hosts and follow redirects. | Environment URL passed directly to `requests.post`. | Fixed; regression passes. |
| Medium | Process/tmux visibility is restricted. | `ps` cannot see the claimed PID; tmux socket returned `Operation not permitted`; journal and log continued writing during audit. | Unresolved environment limitation. Current activity is inferred from recent journal/log updates, not independently attached to the process. |
| Medium | Project and journal/log permissions are world-readable (`0755` directories, `0644` files). | `stat` inspection. No `.env` files found at project root or V2 root. | Residual hardening issue: restrict runtime state/logs to owner-only during a safe maintenance window. |
| Medium | Dashboard auth is optional for loopback binding. | Default server allows localhost without token; remote access is blocked unless token provided. | Local-only default is consistent with the mission, but localhost clients share access. Keep remote access behind SSH forwarding or authenticated TLS proxy. |

## Not verified / residual risk

- No API credential values were read. No matching Binance/live credential variable names were present in this process environment. API key permission scope and withdrawal restrictions cannot be proven from this environment.
- No safe dashboard listener test was possible due to socket restrictions. Dashboard code binds localhost by default and rejects unauthenticated non-loopback access.
- Restart recovery was covered by unit tests, but no real runner restart was performed. The existing runner process did not hold the new lock at audit time; the lock protects only the next process start.
- Daily drawdown, stop, take-profit, liquidation, timeout, stale data, and duplicate handling have existing tests, but the current live paper run has not generated closed trades yet; actual operational outcomes remain unobserved.
- Exact sampled exchange filters and public endpoint responses were not re-fetched during the security audit. Historical snapshots and mock tests are not proof of current filters.
- The market scanner has a persisted universe of 100 distinct markets. Strategy routing selects one ledger per symbol; independent strategy evaluation on every eligible Spot and Futures market is not verified.
- No continuous 72-hour run has completed. Phase 1 remains active; no Phase 2/3 work or live readiness approval is authorized yet.

## Live gate

**CLOSED.** No live trading is authorized or implemented. All AGY phases must complete and pass first, then a separate explicit operator authorization is required. This audit does not activate live mode.
