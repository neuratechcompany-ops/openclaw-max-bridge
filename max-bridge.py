#!/usr/bin/env python3
"""MAX Messenger Bridge — Long Polling → OpenClaw → MAX
Resilient: retries, auto-reconnect, never dies.
"""

import json, time, requests, sys, os, traceback

# ====== CONFIGURE THESE ======
MAX_TOKEN = "your_max_bot_token_here"
OC_TOKEN  = "your_openclaw_gateway_token_here"
# =============================

MAX_API = "https://platform-api.max.ru"
OC_API = "http://localhost:18789/v1/chat/completions"

HEADERS_MAX = {"Authorization": MAX_TOKEN}
HEADERS_OC = {
    "Authorization": f"Bearer {OC_TOKEN}",
    "Content-Type": "application/json"
}

STATE_FILE = os.path.expanduser("~/.openclaw/workspace/max-bridge-state.json")


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def load_marker():
    try:
        with open(STATE_FILE) as f:
            return json.load(f).get("marker")
    except:
        return None


def save_marker(marker):
    with open(STATE_FILE, "w") as f:
        json.dump({"marker": marker, "updated": time.time()}, f)


def get_updates(marker):
    params = {"timeout": 25}
    if marker is not None:
        params["marker"] = marker
    resp = requests.get(f"{MAX_API}/updates", headers=HEADERS_MAX, params=params, timeout=35)
    resp.raise_for_status()
    return resp.json()


def send_to_openclaw(user_id, text):
    """Send to OpenClaw with retry on timeout."""
    payload = {
        "model": "openclaw",
        "messages": [{"role": "user", "content": text}],
        "max_tokens": 1000
    }
    headers = dict(HEADERS_OC)
    headers["x-openclaw-session-key"] = f"max-user-{user_id}"
    headers["x-openclaw-model"] = "deepseek/deepseek-v4-flash"

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


def send_to_max(user_id, text):
    """Send to MAX with retry."""
    payload = {"text": text, "format": "markdown"}
    for attempt in range(3):
        try:
            resp = requests.post(
                f"{MAX_API}/messages", params={"user_id": user_id},
                headers=HEADERS_MAX, json=payload, timeout=15
            )
            if resp.status_code == 200:
                return
            log(f"  MAX send returned {resp.status_code} (attempt {attempt+1}/3)")
        except Exception as e:
            log(f"  MAX send error (attempt {attempt+1}/3): {e}")
        time.sleep(3)
    log(f"  MAX send FAILED after 3 attempts")


def send_typing(user_id):
    try:
        requests.post(f"{MAX_API}/chats/actions", params={"user_id": user_id},
                      headers=HEADERS_MAX, json={"action": "typing"}, timeout=5)
    except:
        pass


def main():
    log("MAX ↔ OpenClaw bridge STARTING")
    log(f"OpenClaw: {OC_API}")

    marker = load_marker()
    log(f"Marker: {marker}")

    consecutive_errors = 0

    while True:
        try:
            data = get_updates(marker)
            consecutive_errors = 0  # reset on success
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

                    log(f"Message from {user_id}: {text[:80]}")

                    send_typing(user_id)

                    try:
                        reply = send_to_openclaw(user_id, text)
                        log(f"Reply: {reply[:80]}")
                        send_to_max(user_id, reply)
                        log(f"Sent to MAX ✓")
                    except Exception as e:
                        log(f"OpenClaw error: {e}")
                        send_to_max(user_id, "⚠️ Извини, я сейчас не могу ответить. Попробуй через минуту.")

            if new_marker != marker:
                marker = new_marker
                save_marker(marker)

            time.sleep(3)

        except requests.exceptions.Timeout:
            # Long polling timeout — normal
            continue
        except requests.exceptions.ConnectionError as e:
            consecutive_errors += 1
            log(f"Connection error (#{consecutive_errors}): {e}")
            time.sleep(min(consecutive_errors * 5, 30))  # backoff
        except Exception as e:
            consecutive_errors += 1
            log(f"Error (#{consecutive_errors}): {e}")
            traceback.print_exc()
            time.sleep(min(consecutive_errors * 5, 60))


if __name__ == "__main__":
    main()
