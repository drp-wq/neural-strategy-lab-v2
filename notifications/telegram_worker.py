"""Persistent Telegram paper-alert worker; no trade execution or credential access."""
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / 'v2/v2_journal.db'
CURSOR = ROOT / 'v2/.telegram_alert_cursor.json'
EVENTS = {'POSITION_OPENED', 'POSITION_CLOSED', 'DAILY_HALT', 'RISK_HALT', 'LIQUIDATION'}


def format_event(event):
    kind, ledger, symbol, payload, stamp = event
    icons = {'POSITION_OPENED': '🟢', 'POSITION_CLOSED': '🏁', 'DAILY_HALT': '🛑', 'RISK_HALT': '🚨', 'LIQUIDATION': '⚠️'}
    title = kind.replace('_', ' ')
    data = json.loads(payload)
    lines = [f'🧠 NEURAL STRATEGY LAB V2', '━━━━━━━━━━━━━━━━━━', f'{icons.get(kind,"🔔")} {title}', f'📈 {symbol}  •  {ledger}', '🧪 PAPER ONLY — NOT REAL ORDERS']
    for key in ('side', 'entry_price', 'exit_price', 'actual_notional', 'net_pnl', 'reason'):
        if key in data and isinstance(data[key], (str, int, float)):
            lines.append(f'▫️ {key.replace("_", " ").title()}: {str(data[key])[:80]}')
    lines += ['━━━━━━━━━━━━━━━━━━', '🕒 ' + datetime.fromtimestamp(stamp, timezone.utc).strftime('%Y-%m-%d %H:%M UTC')]
    return '\n'.join(lines)


def send(token, chat, msg):
    if not token or not chat:
        return False
    r = requests.post(f'https://api.telegram.org/bot{token}/sendMessage', json={'chat_id': chat, 'text': msg, 'disable_web_page_preview': True}, timeout=8, allow_redirects=False)
    r.raise_for_status()
    if not r.json().get('ok'):
        raise RuntimeError('Telegram rejected message')
    return True


def read_events(after):
    con = sqlite3.connect(f'file:{DB}?mode=ro', uri=True, timeout=5)
    try:
        return con.execute('SELECT rowid,event_type,ledger,symbol,payload_json,ts_utc FROM events WHERE rowid>? ORDER BY rowid ASC LIMIT 100', (after,)).fetchall()
    finally:
        con.close()


def main():
    token = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
    chat = os.getenv('TELEGRAM_CHAT_ID', '').strip()
    if not token or not chat:
        print('Telegram credentials absent: worker waiting; no messages will be sent', flush=True)
    cursor = 0
    if CURSOR.exists():
        try:
            cursor = int(json.loads(CURSOR.read_text())['rowid'])
        except (ValueError, KeyError, json.JSONDecodeError):
            raise RuntimeError('Invalid alert cursor; fail closed')
    else:
        # Start at the current end; do not spam old trades on first activation.
        rows = read_events(0)
        while rows:
            cursor = rows[-1][0]
            rows = read_events(cursor)
        CURSOR.write_text(json.dumps({'rowid': cursor}))
        os.chmod(CURSOR, 0o600)
    while True:
        token = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
        chat = os.getenv('TELEGRAM_CHAT_ID', '').strip()
        if token and chat:
            for rowid, kind, ledger, symbol, payload, stamp in read_events(cursor):
                if kind in EVENTS:
                    try:
                        send(token, chat, format_event((kind, ledger, symbol, payload, stamp)))
                    except Exception as exc:
                        print('Telegram delivery failed:', type(exc).__name__, flush=True)
                        break
                cursor = rowid
                tmp = CURSOR.with_suffix('.tmp')
                tmp.write_text(json.dumps({'rowid': cursor}))
                os.chmod(tmp, 0o600)
                tmp.replace(CURSOR)
        time.sleep(15)

if __name__ == '__main__':
    main()
