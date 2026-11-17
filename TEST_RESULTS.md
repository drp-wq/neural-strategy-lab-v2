# Test Results

**Run date:** 2026-11-17 UTC  
**V2 suite:** `.venv/bin/python -m pytest -q v2/tests` → **22 passed in 0.95s**.  
**Full repository:** `.venv/bin/python -m pytest -q` → **32 passed, 1 failed in 1.62s**.

## Failure

`lab/tests/test_dashboard.py::test_http_endpoints_auth_and_ui_smoke` could not create an HTTP listener. Python socket creation returned `PermissionError: [Errno 1] Operation not permitted` in this restricted execution environment. No application assertion was reached for this test. V2 dashboard access-control behavior is covered by a handler-level test that does not bind a socket.

## Added regression coverage

- Concurrent order attempts remain within the global 100-slot and 5,000 USDT limits.
- Invalid, non-finite, or zero-step exchange filters reject safely.
- A second runner instance is rejected by the singleton lock; saved clock and positions survive sequential restart.
- Dashboard UI, health, and data route authentication is enforced; unauthenticated non-loopback override is blocked.
- Webhook destinations and redirects fail safely.
- Existing tests also cover stale quotes, simulated fees/funding/PnL, liquidation math, daily drawdown halt, duplicate symbols, paper-only adapter barriers, and model `UNTRAINED` status.

## Verification limits

No live listener test, live market API request, process restart, or 72-hour completion test was performed in the restricted audit session. Do not treat the full suite as passing until the socket-dependent V1 smoke test passes in an authorized runtime.
