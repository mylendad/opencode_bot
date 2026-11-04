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
STATE_PATH = os.path.join(BASE, "bot_state.json")
TELEGRAM_API = "https://api.telegram.org"
TG_CHUNK_LIMIT = 3800
ALLOWED_IDS = {458073613}


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

instances = list(CFG.get("instances") or [])

state = load_json(STATE_PATH, {})
active_name = state.get("instance")

pending_reply = {}
last_assistant = {}
current_msg = {}
pinned = {}
lock = threading.Lock()


def load_pins():
    for c, m in (state.get("pinned") or {}).items():
        chat, inst = c.rsplit("|", 1)
        pinned[(int(chat), inst)] = m


def save_pins():
    state["pinned"] = {f"{chat}|{inst}": sid for (chat, inst), sid in pinned.items()}
    save_json(STATE_PATH, state)


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
    print(f"[tg] {method} failed: {last_err}", file=sys.stderr, flush=True)
    return {}


def tg_send(chat_id, text, keyboard=None):
    text = text or "(пусто)"
    while text:
        chunk, text = text[:TG_CHUNK_LIMIT], text[TG_CHUNK_LIMIT:]
        params = {"chat_id": chat_id, "text": chunk}
        if keyboard:
            params["reply_markup"] = json.dumps(keyboard)
        tg("sendMessage", params)


def oc_sessions(inst):
    with http(f"{inst['url']}/session", headers=auth_headers(inst)) as r:
        sessions = json.loads(r.read().decode("utf-8"))
    recent = sorted(
        sessions,
        key=lambda s: (s.get("time") or {}).get("updated") or (s.get("time") or {}).get("created") or 0,
        reverse=True,
    )
    return recent


def oc_find_session(inst):
    recent = oc_sessions(inst)
    if not recent:
        raise RuntimeError("нет ни одной сессии opencode")
    return recent[0]["id"]


def oc_send_prompt(inst, session_id, text):
    body = {"parts": [{"type": "text", "text": text}]}
    if CFG.get("agent"):
        body["agent"] = CFG["agent"]
    with http(f"{inst['url']}/session/{session_id}/prompt_async", method="POST",
              body=body, headers=auth_headers(inst)) as r:
        status = r.status
    if status not in (200, 204):
        raise RuntimeError(f"opencode вернул {status}")


def menu_keyboard():
    return {"inline_keyboard": [
        [{"text": "Сессии", "callback_data": "sess:list"},
         {"text": "Новая сессия", "callback_data": "sess:new"}],
        [{"text": "Прервать (abort)", "callback_data": "sess:abort"},
         {"text": "Открепить сессию", "callback_data": "sess:clear"}],
        [{"text": "Инстансы", "callback_data": "inst:list"}],
    ]}


def target_session(chat_id, inst):
    key = (chat_id, inst["name"])
    with lock:
        sid = pinned.get(key)
    if sid:
        return sid
    return oc_find_session(inst)


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
                            print(f"[event:{inst['name']}] {e}", flush=True)
                        event_data = []
        except Exception as e:
            print(f"[event:{inst['name']}] reconnect: {e}", flush=True)
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
                if sid and current_msg.get(key(sid)) == mid:
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


def cb_show_menu(chat_id, callback_id=None):
    inst = get_active()
    key = (chat_id, inst["name"])
    with lock:
        sid = pinned.get(key)
    target = f"закреплена: {sid}" if sid else "авто: последняя активная"
    tg_send(chat_id, f"Инстанс: {inst['name']}\nСессия: {target}", keyboard=menu_keyboard())
    if callback_id:
        tg("answerCallbackQuery", {"callback_query_id": callback_id})


def cb_session_list(chat_id, callback_id=None):
    inst = get_active()
    try:
        recent = oc_sessions(inst)[:8]
    except Exception as e:
        tg_send(chat_id, f"ошибка ({inst['name']}): {e}")
        return
    key = (chat_id, inst["name"])
    with lock:
        cur = pinned.get(key)
    rows = []
    for s in recent:
        title = (s.get("title") or s["id"])[:40]
        rows.append([{"text": ("• " if s["id"] == cur else "") + title,
                      "callback_data": f"sess:use:{s['id']}"}])
    rows.append([{"text": "← меню", "callback_data": "menu"}])
    tg_send(chat_id, f"Сессии ({inst['name']}):", keyboard={"inline_keyboard": rows})
    if callback_id:
        tg("answerCallbackQuery", {"callback_query_id": callback_id})


