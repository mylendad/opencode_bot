#!/usr/bin/env python3
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
OWNER_PATH = os.path.join(BASE, "owner.json")
STATE_PATH = os.path.join(BASE, "bot_state.json")
TELEGRAM_API = "https://api.telegram.org"
TG_CHUNK_LIMIT = 3800


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


CFG = load_json(CONFIG_PATH, {})
TOKEN = (CFG.get("bot_token") or "").strip()
SEND_REPLIES = bool(CFG.get("send_replies_to_telegram", True))

instances = list(CFG.get("instances") or [])
if not instances and (CFG.get("opencode_url") or "http://127.0.0.1:4096"):
    instances = [{
        "name": "local",
        "url": (CFG.get("opencode_url") or "http://127.0.0.1:4096").rstrip("/"),
        "password": CFG.get("opencode_password") or "",
    }]

state = load_json(STATE_PATH, {})
active_name = state.get("instance")

allowed = set(CFG.get("allowed_telegram_ids") or [])
owner = load_json(OWNER_PATH, {})
if owner.get("id"):
    allowed.add(owner["id"])

pending_reply = {}
last_assistant = {}
current_msg = {}
lock = threading.Lock()


def inst_by_name(name):
    for i in instances:
        if i["name"] == name:
            return i
    return None


def get_active():
    global active_name
    for i in instances:
        if i["name"] == active_name:
            return i
    active_name = instances[0]["name"]
    return instances[0]


def set_active(name):
    global active_name
    inst = inst_by_name(name)
    if not inst:
        return False
    active_name = name
    state["instance"] = name
    save_json(STATE_PATH, state)
    return True


def auth_headers(inst):
    headers = {}
    pw = inst.get("password") or ""
    if pw:
        import base64
        user = inst.get("username") or "opencode"
        cred = base64.b64encode(f"{user}:{pw}".encode()).decode()
        headers["Authorization"] = "Basic " + cred
    return headers


def http(url, method="GET", body=None, headers=None, stream=False, timeout=120):
    req = urllib.request.Request(url, method=method)
    req.add_header("Accept", "application/json")
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req.add_header("Content-Type", "application/json")
        req.data = data
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    return urllib.request.urlopen(req, timeout=None if stream else timeout)


def tg(method, params, retries=3):
    url = f"{TELEGRAM_API}/bot{TOKEN}/{method}"
    data = urllib.parse.urlencode(params).encode("utf-8")
    last_err = None
    for _ in range(retries):
        try:
            with urllib.request.urlopen(url, data=data, timeout=70) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            last_err = e
            time.sleep(2)
    print(f"[tg] {method} failed: {last_err}", file=sys.stderr)
    return {}


def tg_send(chat_id, text):
    text = text or "(пусто)"
    while text:
        chunk, text = text[:TG_CHUNK_LIMIT], text[TG_CHUNK_LIMIT:]
        tg("sendMessage", {"chat_id": chat_id, "text": chunk})


def oc_find_session(inst):
    with http(f"{inst['url']}/session", headers=auth_headers(inst)) as r:
        sessions = json.loads(r.read().decode("utf-8"))
    best, best_t = None, -1
    for s in sessions:
        t = (s.get("time") or {}).get("updated") or (s.get("time") or {}).get("created") or 0
        if t > best_t:
            best, best_t = s, t
    if not best:
        raise RuntimeError("нет ни одной сессии opencode")
    return best["id"]


def oc_send_prompt(inst, session_id, text):
    body = {"parts": [{"type": "text", "text": text}]}
    if CFG.get("agent"):
        body["agent"] = CFG["agent"]
    with http(f"{inst['url']}/session/{session_id}/prompt_async", method="POST",
              body=body, headers=auth_headers(inst)) as r:
        status = r.status
    if status not in (200, 204):
        raise RuntimeError(f"opencode вернул {status}")


def event_loop(inst):
    while True:
        try:
            with http(f"{inst['url']}/event", headers=auth_headers(inst), stream=True) as r:
                event_data = []
                for raw in r:
                    line = raw.decode("utf-8", "replace").rstrip("\n")
                    if line.startswith("data:"):
                        event_data.append(line[5:].strip())
                    elif not line and event_data:
                        try:
                            handle_event(inst, json.loads("\n".join(event_data)))
                        except Exception as e:
                            print(f"[event:{inst['name']}] {e}", file=sys.stderr)
                        event_data = []
        except Exception as e:
            print(f"[event:{inst['name']}] reconnect: {e}", file=sys.stderr)
            for key in list(pending_reply):
                name, _sid = key
                if name == inst["name"]:
                    chat_id = pending_reply.pop(key)
                    tg_send(chat_id, f"связь с {name} прервана")
            time.sleep(3)


