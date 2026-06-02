#!/usr/bin/env python3
"""MAX Messenger Bridge — Long Polling + Proactive Send → OpenClaw → MAX
v2.0 — Resilient: retries, auto-reconnect, HTTP server for cron/proactive messages.
"""

import json, time, requests, sys, os, traceback, threading
from http.server import HTTPServer, BaseHTTPRequestHandler

# ====== CONFIGURE VIA ENVIRONMENT ======
# export MAX_TOKEN="your_max_bot_token"
# export OC_TOKEN="your_openclaw_token"
# export OC_API="http://localhost:18789/v1/chat/completions"  # optional
MAX_TOKEN = os.environ.get("MAX_TOKEN", "")
OC_TOKEN  = os.environ.get("OC_TOKEN", "")
PROACTIVE_PORT = int(os.environ.get("PROACTIVE_PORT", "18790"))

if not MAX_TOKEN or not OC_TOKEN:
    print("ERROR: MAX_TOKEN and OC_TOKEN environment variables are required.")
    print("Example:")
    print("  export MAX_TOKEN='your_max_token'")
    print("  export OC_TOKEN='your_openclaw_token'")
    sys.exit(1)
# =======================================

MAX_API = "https://platform-api.max.ru"
OC_API = os.environ.get("OC_API", "http://localhost:18789/v1/chat/completions")

HEADERS_MAX = {"Authorization": MAX_TOKEN}
HEADERS_OC = {
    "Authorization": f"Bearer {OC_TOKEN}",
    "Content-Type": "application/json"
}

STATE_FILE = os.path.expanduser("~/.openclaw/workspace/max-bridge-state.json")


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ─── State management ─────────────────────────────────────────

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except:
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def load_marker():
    return load_state().get("marker")


def save_marker(marker):
    state = load_state()
    state["marker"] = marker
    state["updated"] = time.time()
    save_state(state)


# ─── Session → user_id mapping ────────────────────────────────

def map_session_to_user(session_key, max_user_id):
    """Remember which MAX user corresponds to which OpenClaw session."""
    state = load_state()
    if "session_map" not in state:
        state["session_map"] = {}
    state["session_map"][session_key] = {
        "user_id": max_user_id,
        "last_seen": time.time()
    }
    save_state(state)
    log(f"  Mapped session {session_key} → user {max_user_id}")


def get_user_by_session(session_key):
    state = load_state()
    return state.get("session_map", {}).get(session_key, {}).get("user_id")


def get_user_by_any(identifier):
    """Resolve user_id from either direct user_id or session_key."""
    state = load_state()
    smap = state.get("session_map", {})
    # Direct match
    for sk, v in smap.items():
        if v["user_id"] == identifier:
            return identifier
    # Session key match
    if identifier in smap:
        return smap[identifier]["user_id"]
    # Fallback: try all known users
    if identifier == "__all__":
        return list(set(v["user_id"] for v in smap.values()))
    return identifier  # assume it's already a user_id


# ─── MAX API ──────────────────────────────────────────────────

def get_updates(marker):
    params = {"timeout": 25}
    if marker is not None:
        params["marker"] = marker
    resp = requests.get(f"{MAX_API}/updates", headers=HEADERS_MAX,
                        params=params, timeout=35)
    resp.raise_for_status()
    return resp.json()


def send_to_max(user_id, text):
    """Send to MAX with retry. Returns True on success."""
    payload = {"text": text, "format": "markdown"}
    for attempt in range(3):
        try:
            resp = requests.post(
                f"{MAX_API}/messages", params={"user_id": user_id},
                headers=HEADERS_MAX, json=payload, timeout=15
            )
            if resp.status_code == 200:
                return True
            log(f"  MAX send returned {resp.status_code} (attempt {attempt+1}/3)")
            if resp.status_code == 429:  # rate limit
                time.sleep(5)
        except Exception as e:
            log(f"  MAX send error (attempt {attempt+1}/3): {e}")
        time.sleep(3)
    log(f"  MAX send FAILED after 3 attempts")
    return False


def send_typing(user_id):
    try:
        requests.post(f"{MAX_API}/chats/actions", params={"user_id": user_id},
                      headers=HEADERS_MAX, json={"action": "typing"}, timeout=5)
    except:
        pass


# ─── OpenClaw API ─────────────────────────────────────────────

OC_MODEL = os.environ.get("OC_MODEL", "deepseek/deepseek-v4-flash")

def send_to_openclaw(user_id, text):
    """Send to OpenClaw with retry on timeout."""
    session_key = f"max-user-{user_id}"

    payload = {
        "model": "openclaw",
        "messages": [{"role": "user", "content": text}],
        "max_tokens": 2000
    }
    headers = dict(HEADERS_OC)
    headers["x-openclaw-session-key"] = session_key
    headers["x-openclaw-model"] = OC_MODEL

    for attempt in range(3):
        try:
            resp = requests.post(OC_API, headers=headers, json=payload, timeout=120)
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except requests.exceptions.Timeout:
            log(f"  OpenClaw timeout (attempt {attempt+1}/3), retrying...")
            if attempt == 2:
                raise
            time.sleep(5)
        except Exception:
            raise


