"""
OpenAI Status Monitor — Web App
================================
Install:  pip install feedparser aiohttp flask flask-sock
Run:      python app.py
Open:     http://localhost:5000
"""

import asyncio
import re
import hashlib
import json
import threading
import feedparser
import aiohttp
from datetime import datetime, timezone
from flask import Flask, render_template, request, jsonify
from flask_sock import Sock

app = Flask(__name__)
sock = Sock(app)

# ── Global state ──────────────────────────────────────────────
active_feeds = []          # list of {name, url}
monitor_thread = None
monitor_running = False
connected_clients = set()  # WebSocket clients
incidents_log = []         # all incidents seen so far (for new clients)

CHECK_INTERVAL = 30


# ── Helpers ───────────────────────────────────────────────────

def content_hash(text):
    return hashlib.md5(text.encode()).hexdigest()


def extract_product(title, content):
    combined = (title + " " + content).lower()
    product_map = {
        "Chat Completions / ChatGPT": ["chat completions", "chatgpt", "chat"],
        "Responses API":              ["responses api"],
        "Assistants API":             ["assistants api", "assistants"],
        "Embeddings":                 ["embeddings"],
        "Fine-tuning":                ["fine-tun"],
        "DALL-E / Images API":        ["dall-e", "image generation", "images api"],
        "Whisper / Audio API":        ["whisper", "audio", "speech"],
        "Realtime API":               ["realtime"],
        "Batch API":                  ["batch"],
        "OpenAI API":                 ["api"],
    }
    for product, keywords in product_map.items():
        if any(kw in combined for kw in keywords):
            return product
    return "Platform"


def clean_html(raw):
    text = re.sub(r"<[^>]+>", " ", raw)
    for entity, char in [("&nbsp;", " "), ("&amp;", "&"),
                          ("&lt;", "<"), ("&gt;", ">"), ("&#39;", "'")]:
        text = text.replace(entity, char)
    return re.sub(r"\s+", " ", text).strip()


def get_timestamp(entry):
    for attr in ("updated_parsed", "published_parsed"):
        val = getattr(entry, attr, None)
        if val:
            return datetime(*val[:6]).strftime("%Y-%m-%d %H:%M:%S")
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def broadcast(msg_type, data):
    """Send a message to all connected WebSocket clients."""
    payload = json.dumps({"type": msg_type, "data": data})
    dead = set()
    for ws in connected_clients:
        try:
            ws.send(payload)
        except Exception:
            dead.add(ws)
    connected_clients.difference_update(dead)


# ── Core async monitor ────────────────────────────────────────

