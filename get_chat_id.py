#!/usr/bin/env python3
"""
Find your Telegram chat_id.

Run this AFTER you've (1) created the bot and (2) sent it any message in Telegram.
    python3 get_chat_id.py <BOT_TOKEN>
or, if you've already put the token in secrets.yaml:
    python3 get_chat_id.py
"""
import json
import os
import ssl
import sys
import urllib.request

import yaml

try:
    import certifi
    CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    CTX = ssl.create_default_context()

HERE = os.path.dirname(os.path.abspath(__file__))

token = sys.argv[1] if len(sys.argv) > 1 else None
if not token:
    p = os.path.join(HERE, "secrets.yaml")
    if os.path.exists(p):
        with open(p) as f:
            token = ((yaml.safe_load(f) or {}).get("telegram") or {}).get("bot_token")

if not token or "your-bot-token" in str(token):
    print("Usage: python3 get_chat_id.py <BOT_TOKEN>")
    sys.exit(1)

url = "https://api.telegram.org/bot%s/getUpdates" % token
req = urllib.request.Request(url, headers={"User-Agent": "job-scout/1.0"})
data = json.loads(urllib.request.urlopen(req, timeout=20, context=CTX).read().decode("utf-8"))

seen = set()
for u in data.get("result", []):
    msg = u.get("message") or u.get("edited_message") or {}
    chat = msg.get("chat") or {}
    cid = chat.get("id")
    if cid and cid not in seen:
        seen.add(cid)
        who = chat.get("username") or chat.get("first_name") or ""
        print("chat_id: %s   (%s)" % (cid, who))

if not seen:
    print("No messages found. Open Telegram, send your bot any message (e.g. 'hi'), then re-run.")