def handle_event(inst, ev):
    etype = ev.get("type")
    props = ev.get("properties") or {}
    key = lambda sid: (inst["name"], sid)
    if etype not in ("server.heartbeat", "server.connected"):
        print(f"[event:{inst['name']}] {etype}", flush=True)
    if etype == "message.updated":
        info = props.get("info") or {}
        sid = info.get("sessionID")
        mid = info.get("id")
        if sid and mid and info.get("role") == "assistant":
            with lock:
                current_msg[key(sid)] = mid
                last_assistant.setdefault(mid, "")
    elif etype == "message.part.updated":
        part = props.get("part") or {}
        sid = props.get("sessionID") or part.get("sessionID")
        if part.get("type") == "text" and part.get("text"):
            mid = part.get("messageID")
            with lock:
                if sid and key(sid) in current_msg and current_msg[key(sid)] == mid:
                    last_assistant[mid] = part["text"]
    elif etype == "session.idle" or (
        etype == "session.status"
        and (props.get("status") or {}).get("type") == "idle"
    ):
        sid = props.get("sessionID")
        with lock:
            chat_id = pending_reply.pop(key(sid), None)
            mid = current_msg.pop(key(sid), None)
            text = last_assistant.pop(mid, "") if mid else ""
        if chat_id and text:
            print(f"[reply:{inst['name']}] chat={chat_id} len={len(text)}", flush=True)
            tg_send(chat_id, text)


def handle_command(chat_id, text):
    parts = text.split()
    cmd = parts[0].lower().split("@")[0]
    if cmd in ("/start", "/help"):
        msg = (
            "Команды:\n"
            "/instances — список opencode-инстансов\n"
            "/use <имя> — переключиться на инстанс\n"
            "Любой другой текст уйдёт в активный инстанс.\n"
            f"Активен: {get_active()['name']}"
        )
        tg_send(chat_id, msg)
    elif cmd == "/instances":
        urls = [f"* {i['name']} ({urllib.parse.urlparse(i['url']).netloc})"
                for i in instances]
        tg_send(chat_id, "\n".join(urls) + f"\nАктивен: {get_active()['name']}")
    elif cmd == "/use":
        name = parts[1] if len(parts) > 1 else ""
        if set_active(name):
            tg_send(chat_id, f"переключился на {name}")
        else:
            tg_send(chat_id, f"нет инстанса {name!r}; доступно: {', '.join(i['name'] for i in instances)}")
    else:
        tg_send(chat_id, "неизвестная команда; /help — список")


def bot_loop():
    offset = 0
    while True:
        try:
            url = f"{TELEGRAM_API}/bot{TOKEN}/getUpdates?timeout=50&offset={offset + 1}"
            with urllib.request.urlopen(url, timeout=70) as r:
                payload = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            print(f"[tg] getUpdates failed: {e}", file=sys.stderr)
            time.sleep(5)
            continue
        for upd in payload.get("result", []):
            offset = max(offset, upd["update_id"])
            msg = upd.get("message") or {}
            chat_id = (msg.get("chat") or {}).get("id")
            from_id = (msg.get("from") or {}).get("id")
            text = (msg.get("text") or "").strip()
            if not chat_id or not from_id or not text:
                continue
            if not allowed:
                if owner.get("claimed"):
                    continue
                if not os.path.exists(OWNER_PATH):
                    save_json(OWNER_PATH, {"id": from_id, "claimed": True})
                    allowed.add(from_id)
                    tg_send(chat_id, f"Бот привязан к вам. /help — команды.")
                else:
                    tg_send(chat_id, "Этот бот уже закреплён за другим пользователем.")
                continue
            if from_id not in allowed:
                tg_send(chat_id, "Доступ запрещён.")
                continue
            if text.startswith("/"):
                handle_command(chat_id, text)
                continue
            if not SEND_REPLIES:
                try:
                    inst = get_active()
                    sid = oc_find_session(inst)
                    oc_send_prompt(inst, sid, text)
                    tg_send(chat_id, f"отправлено в {inst['name']}")
                except Exception as e:
                    tg_send(chat_id, f"ошибка: {e}")
                continue
            inst = get_active()
            try:
                sid = oc_find_session(inst)
                print(f"[inject:{inst['name']}] chat={chat_id} sid={sid} text={text[:50]!r}", flush=True)
                with lock:
                    pending_reply[(inst["name"], sid)] = chat_id
            except Exception as e:
                pending_reply.clear()
                tg_send(chat_id, f"ошибка opencode ({inst['name']}): {e}")
                continue
            try:
                oc_send_prompt(inst, sid, text)
                tg_send(chat_id, f"отправлено в {inst['name']}")
            except Exception as e:
                pending_reply.pop((inst["name"], sid), None)
                tg_send(chat_id, f"ошибка: {e}")


def main():
    if not TOKEN or "PASTE_TOKEN" in TOKEN:
        print("впишите bot_token в config.json", file=sys.stderr)
        sys.exit(1)
    healthy = []
    for inst in instances:
        try:
            with http(f"{inst['url']}/global/health", headers=auth_headers(inst)) as r:
                h = json.loads(r.read().decode("utf-8"))
            if h.get("healthy"):
                healthy.append(inst["name"])
        except Exception:
            print(f"инстанс {inst['name']} ({inst['url']}) недоступен", file=sys.stderr)
    if not healthy:
        print(
            "ни один opencode-инстанс не доступен: запустите везде\n"
            "  opencode --port 4096 [--hostname 0.0.0.0]\n"
            "или поправьте config.json",
            file=sys.stderr,
        )
        sys.exit(1)
    for inst in instances:
        threading.Thread(target=event_loop, args=(inst,), daemon=True).start()
    print(f"бот запущен; активен: {get_active()['name']} ({'✓' if get_active()['name'] in healthy else 'НЕ отвечает'})")
    bot_loop()


if __name__ == "__main__":
    main()