async def watch_feed(session, feed_cfg):
    global incidents_log
    name      = feed_cfg["name"]
    url       = feed_cfg["url"]
    etag      = None
    last_mod  = None
    last_hash = None
    seen_ids  = set()
    first_run = True

    broadcast("log", {"msg": f"▶ Starting watcher for {name}", "level": "info"})

    while monitor_running:
        try:
            headers = {"User-Agent": "OpenAI-Status-Monitor/1.0"}
            if etag:     headers["If-None-Match"]     = etag
            if last_mod: headers["If-Modified-Since"] = last_mod

            async with session.get(url, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:

                if resp.status == 304:
                    broadcast("log", {
                        "msg": f"{name}: 304 Not Modified — no new incidents",
                        "level": "ok"
                    })

                elif resp.status == 200:
                    body        = await resp.text()
                    server_etag = resp.headers.get("ETag")
                    server_lmod = resp.headers.get("Last-Modified")
                    etag_support = bool(server_etag or server_lmod)

                    if server_etag: etag     = server_etag
                    if server_lmod: last_mod = server_lmod

                    current_hash = content_hash(body)
                    if not first_run and not etag_support:
                        if current_hash == last_hash:
                            broadcast("log", {
                                "msg": f"{name}: Hash unchanged — no new incidents",
                                "level": "ok"
                            })
                            await asyncio.sleep(CHECK_INTERVAL)
                            continue
                    last_hash = current_hash

                    feed    = feedparser.parse(body)
                    entries = feed.entries

                    if first_run:
                        broadcast("log", {
                            "msg": f"Connected to {name} — {len(entries)} incidents in history. ETag: {'supported' if etag_support else 'not supported, using hash fallback'}",
                            "level": "info"
                        })
                        if entries:
                            e = entries[0]
                            content_raw = ""
                            if hasattr(e, "content") and e.content:
                                content_raw = e.content[0].value
                            elif hasattr(e, "summary"):
                                content_raw = e.summary
                            content = clean_html(content_raw)
                            incident = {
                                "ts":      get_timestamp(e),
                                "provider": name,
                                "product": extract_product(e.get("title",""), content),
                                "title":   e.get("title","No title"),
                                "detail":  content[:400],
                                "link":    e.get("link","#"),
                                "is_new":  False
                            }
                            incidents_log.insert(0, incident)
                            broadcast("incident", incident)

                        for e in entries:
                            seen_ids.add(e.get("id") or e.get("link",""))
                        first_run = False

                    else:
                        new_entries = []
                        for e in entries:
                            uid = e.get("id") or e.get("link","")
                            if uid not in seen_ids:
                                new_entries.append(e)
                                seen_ids.add(uid)

                        if new_entries:
                            for e in reversed(new_entries):
                                content_raw = ""
                                if hasattr(e, "content") and e.content:
                                    content_raw = e.content[0].value
                                elif hasattr(e, "summary"):
                                    content_raw = e.summary
                                content = clean_html(content_raw)
                                incident = {
                                    "ts":       get_timestamp(e),
                                    "provider": name,
                                    "product":  extract_product(e.get("title",""), content),
                                    "title":    e.get("title","No title"),
                                    "detail":   content[:400],
                                    "link":     e.get("link","#"),
                                    "is_new":   True
                                }
                                incidents_log.insert(0, incident)
                                broadcast("incident", incident)
                        else:
                            broadcast("log", {
                                "msg": f"{name}: Monitoring... no new incidents",
                                "level": "ok"
                            })

                else:
                    broadcast("log", {"msg": f"{name}: HTTP {resp.status}", "level": "warn"})

        except Exception as e:
            broadcast("log", {"msg": f"{name} error: {type(e).__name__}: {e}", "level": "error"})

        await asyncio.sleep(CHECK_INTERVAL)


async def run_monitor():
    connector = aiohttp.TCPConnector(limit=50)
    async with aiohttp.ClientSession(connector=connector) as session:
        await asyncio.gather(*[watch_feed(session, cfg) for cfg in active_feeds])


def start_monitor_thread():
    global monitor_running
    monitor_running = True
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(run_monitor())


# ── Flask routes ──────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/start", methods=["POST"])
def api_start():
    global active_feeds, monitor_thread, monitor_running, incidents_log

    data = request.json
    feeds = data.get("feeds", [])

    if not feeds:
        return jsonify({"error": "No feeds provided"}), 400

    # Stop existing monitor if running
    monitor_running = False
    incidents_log = []

    active_feeds = feeds

    monitor_thread = threading.Thread(target=start_monitor_thread, daemon=True)
    monitor_thread.start()

    return jsonify({"status": "started", "feeds": len(feeds)})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    global monitor_running
    monitor_running = False
    broadcast("log", {"msg": "Monitor stopped by user.", "level": "warn"})
    return jsonify({"status": "stopped"})


@app.route("/api/status")
def api_status():
    return jsonify({
        "running": monitor_running,
        "feeds": active_feeds,
        "incidents": incidents_log[:50]
    })


@sock.route("/ws")
def websocket(ws):
    connected_clients.add(ws)
    # Send existing incidents to newly connected client
    for inc in incidents_log[:20]:
        try:
            ws.send(json.dumps({"type": "incident", "data": inc}))
        except Exception:
            break
    try:
        while True:
            ws.receive(timeout=60)   # keep alive
    except Exception:
        pass
    finally:
        connected_clients.discard(ws)


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port, threaded=True)