def cb_session_new(chat_id, callback_id=None):
    inst = get_active()
    try:
        with http(f"{inst['url']}/session", method="POST",
                  body={"title": f"Telegram {time.strftime('%d.%m %H:%M')}"},
                  headers=auth_headers(inst)) as r:
            sess = json.loads(r.read().decode("utf-8"))
        sess_id = sess.get("id")
        if not sess_id:
            raise RuntimeError(str(sess))
        with lock:
            pinned[(chat_id, inst["name"])] = sess_id
            save_pins()
        tg_send(chat_id, f"Новая сессия создана и закреплена ({inst['name']}). Пишите — уйдёт в неё.")
    except Exception as e:
        tg_send(chat_id, f"не удалось создать сессию: {e}")
    if callback_id:
        tg("answerCallbackQuery", {"callback_query_id": callback_id})


def cb_session_abort(chat_id, callback_id=None):
    inst = get_active()
    try:
        sid = target_session(chat_id, inst)
        with http(f"{inst['url']}/session/{sid}/abort", method="POST",
                  headers=auth_headers(inst)) as r:
            json.loads(r.read() or b"{}")
        tg_send(chat_id, f"abort отправлен: {sid}")
    except Exception as e:
        tg_send(chat_id, f"не удалось прервать: {e}")
    if callback_id:
        tg("answerCallbackQuery", {"callback_query_id": callback_id})


def cb_session_clear(chat_id, callback_id=None):
    inst = get_active()
    key = (chat_id, inst["name"])
    with lock:
        pinned.pop(key, None)
        save_pins()
    tg_send(chat_id, f"сессия откреплена; теперь пишем в последнюю активную ({inst['name']})")
    if callback_id:
        tg("answerCallbackQuery", {"callback_query_id": callback_id})


def cb_instance_list(chat_id, callback_id=None):
    inst = get_active()
    rows = []
    for i in instances:
        host = urllib.parse.urlparse(i["url"]).netloc
        mark = ("активен" if i["name"] == inst["name"] else "")
        rows.append([{"text": f"{i['name']} ({host}) {mark}".strip(),
                      "callback_data": f"inst:use:{i['name']}"}])
    rows.append([{"text": "← меню", "callback_data": "menu"}])
    tg_send(chat_id, "Инстансы:", keyboard={"inline_keyboard": rows})
    if callback_id:
        tg("answerCallbackQuery", {"callback_query_id": callback_id})


def cb_instance_use(chat_id, name, callback_id=None):
    if set_active(name):
        break_replies = True
        tg_send(chat_id, f"переключился на {name}", keyboard=menu_keyboard())
    else:
        tg_send(chat_id, f"нет инстанса {name!r}")
    if callback_id:
        tg("answerCallbackQuery", {"callback_query_id": callback_id})


def handle_callback(cb):
    chat_id = ((cb.get("message") or {}).get("chat") or {}).get("id")
    callback_id = cb.get("id")
    from_id = (cb.get("from") or {}).get("id")
    data = (cb.get("data") or "").strip()
    if not chat_id or from_id not in ALLOWED_IDS:
        return
    if data == "menu":
        cb_show_menu(chat_id, callback_id)
    elif data == "sess:list":
        cb_session_list(chat_id, callback_id)
    elif data == "sess:new":
        cb_session_new(chat_id, callback_id)
    elif data == "sess:abort":
        cb_session_abort(chat_id, callback_id)
    elif data == "sess:clear":
        cb_session_clear(chat_id, callback_id)
    elif data == "inst:list":
        cb_instance_list(chat_id, callback_id)
    elif data.startswith("sess:use:"):
        sid = data.split("sess:use:", 1)[1]
        inst = get_active()
        with lock:
            pinned[(chat_id, inst["name"])] = sid
            save_pins()
        saver = f"Сессия закреплена ({inst['name']}). Пишите — уйдёт в неё."
        tg_send(chat_id, saver)
        if callback_id:
            tg("answerCallbackQuery", {"callback_query_id": callback_id})
    elif data.startswith("inst:use:"):
        cb_instance_use(chat_id, data.split("inst:use:", 1)[1], callback_id)


