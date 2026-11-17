"""Read-only HTTP dashboard server (localhost-only by default, optional token auth)."""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import os
import signal
import sys
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from dashboard.reader import build_dashboard_snapshot, inspect_runner_process, parse_status_md, query_events

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
STATIC_INDEX = Path(__file__).resolve().parent / "static" / "index.html"


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class DashboardHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        work_dir: Path,
        auth_token: str | None = None,
        index_path: Path = STATIC_INDEX,
    ) -> None:
        self.work_dir = Path(work_dir)
        self.auth_token = auth_token.strip() if auth_token else None
        self.index_path = Path(index_path)
        self.started_at_ts = int(time.time())
        super().__init__(server_address, DashboardRequestHandler)


class DashboardRequestHandler(BaseHTTPRequestHandler):
    server: DashboardHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        # Compact structured stdout logging without leaking sensitive headers/tokens
        sys.stdout.write(
            f"[{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}] "
            f"{self.address_string()} - {format % args}\n"
        )
        sys.stdout.flush()

    def _send_security_headers(self, is_api: bool = False) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'",
        )
        if is_api:
            self.send_header("Cache-Control", "no-store, max-age=0")

    def _send_json(self, status: int, payload: dict[str, Any], extra_headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_security_headers(is_api=True)
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html_bytes: bytes) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html_bytes)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self._send_security_headers(is_api=False)
        self.end_headers()
        self.wfile.write(html_bytes)

    def _is_authorized(self) -> bool:
        expected = self.server.auth_token
        if not expected:
            return True

        auth_header = self.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            candidate = auth_header[7:].strip()
            if hmac.compare_digest(candidate, expected):
                return True

        token_header = self.headers.get("X-Dashboard-Token", "").strip()
        if token_header and hmac.compare_digest(token_header, expected):
            return True

        return False

    def _require_auth(self) -> bool:
        if self._is_authorized():
            return True
        self._send_json(
            HTTPStatus.UNAUTHORIZED,
            {"error": "unauthorized", "message": "Valid bearer token required"},
            extra_headers={"WWW-Authenticate": "Bearer"},
        )
        return False

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            if not self._require_auth():
                return
            if not self.server.index_path.exists():
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "ui_missing"})
                return
            self._send_html(self.server.index_path.read_bytes())
            return

        if path == "/api/health":
            if not self._require_auth():
                return
            v2_status_path = self.server.work_dir / "v2" / "V2_STATUS.md"
            v2_db_path = self.server.work_dir / "v2" / "v2_journal.db"
            if v2_status_path.exists() or v2_db_path.exists():
                status = parse_status_md(v2_status_path)
                proc = inspect_runner_process(status.get("pid"), expected_module="v2.runner")
                db_exists = v2_db_path.exists()
                exp_ver = "v2"
            else:
                status = parse_status_md(self.server.work_dir / "STATUS.md")
                proc = inspect_runner_process(status.get("pid"), expected_module="lab.runner")
                db_exists = (self.server.work_dir / "lab_journal.db").exists()
                exp_ver = "v1"
            self._send_json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "service": "neural-strategy-lab-dashboard",
                    "experiment_version": exp_ver,
                    "read_only": True,
                    "paper_only": True,
                    "auth_required": bool(self.server.auth_token),
                    "uptime_sec": max(0, int(time.time()) - self.server.started_at_ts),
                    "db_present": db_exists,
                    "runner_alive": proc["alive"],
                },
            )
            return

        if path == "/api/dashboard":
            if not self._require_auth():
                return
            snap = build_dashboard_snapshot(self.server.work_dir)
            snap["security"] = {
                "host": self.server.server_address[0],
                "port": self.server.server_address[1],
                "localhost_only": _is_loopback(self.server.server_address[0]),
                "token_auth_enabled": bool(self.server.auth_token),
                "read_only_sqlite": True,
                "paper_only": True,
            }
            self._send_json(HTTPStatus.OK, snap)
            return

        if path == "/api/events":
            if not self._require_auth():
                return
            event_type = qs.get("type", ["ALL"])[0]
            ledger = qs.get("ledger", ["ALL"])[0]
            version = qs.get("version", [None])[0]
            try:
                limit = int(qs.get("limit", ["100"])[0])
            except ValueError:
                limit = 100
            data = query_events(
                self.server.work_dir,
                event_type=event_type,
                ledger=ledger,
                limit=limit,
                version=version,
            )
            self._send_json(HTTPStatus.OK, data)
            return

        if path == "/api/logs":
            if not self._require_auth():
                return
            snap = build_dashboard_snapshot(self.server.work_dir)
            self._send_json(HTTPStatus.OK, snap["logs"])
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found", "path": path})

    def do_POST(self) -> None:  # noqa: N802
        self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "read_only_service"})

    def do_PUT(self) -> None:  # noqa: N802
        self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "read_only_service"})

    def do_DELETE(self) -> None:  # noqa: N802
        self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "read_only_service"})


def create_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    work_dir: str | Path = "/root/neural-strategy-lab",
    auth_token: str | None = None,
    allow_unauth_nonlocal: bool = False,
) -> DashboardHTTPServer:
    if allow_unauth_nonlocal:
        raise ValueError("Unauthenticated non-loopback dashboard access is permanently disabled")
    if not _is_loopback(host) and not auth_token and not allow_unauth_nonlocal:
        raise ValueError(
            "Refusing to bind non-loopback address without DASHBOARD_AUTH_TOKEN. "
            "Use 127.0.0.1 with SSH port-forwarding or set DASHBOARD_AUTH_TOKEN."
        )
    return DashboardHTTPServer((host, port), Path(work_dir), auth_token=auth_token)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Neural Strategy Lab Read-Only Dashboard")
    parser.add_argument("--host", default=os.environ.get("DASHBOARD_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.environ.get("DASHBOARD_PORT", DEFAULT_PORT)))
    parser.add_argument("--work-dir", default=os.environ.get("LAB_WORK_DIR", "/root/neural-strategy-lab"))
    parser.add_argument(
        "--token",
        default=os.environ.get("DASHBOARD_AUTH_TOKEN") or os.environ.get("DASHBOARD_TOKEN", ""),
    )
    parser.add_argument("--allow-unauth-nonlocal", action="store_true")
    args = parser.parse_args(argv)

    token = args.token.strip() or None
    try:
        srv = create_server(
            host=args.host,
            port=args.port,
            work_dir=args.work_dir,
            auth_token=token,
            allow_unauth_nonlocal=args.allow_unauth_nonlocal,
        )
    except ValueError as exc:
        sys.stderr.write(f"SECURITY BLOCK: {exc}\n")
        return 2

    def _handle_sig(_signum: int, _frame: Any) -> None:
        srv.shutdown()

    signal.signal(signal.SIGTERM, _handle_sig)
    signal.signal(signal.SIGINT, _handle_sig)

    actual_host, actual_port = srv.server_address
    sys.stdout.write(
        f"Neural Strategy Lab Dashboard listening on http://{actual_host}:{actual_port} "
        f"(read-only, paper-only, auth={'enabled' if token else 'disabled'})\n"
    )
    sys.stdout.flush()
    try:
        srv.serve_forever(poll_interval=0.5)
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
