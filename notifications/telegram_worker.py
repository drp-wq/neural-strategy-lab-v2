"""Read-only, 30-minute Telegram digest with immediate critical paper-risk alerts."""
import json
import os
import sqlite3
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / 'v2/v2_journal.db'
STATE = ROOT / 'v2/.telegram_alert_cursor.json'
CRITICAL = {'DAILY_HALT', 'RISK_HALT', 'LIQUIDATION', 'KILL_SWITCH', 'EXPOSURE_HALT'}
OPEN = {'POSITION_OPENED'}
CLOSE = {'POSITION_CLOSED'}
QUALITY = {'STALE_DATA_SKIP'}
INTERVAL = 1800


def read_events(after, until=None):
    con = sqlite3.connect(f'file:{DB}?mode=ro', uri=True, timeout=5)
    try:
        sql = 'SELECT rowid,event_type,ledger,symbol,payload_json,ts_utc FROM events WHERE rowid>?'
        args = [after]
        if until is not None:
            sql += ' AND ts_utc<?'
            args.append(until)
        return con.execute(sql + ' ORDER BY rowid ASC LIMIT 500', args).fetchall()
    finally:
        con.close()


def last_rowid():
    con = sqlite3.connect(f'file:{DB}?mode=ro', uri=True, timeout=5)
    try:
        return con.execute('SELECT COALESCE(MAX(rowid),0) FROM events').fetchone()[0]
    finally:
        con.close()


def save(state):
    temp = STATE.with_suffix('.tmp')
    temp.write_text(json.dumps(state))
    os.chmod(temp, 0o600)
    temp.replace(STATE)


def send(token, chat, message):
    response = requests.post('https://api.telegram.org/bot' + token + '/sendMessage', json={
        'chat_id': chat, 'text': message[:3900], 'disable_web_page_preview': True,
    }, timeout=10, allow_redirects=False)
    response.raise_for_status()
    if not response.json().get('ok'):
        raise RuntimeError('Telegram rejected message')


def format_critical(row):
    _, kind, ledger, symbol, raw, stamp = row
    try:
        details = json.loads(raw)
    except ValueError:
        details = {}
    reason = str(details.get('reason', details.get('message', 'Review paper journal')))[:150]
    date = datetime.fromtimestamp(stamp, timezone.utc).strftime('%H:%M UTC')
    return f'🚨 NEURAL LAB • CRITICAL\n━━━━━━━━━━━━━━━━━━\n🛑 {kind.replace("_", " ")}\n📊 {ledger} / {symbol}\n🧪 PAPER ONLY\n📝 {reason}\n🕒 {date}'


def format_digest(rows, start, end):
    counts = Counter(row[1] for row in rows)
    opens = [r for r in rows if r[1] in OPEN]
    closes = [r for r in rows if r[1] in CLOSE]
    if not opens and not closes and not any(r[1] in QUALITY for r in rows):
        return None
    stamp = lambda ts: datetime.fromtimestamp(ts, timezone.utc).strftime('%H:%M')
    lines = ['🧠 NEURAL STRATEGY LAB V2', '━━━━━━━━━━━━━━━━━━', '📋 30-MINUTE FILTERED DIGEST', f'🕒 {stamp(start)}–{stamp(end)} UTC', '🧪 PAPER ONLY • NO LIVE ORDERS', '']
    for name, group in [('SPOT', 'SPOT'), ('FUTURES', 'FUTURES')]:
        oo = [r for r in opens if group in r[2].upper()]
        cc = [r for r in closes if group in r[2].upper()]
        lines.append(f'📊 {name}: 🟢 {len(oo)} opened • 🏁 {len(cc)} closed')
    if closes:
        lines.append('')
        lines.append('🏁 CLOSED POSITIONS (up to 8)')
        for r in closes[:8]:
            try:
                data = json.loads(r[4])
            except ValueError:
                data = {}
            pnl = next((data[k] for k in ('net_pnl', 'realized_pnl', 'pnl_usdt', 'pnl') if k in data), None)
            lines.append(f'• {r[3]} ({r[2]})' + (f' | PnL {pnl}' if pnl is not None else ''))
        if len(closes) > 8:
            lines.append(f'• +{len(closes)-8} more closures in journal')
    if opens:
        lines.append('')
        lines.append('🟢 NEW PAPER POSITIONS')
        top = Counter(r[3] for r in opens).most_common(5)
        lines.append(' • '.join(f'{symbol} ×{n}' for symbol, n in top))
        if len(top) < len(set(r[3] for r in opens)):
            lines.append('Additional symbols in journal')
    if counts['STALE_DATA_SKIP']:
        lines += ['', f'⚠️ Data quality: {counts["STALE_DATA_SKIP"]} stale-data skips (aggregated)']
    lines += ['', '🔕 Heartbeats and routine reports suppressed', '━━━━━━━━━━━━━━━━━━']
    return '\n'.join(lines)


def process_once(state, token, chat, now):
    # Critical alerts bypass the digest; persist their cursor only after successful delivery.
    while True:
        batch = read_events(state['critical_rowid'])
        if not batch:
            break
        for row in batch:
            if row[1] in CRITICAL:
                send(token, chat, format_critical(row))
            state['critical_rowid'] = row[0]
            save(state)
        if len(batch) < 500:
            break
    cutoff = now // INTERVAL * INTERVAL
    if cutoff <= state['digest_end']:
        return
    # Digest complete time windows only, with no replay of old history on activation.
    while state['digest_end'] < cutoff:
        end = state['digest_end'] + INTERVAL
        rows = []
        cursor = state['digest_rowid']
        while True:
            batch = read_events(cursor, end)
            if not batch:
                break
            rows.extend(batch)
            cursor = batch[-1][0]
            if len(batch) < 500:
                break
        message = format_digest(rows, state['digest_end'], end)
        if message:
            send(token, chat, message)
        # Do not advance past future-timestamped events; a later window will read them.
        state['digest_rowid'] = cursor
        state['digest_end'] = end
        save(state)


def main():
    if STATE.exists():
        state = json.loads(STATE.read_text())
        if not all(k in state for k in ('digest_rowid', 'critical_rowid', 'digest_end')):
            # Migrate old cursor without replaying historic trades.
            current = last_rowid()
            state = {'digest_rowid': current, 'critical_rowid': current, 'digest_end': int(time.time()) // INTERVAL * INTERVAL}
            save(state)
    else:
        current = last_rowid()
        state = {'digest_rowid': current, 'critical_rowid': current, 'digest_end': int(time.time()) // INTERVAL * INTERVAL}
        save(state)
    while True:
        token = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
        chat = os.getenv('TELEGRAM_CHAT_ID', '').strip()
        if token and chat:
            try:
                process_once(state, token, chat, int(time.time()))
            except Exception as exc:
                print('Telegram dispatch retry:', type(exc).__name__, flush=True)
        time.sleep(15)

if __name__ == '__main__':
    main()