def handle_command(chat_id, text):
    parts = text.split()
    cmd = parts[0].lower().split("@")[0]
    if cmd in ("/start", "/menu", "/help"):
        cb_show_menu(chat_id)
    elif cmd == "/sessions":
        cb_session_list(chat_id)
    elif cmd == "/new":
        cb_session_new(chat_id)
    elif cmd == "/abort":
        cb_session_abort(chat_id)
    elif cmd in ("/exit", "/clear"):
        cb_session_clear(chat_id)
    elif cmd == "/instances":
        cb_instance_list(chat_id)
    elif cmd == "/use":
        name = parts[1] if len(parts) > 1 else ""
        if set_active(name):
            tg_send(chat_id, f"переключился на {name}", keyboard=menu_keyboard())
        else:
            tg_send(chat_id, f"нет инстанса {name!r}")
    elif cmd == "/stop":
        for inst in instances:
            try:
                sid = oc_find_session(inst)
                with http(f"{inst['url']}/session/{sid}/abort", method="POST",
                          headers=auth_headers(inst)) as r:
                    r.read()
            except Exception:
                pass
        tg_send(chat_id, "abort отправлен во все инстансы")
    else:
        tg_send(chat_id, "неизвестная команда; /help — список")


def prompt_flow(chat_id, text):
    inst = get_active()
    try:
        sid = target_session(chat_id, inst)
        print(f"[inject:{inst['name']}] chat={chat_id} sid={sid} text={text[:50]!r}", flush=True)
        with lock:
            pending_reply[(inst["name"], sid)] = chat_id
    except Exception as e:
        pending_reply.clear()
        tg_send(chat_id, f"ошибка opencode ({inst['name']}): {e}")
        return
    try:
        oc_send_prompt(inst, sid, text)
        pinned_title = " (закреплена)" if (chat_id, inst["name"]) in pinned else ""
        tg_send(chat_id, f"отправлено в {inst['name']}{pinned_title}")
    except Exception as e:
        pending_reply.pop((inst["name"], sid), None)
        tg_send(chat_id, f"ошибка: {e}")


def bot_loop():
    offset = 0
    while True:
        try:
            url = f"{TELEGRAM_API}/bot{TOKEN}/getUpdates?timeout=50&offset={offset + 1}"
            with urllib.request.urlopen(url, timeout=70) as r:
                payload = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            print(f"[tg] getUpdates failed: {e}", flush=True)
            time.sleep(5)
            continue
        for upd in payload.get("result", []):
            offset = max(offset, upd["update_id"])
            if upd.get("callback_query"):
                handle_callback(upd["callback_query"])
                continue
            msg = upd.get("message") or {}
            chat_id = (msg.get("chat") or {}).get("id")
            from_id = (msg.get("from") or {}).get("id")
            text = (msg.get("text") or "").strip()
            if not chat_id or not from_id or not text:
                continue
            if from_id not in ALLOWED_IDS:
                tg_send(chat_id, "Доступ запрещён (бот только для владельца).")
                continue
            if text.startswith("/"):
                handle_command(chat_id, text)
            else:
                prompt_flow(chat_id, text)


def main():
    if not TOKEN or "PASTE_TOKEN" in TOKEN:
        print("нет bot_token в config.json", file=sys.stderr)
        sys.exit(1)
    load_pins()
    healthy = []
    for inst in instances:
        try:
            with http(f"{inst['url']}/global/health", headers=auth_headers(inst), timeout=8) as r:
                h = json.loads(r.read().decode("utf-8"))
            if h.get("healthy"):
                healthy.append(inst["name"])
        except Exception as e:
            print(f"инстанс {inst['name']} ({inst['url']}) недоступен", flush=True)
    if not healthy:
        print(
            "ни один opencode-инстанс не доступен: запустите на VPS\n"
            "  opencode --port 4096\n"
            "и дождитесь туннеля home→vps для второго инстанса.\n"
            "Перезапуск через 60 секунд.",
            flush=True,
        )
        time.sleep(60)
        main()
    if get_active()["name"] not in healthy and healthy:
        set_active(healthy[0])
        print(f"активный инстанс не отвечает; переключился на {healthy[0]}", flush=True)
    for inst in instances:
        threading.Thread(target=event_loop, args=(inst,), daemon=True).start()
    print(f"бот запущен; активен: {get_active()['name']}", flush=True)
    bot_loop()


if __name__ == "__main__":
    main()