# ─── Proactive HTTP Server ────────────────────────────────────

class ProactiveHandler(BaseHTTPRequestHandler):
    """HTTP server that accepts push messages → delivers to MAX."""

    def log_message(self, fmt, *args):
        log(f"  HTTP: {fmt % args}")

    def do_POST(self):
        # Read body
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
            return

        # ═══ OpenClaw webhook format (from cron delivery) ═══
        # May contain: {"payload": {...}, "sessionKey": "max-user-xxx", ...}
        text = None
        user_id = None

        # OpenClaw cron webhook format
        if "sessionKey" in data and "payload" in data:
            session_key = data["sessionKey"]
            user_id = get_user_by_session(session_key)
            payload = data["payload"]
            if isinstance(payload, dict):
                text = payload.get("text") or payload.get("message")
            elif isinstance(payload, str):
                text = payload

        # Direct format: {"user_id": "...", "text": "..."}
        if not text:
            text = data.get("text") or data.get("message")
        if not user_id:
            user_id = data.get("user_id")
            if user_id:
                user_id = get_user_by_any(user_id)

        # Broadcast to all known users
        if not user_id and data.get("broadcast"):
            state = load_state()
            smap = state.get("session_map", {})
            all_users = list(set(v["user_id"] for v in smap.values()))
            if all_users and text:
                log(f"  Broadcast to {len(all_users)} users: {text[:80]}")
                for uid in all_users:
                    send_to_max(uid, text)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "ok": True, "sent_to": len(all_users)
                }).encode())
                return

        if not user_id or not text:
            self.send_error(400, "Missing user_id or text")
            log(f"  Rejected request: user_id={user_id}, text={bool(text)}")
            return

        log(f"  Proactive send → user {user_id}: {text[:80]}")
        ok = send_to_max(user_id, text)

        self.send_response(200 if ok else 502)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": ok}).encode())

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            state = load_state()
            self.wfile.write(json.dumps({
                "ok": True,
                "users": len(state.get("session_map", {})),
                "marker": state.get("marker")
            }).encode())
        elif self.path == "/users":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            state = load_state()
            self.wfile.write(json.dumps(state.get("session_map", {})).encode())
        else:
            self.send_error(404)


def start_proactive_server():
    server = HTTPServer(("127.0.0.1", PROACTIVE_PORT), ProactiveHandler)
    log(f"Proactive HTTP server on :{PROACTIVE_PORT} (cron → MAX gateway)")
    server.serve_forever()


# ─── Main Loop ────────────────────────────────────────────────

def main():
    log("MAX ↔ OpenClaw bridge v2.0 STARTING")
    log(f"OpenClaw: {OC_API}")
    log(f"Proactive: http://127.0.0.1:{PROACTIVE_PORT}/send")

    # Start proactive HTTP server in background
    http_thread = threading.Thread(target=start_proactive_server, daemon=True)
    http_thread.start()

    marker = load_marker()
    log(f"Marker: {marker}")

    consecutive_errors = 0

    while True:
        try:
            data = get_updates(marker)
            consecutive_errors = 0
            updates = data.get("updates", [])
            new_marker = data.get("marker", marker)

            if updates:
                log(f"Got {len(updates)} update(s), marker={new_marker}")
                for upd in updates:
                    if upd.get("update_type") != "message_created":
                        continue

                    msg = upd.get("message", {})
                    sender = msg.get("sender", {})
                    body = msg.get("body", {})
                    user_id = sender.get("user_id")
                    text = body.get("text", "")

                    if sender.get("is_bot") or not text:
                        continue

                    # Map session for future proactive sends
                    map_session_to_user(f"max-user-{user_id}", user_id)

                    log(f"Message from {user_id}: {text[:80]}")

                    send_typing(user_id)

                    try:
                        reply = send_to_openclaw(user_id, text)
                        log(f"Reply: {reply[:80]}")
                        send_to_max(user_id, reply)
                        log(f"Sent to MAX ✓")
                    except Exception as e:
                        log(f"OpenClaw error: {e}")
                        send_to_max(user_id,
                            "⚠️ Извини, я сейчас не могу ответить. Попробуй через минуту.")

            if new_marker != marker:
                marker = new_marker
                save_marker(marker)

            time.sleep(3)

        except requests.exceptions.Timeout:
            continue
        except requests.exceptions.ConnectionError as e:
            consecutive_errors += 1
            log(f"Connection error (#{consecutive_errors}): {e}")
            time.sleep(min(consecutive_errors * 5, 30))
        except Exception as e:
            consecutive_errors += 1
            log(f"Error (#{consecutive_errors}): {e}")
            traceback.print_exc()
            time.sleep(min(consecutive_errors * 5, 60))


if __name__ == "__main__":
    main()
