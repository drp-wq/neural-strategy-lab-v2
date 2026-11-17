# Neural Strategy Lab — Dashboard Status

- **Status**: `RUNNING` (Read-only, Paper-only)
- **Tmux Session**: `neural_dashboard` (Dashboard PID `76827`, `.venv/bin/python -m dashboard.server --host 127.0.0.1 --port 8765`)
- **Local URL**: `http://127.0.0.1:8765`
- **Bind Address**: `127.0.0.1:8765` (localhost-only by default; non-loopback bind is blocked unless `DASHBOARD_AUTH_TOKEN` / `DASHBOARD_TOKEN` is set)
- **Optional Remote Auth**: Set `DASHBOARD_AUTH_TOKEN` (or `DASHBOARD_TOKEN` / `--token`) before starting `dashboard.server` to require `Authorization: Bearer <token>` or `X-Dashboard-Token: <token>` on `/api/dashboard`, `/api/events`, and `/api/logs` (`/api/health` remains a minimal unauthenticated liveness check without sensitive paths).
- **Existing 72h Paper Runner**: PID `76467` (`.venv/bin/python -m lab.runner`) — untouched and running (`HEALTHY`, verified via `ps` and `/api/dashboard`).

## Implemented Files (Isolated to `/root/neural-strategy-lab`)
- `dashboard/__init__.py`
- `dashboard/reader.py` — Read-only SQLite URI (`?mode=ro`) event reconstruction, `STATUS.md` & `lab.log` parsing, isolated project-local `dashboard_agy.log` inspection, and honest `"not yet available"` reporting when closed-trade metrics or full universe lists are absent.
- `dashboard/server.py` — `ThreadingHTTPServer` read-only API + static UI server with security headers (`X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, `CSP`, `Cache-Control: no-store`), optional constant-time Bearer token auth, and `405` rejection on write HTTP methods.
- `dashboard/static/index.html` — Responsive dark cyberpunk UI (violet/cyan/lime accents), live 1s 72h countdown & heartbeat age clocks, stale/offline warning banner, prominent `NEURAL STATUS: UNTRAINED` badge, SVG equity curve, 5 responsive tabs (`Overview`, `Positions & Trades`, `Universe & Rejections`, `Logs & AGY`, `Journal & Security`), and token unlock modal.
- `lab/tests/test_dashboard.py` — Unit, endpoint, security, and UI smoke tests.

## Verified Endpoints (`curl` checked)
- `GET /` — `200 OK` (`52606` bytes HTML)
- `GET /api/health` — `200 OK` (`{"status":"ok","service":"neural-strategy-lab-dashboard","read_only":true,"paper_only":true,"auth_required":false,"db_present":true,"runner_alive":true}`)
- `GET /api/dashboard` — `200 OK` (live snapshot: `13` SQLite events, `2` open positions, `0` closed trades, `SPOT` & `FUTURES` independent ledgers, `UNTRAINED` neural status)
- `GET /api/events` — `200 OK` (filterable read-only event journal)
- `GET /api/logs` — `200 OK` (`lab.log` tail + isolated project-local `dashboard_agy.log`)

## Verified Tests
- `.venv/bin/python -m pytest -v`: `11 passed in 1.16s` (`6` dashboard/API/security/UI smoke tests in `lab/tests/test_dashboard.py` + `5` runner tests in `lab/tests/test_lab.py`).

## Safe SSH Port-Forwarding (Android Termius)
1. In **Termius**, open **Port Forwarding** -> **New Rule** -> **Local**.
2. Configure:
   - **Host (from)**: `127.0.0.1`
   - **Port (from)**: `8765`
   - **Destination Host**: `127.0.0.1`
   - **Destination Port**: `8765`
3. Connect to this server via SSH in Termius with the rule active (or from a terminal: `ssh -N -L 8765:127.0.0.1:8765 root@<YOUR_SERVER_IP>`).
4. Open your Android browser and navigate to `http://127.0.0.1:8765`.
5. Keep the dashboard bound to `127.0.0.1` unless you explicitly set `DASHBOARD_AUTH_TOKEN` and put TLS in front of it.
