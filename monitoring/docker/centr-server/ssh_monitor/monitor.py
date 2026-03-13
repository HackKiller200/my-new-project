#!/usr/bin/env python3
import requests
import json
import time
import re
import os
from datetime import datetime
TG_BOT_TOKEN = ''
TG_CHAT_ID = ''

LOKI_URL = 'http://localhost:3100'
POLL_INTERVAL = 10
STATE_FILE = '/opt/ssh_monitor/last_timestamp.txt'

LOKI_QUERY = '{job="authlog"} |= "sshd" |~ "(Accepted|Failed|Invalid|disconnected)" !~ "Server listening"'

def get_last_timestamp():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            try:
                return int(f.read().strip())
            except ValueError:
                return 0
    return 0

def save_last_timestamp(ts):
    with open(STATE_FILE, 'w') as f:
        f.write(str(ts))

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        resp = requests.post(url, json=payload, timeout=5)
        if not resp.json().get('ok'):
            print(f"[!] Telegram error: {resp.text}")
    except Exception as e:
        print(f"[!] Ошибка отправки: {e}")

def parse_ssh_log(line):
    match_success = re.search(r'Accepted (\S+) for (.+?) from (\d+\.\d+\.\d+\.\d+)', line)
    if match_success:
        method = match_success.group(1)
        user = match_success.group(2)
        ip = match_success.group(3)
        return "SUCCESS", ip, user, f"Метод: {method}"
    match_fail = re.search(r'Failed password for (?:invalid user )?(.+?) from (\d+\.\d+\.\d+\.\d+)', line)
    if match_fail:
        user = match_fail.group(1)
        ip = match_fail.group(2)
        return "FAILED", ip, user, "Неверный пароль"
    match_invalid = re.search(r'Invalid user (.+?) from (\d+\.\d+\.\d+\.\d+)', line)
    if match_invalid:
        user = match_invalid.group(1)
        ip = match_invalid.group(2)
        return "INVALID_USER", ip, user, "Пользователь не найден"
    match_preauth = re.search(r'(?:Connection closed|Disconnected) by authenticating user (.+?) from (\d+\.\d+\.\d+\.\d+)', line)
    if match_preauth:
        user = match_preauth.group(1)
        ip = match_preauth.group(2)
        return "PREAUTH", ip, user, "Разрыв до аутентификации"
    match_closed = re.search(r'Connection closed by (\d+\.\d+\.\d+\.\d+)', line)
    if match_closed:
        ip = match_closed.group(1)
        return "CLOSED", "🔌", ip, "-", "Соединение закрыто"
    return None, None, None, None, None

def query_loki(start_time_ns):
    safe_start = max(0, start_time_ns - 5_000_000_000)
    end_time_ns = int(time.time() * 1e9)

    params = {
        'query': LOKI_QUERY,
        'start': safe_start,
        'end': end_time_ns,
        'limit': 100
    }

    try:
        resp = requests.get(f"{LOKI_URL}/loki/api/v1/query_range", params=params, timeout=5)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"[!] Ошибка запроса к Loki: {e}")
        return start_time_ns

    results = data.get('data', {}).get('result', [])
    if not results:
        return start_time_ns

    max_ts = start_time_ns

    for stream in results:
        for entry in stream.get('values', []):
            ts = int(entry[0])
            line = entry[1]

            if ts <= start_time_ns:
                continue

            status_code, icon, ip, user, details = parse_ssh_log(line)
            
            if status_code:
                labels = stream.get('stream', {})
                source_host = labels.get('instance', labels.get('hostname', 'unknown'))
                
                msg = f"{icon} <b>SSH Event: {status_code}</b>\n"
                msg += f" User: <code>{user}</code>\n"
                msg += f" IP: <code>{ip}</code>\n"
                if details and details != "-":
                    msg += f" {details}\n"
                msg += f"🖥 Host: <code>{source_host}</code>"
                
                send_telegram(msg)
                print(f"[+] Alert: {status_code} | {user}@{ip}")

            if ts > max_ts:
                max_ts = ts

    return max_ts

def main():
    print(f"[*] SSH Monitor started. Loki: {LOKI_URL}")
    last_ts = get_last_timestamp()
    if last_ts == 0:
        last_ts = int((time.time() - 60) * 1e9)
        print(f"[*] First run, start time: 1 min ago")
    
    while True:
        try:
            new_ts = query_loki(last_ts)
            if new_ts > last_ts:
                save_last_timestamp(new_ts)
                last_ts = new_ts
        except KeyboardInterrupt:
            print("\n[*] Stopping...")
            break
        except Exception as e:
            print(f"[!] Critical error: {e}")
            time.sleep(5)
        time.sleep(POLL_INTERVAL)

if __name__ == '__main__':
    main()
