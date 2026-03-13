#!/usr/bin/env python3
import requests
import json
import time
import re
import os
from datetime import datetime

TG_BOT_TOKEN = '8762344936:AAFKNCoiymDuaTwTwDlWHLy9pUHmrr9scY0'
TG_CHAT_ID = '829032472'

LOKI_URL = 'http://localhost:31000'
POLL_INTERVAL = 10
STATE_FILE = '/opt/ssh_monitor/last_timestamp.txt'
LOKI_QUERY = '{job="authlog"} |= "sshd" |~ "(Accepted|Failed|Invalid)"'

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
        if resp.json().get('ok'):
            print(f"[+] Уведомление отправлено")
        else:
            print(f"[!] Ошибка Telegram: {resp.text}")
    except Exception as e:
        print(f"[!] Ошибка отправки: {e}")

def extract_ip_from_log(line):
    ipv4_pattern = r'(\d{1,3}\.){3}\d{1,3}'
    ipv4_match = re.search(ipv4_pattern, line)
    if ipv4_match:
        return ipv4_match.group(0)
    
    ipv6_pattern = r'from (::1|[\da-f:]+)'
    ipv6_match = re.search(ipv6_pattern, line)
    if ipv6_match:
        ip = ipv6_match.group(1)
        if ip == '::1':
            return 'localhost'
        return ip
    
    return None

def parse_ssh_log(line):
    attacker_ip = extract_ip_from_log(line)
    if not attacker_ip:
        return (None, None, None, None)
    
    if 'Accepted' in line:
        match = re.search(r'Accepted (\S+) for (\S+)', line)
        if match:
            method = match.group(1)
            user = match.group(2)
            return ("SSH_SUCCESS", attacker_ip, user, f"Method: {method}")
    
    elif 'Failed password' in line:
        if 'invalid user' in line:
            match = re.search(r'Failed password for invalid user (\S+)', line)
            if match:
                user = match.group(1)
                return ("SSH_FAILED", attacker_ip, user, "Invalid password - user does not exist")
        else:
            match = re.search(r'Failed password for (\S+)', line)
            if match:
                user = match.group(1)
                return ("SSH_FAILED", attacker_ip, user, "Invalid password")
    
    elif 'Invalid user' in line:
        match = re.search(r'Invalid user (\S+)', line)
        if match:
            user = match.group(1)
            return ("SSH_INVALID_USER", attacker_ip, user, "Attempt with non-existent user")
    
    return (None, None, None, None)

def query_loki(start_time_ns):
    end_time_ns = int(time.time() * 1e9)
    params = {
        'query': LOKI_QUERY,
        'start': start_time_ns,
        'end': end_time_ns,
        'limit': 100
    }

    try:
        url = f"{LOKI_URL}/loki/api/v1/query_range"
        resp = requests.get(url, params=params, timeout=5)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"[!] Ошибка запроса к Loki: {e}")
        return start_time_ns

    results = data.get('data', {}).get('result', [])
    if not results:
        return start_time_ns

    max_ts = start_time_ns
    alerts_sent = 0

    for stream in results:
        for entry in stream.get('values', []):
            ts = int(entry[0])
            line = entry[1]

            if ts <= start_time_ns:
                continue

            status, attacker_ip, user, details = parse_ssh_log(line)
            
            if status:
                event_time = datetime.fromtimestamp(ts/1e9).strftime('%d.%m.%Y %H:%M:%S')
                
                msg = f"<b>{status}</b>\n"
                msg += f"──────────────────\n"
                msg += f"User: <b>{user}</b>\n"
                msg += f"Attacker IP: <b>{attacker_ip}</b>\n"
                msg += f"Details: {details}\n"
                msg += f"Time: {event_time}\n"
                msg += f"──────────────────"
                
                print(f"[!] {status} | {user}@{attacker_ip}")
                send_telegram(msg)
                alerts_sent += 1

            if ts > max_ts:
                max_ts = ts

    if alerts_sent > 0:
        print(f"[+] Отправлено уведомлений: {alerts_sent}")
    
    return max_ts

def main():
    print(f"[*] SSH Monitor запущен")
    print(f"[*] Loki URL: {LOKI_URL}")
    print(f"[*] Отслеживаются все IP адреса")
    
    try:
        ready = requests.get(f"{LOKI_URL}/ready", timeout=2)
        if ready.status_code == 200:
            print(f"[✓] Loki доступен")
    except:
        print(f"[!] Loki недоступен, проверьте подключение")
    
    last_ts = get_last_timestamp()
    if last_ts == 0:
        last_ts = int((time.time() - 300) * 1e9)
        print(f"[*] Первый запуск, проверяем логи за последние 5 минут")
    
    while True:
        try:
            new_ts = query_loki(last_ts)
            if new_ts > last_ts:
                save_last_timestamp(new_ts)
                last_ts = new_ts
            time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            print("\n[*] Остановка монитора...")
            break
        except Exception as e:
            print(f"[!] Ошибка: {e}")
            time.sleep(5)

if __name__ == '__main__':
    main()
