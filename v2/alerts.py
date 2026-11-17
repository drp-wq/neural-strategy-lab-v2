"""Lightweight optional webhook/Telegram alert helper for V2 milestones and halts."""

from __future__ import annotations

import os
import ipaddress
from urllib.parse import urlparse
import requests


def _is_safe_https_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            return False
        host = parsed.hostname.rstrip(".").lower()
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            return host not in {"localhost", "localhost.localdomain"} and "." in host
        return addr.is_global
    except ValueError:
        return False


def send_alert(title: str, message: str, timeout_sec: int = 5) -> bool:
    """Send alert via ALERT_WEBHOOK_URL or Telegram env vars; no-op if unconfigured."""
    text = f"[{title}] {message}"
    webhook = os.getenv("ALERT_WEBHOOK_URL", "").strip()
    if webhook:
        if not _is_safe_https_url(webhook):
            return False
        try:
            resp = requests.post(webhook, json={"title": title, "message": message, "text": text}, timeout=timeout_sec, allow_redirects=False)
            return resp.ok
        except requests.RequestException:
            return False

    tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    tg_chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if tg_token and tg_chat:
        if not tg_token.isalnum() or not tg_chat.lstrip("-").isdigit():
            return False
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{tg_token}/sendMessage",
                json={"chat_id": tg_chat, "text": text},
                timeout=timeout_sec,
                allow_redirects=False,
            )
            return resp.ok
        except requests.RequestException:
            return False

    return False
