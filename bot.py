#!/usr/bin/env python3
import fcntl
import json
import os
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
_lock_fd = None
CONFIG_PATH = os.path.join(BASE, "config.json")
STATE_PATH = os.path.join(BASE, "bot_state.json")
LOCK_PATH = os.path.join(BASE, "bot.lock")
TG_CHUNK_LIMIT = 3800
ALLOWED_IDS = {458073613}
STARTED_AT = time.strftime("%Y-%m-%d %H:%M:%S")


def bot_version():
    import subprocess
    try:
        rev = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=BASE,
            stderr=subprocess.DEVNULL, text=True,
        ).strip()
        dirty = subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=BASE,
            stderr=subprocess.DEVNULL, text=True,
        ).strip()
        return rev + ("+dirty" if dirty else "")
    except Exception:
        return "unknown"


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def acquire_singleton():
    global _lock_fd
    if _lock_fd is not None:
        return
    _lock_fd = open(LOCK_PATH, "a+", encoding="utf-8")
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("бот уже запущен: занят bot.lock", file=sys.stderr)
        sys.exit(1)
    _lock_fd.seek(0)
    _lock_fd.truncate()
    _lock_fd.write(str(os.getpid()))
    _lock_fd.flush()


def load_dotenv(path):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


load_dotenv(os.path.join(BASE, ".env"))

TELEGRAM_API = os.environ.get("TELEGRAM_API", "").strip() or "https://api.telegram.org"
TG_CTX = ssl._create_unverified_context() if "127.0.0.1" in TELEGRAM_API else None

TELEGRAM_PROXY = os.environ.get("TELEGRAM_PROXY", "").strip()
tg_opener = None
if TELEGRAM_PROXY:
    tg_ctx = ssl._create_unverified_context()
    tg_opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": TELEGRAM_PROXY, "https": TELEGRAM_PROXY}),
        urllib.request.HTTPSHandler(context=tg_ctx),
    )


def _tg_open(req, timeout=70):
    if tg_opener is not None:
        return tg_opener.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout, context=TG_CTX)

CFG = load_json(CONFIG_PATH, {})
TOKEN = (os.environ.get("BOT_TOKEN") or CFG.get("bot_token") or "").strip()

instances = list(CFG.get("instances") or [])

state = load_json(STATE_PATH, {})
active_name = state.get("instance")

pending_reply = {}
notified_perms = set()
last_assistant = {}
current_msg = {}
pinned = {}
busy = {}
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
    env_key = inst.get("password_env")
    if not pw and env_key:
        pw = os.environ.get(env_key) or ""
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
    req = urllib.request.Request(url, data=data)
    for _ in range(retries):
        try:
            with _tg_open(req) as r:
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


def oc_session_exists(inst, session_id):
    try:
        with http(f"{inst['url']}/session/{session_id}", headers=auth_headers(inst)) as r:
            return r.status == 200
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise


def oc_send_prompt(inst, session_id, text):
    body = {"parts": [{"type": "text", "text": text}]}
    if CFG.get("agent"):
        body["agent"] = CFG["agent"]
    with http(f"{inst['url']}/session/{session_id}/prompt_async", method="POST",
              body=body, headers=auth_headers(inst)) as r:
        status = r.status
    if status not in (200, 204):
        raise RuntimeError(f"opencode вернул {status}")


BOT_COMMANDS = [
    ("start", "меню управления"),
    ("menu", "меню управления"),
    ("help", "список команд"),
    ("sessions", "список сессий"),
    ("new", "новая сессия"),
    ("abort", "прервать текущую сессию"),
    ("clear", "открепить сессию"),
    ("instances", "список инстансов"),
    ("use", "переключить инстанс: /use home"),
    ("stop", "прервать во всех инстансах"),
    ("version", "версия, pid и активный инстанс"),
]


def help_text():
    lines = ["Команды:"]
    for name, desc in BOT_COMMANDS:
        lines.append(f"/{name} — {desc}")
    lines.append("")
    lines.append("Любой текст без «/» уходит в текущую сессию opencode.")
    return "\n".join(lines)


def menu_keyboard():
    return {"inline_keyboard": [
        [{"text": "Сессии", "callback_data": "sess:list"},
         {"text": "Новая сессия", "callback_data": "sess:new"}],
        [{"text": "Прервать (abort)", "callback_data": "sess:abort"},
         {"text": "Открепить сессию", "callback_data": "sess:clear"}],
        [{"text": "Инстансы", "callback_data": "inst:list"},
         {"text": "Стоп во все", "callback_data": "sess:stopall"}],
        [{"text": "Помощь", "callback_data": "help"}],
    ]}


def target_session(chat_id, inst):
    key = (chat_id, inst["name"])
    with lock:
        sid = pinned.get(key)
    if sid:
        if oc_session_exists(inst, sid):
            return sid
        with lock:
            pinned.pop(key, None)
            save_pins()
        print(f"[pin:{inst['name']}] удалена несуществующая сессия {sid}", flush=True)
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


def notify_permission(inst, props):
    pid = props.get("id")
    sid = props.get("sessionID")
    if not pid or not sid:
        return
    with lock:
        if pid in notified_perms:
            return
        chat_id = pending_reply.get((inst["name"], sid))
        if not chat_id:
            for (chat, name), pinned_sid in pinned.items():
                if name == inst["name"] and pinned_sid == sid:
                    chat_id = chat
                    break
        if not chat_id and ALLOWED_IDS:
            chat_id = next(iter(ALLOWED_IDS))
        if not chat_id:
            return
        notified_perms.add(pid)
    pattern = props.get("permission") or "permission"
    patterns = props.get("patterns") or []
    details = "\n".join(str(p) for p in patterns[:8])
    text = f"Требуется разрешение ({inst['name']}): {pattern}"
    if details:
        text += f"\n{details}"
    keyboard = {"inline_keyboard": [
        [{"text": "Разрешить", "callback_data": f"perm:once:{inst['name']}:{pid}"},
         {"text": "Всегда", "callback_data": f"perm:always:{inst['name']}:{pid}"}],
        [{"text": "Отклонить", "callback_data": f"perm:reject:{inst['name']}:{pid}"}],
    ]}
    tg_send(chat_id, text, keyboard=keyboard)


def check_pending_permissions():
    for inst in instances:
        try:
            with http(f"{inst['url']}/permission", headers=auth_headers(inst), timeout=8) as r:
                perms = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            print(f"[perm:{inst['name']}] {e}", flush=True)
            continue
        for perm in perms or []:
            notify_permission(inst, perm)


def finish_reply(inst, sid):
    key = (inst["name"], sid)
    with lock:
        busy.pop(key, None)
        chat_id = pending_reply.pop(key, None)
        mid = current_msg.pop(key, None)
        text = last_assistant.pop(mid, "") if mid else ""
    if chat_id and text:
        print(f"[reply:{inst['name']}] chat={chat_id} len={len(text)}", flush=True)
        tg_send(chat_id, text)


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
    elif etype == "permission.asked":
        notify_permission(inst, props)
    elif etype == "permission.replied":
        with lock:
            notified_perms.discard(props.get("id") or props.get("requestID"))
    elif etype == "session.status":
        sid = props.get("sessionID")
        stype = (props.get("status") or {}).get("type")
        if not sid:
            return
        if stype == "busy":
            with lock:
                busy[key(sid)] = True
        elif stype == "idle":
            finish_reply(inst, sid)
    elif etype == "session.idle":
        sid = props.get("sessionID")
        if sid:
            finish_reply(inst, sid)


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
        busy_now = {sid for (name, sid) in busy if name == inst["name"]}
    rows = []
    for s in recent:
        title = (s.get("title") or s["id"])[:40]
        marks = ""
        if s["id"] in busy_now:
            marks += "▶ "
        if s["id"] == cur:
            marks += "• "
        rows.append([{"text": marks + title,
                      "callback_data": f"sess:use:{inst['name']}:{s['id']}"}])
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


def drop_reply(inst_name, sid):
    with lock:
        pending_reply.pop((inst_name, sid), None)
        mid = current_msg.pop((inst_name, sid), None)
        if mid:
            last_assistant.pop(mid, None)


def cb_session_abort(chat_id, callback_id=None):
    inst = get_active()
    name = inst["name"]
    try:
        with lock:
            pending = {sid for (n, sid), chat in pending_reply.items()
                       if n == name and chat == chat_id}
            targets = set(pending)
            sid = pinned.get((chat_id, name))
            if sid:
                targets.add(sid)
            running = {sid for sid in targets if (name, sid) in busy}
        if not targets:
            targets = {oc_find_session(inst)}
        print(f"[abort:{name}] chat={chat_id} targets={sorted(targets)} "
              f"running={sorted(running)} pending={sorted(pending)}", flush=True)
        stopped = []
        for sid in sorted(targets):
            with http(f"{inst['url']}/session/{sid}/abort", method="POST",
                      headers=auth_headers(inst)) as r:
                r.read()
            if sid in running or sid in pending:
                drop_reply(name, sid)
                stopped.append(sid)
            else:
                stopped.append(f"{sid} (не запущена)")
        tg_send(chat_id, "abort отправлен: " + ", ".join(stopped))
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


def cb_session_stopall(chat_id, callback_id=None):
    stopped = []
    for inst in instances:
        try:
            sid = oc_find_session(inst)
            with http(f"{inst['url']}/session/{sid}/abort", method="POST",
                      headers=auth_headers(inst)) as r:
                r.read()
            drop_reply(inst["name"], sid)
            stopped.append(inst["name"])
        except Exception:
            pass
    if stopped:
        tg_send(chat_id, "abort отправлен: " + ", ".join(stopped))
    else:
        tg_send(chat_id, "не удалось отправить abort ни в один инстанс")
    if callback_id:
        tg("answerCallbackQuery", {"callback_query_id": callback_id})


def cb_help(chat_id, callback_id=None):
    tg_send(chat_id, help_text(), keyboard=menu_keyboard())
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
        tg_send(chat_id, f"переключился на {name}", keyboard=menu_keyboard())
    else:
        tg_send(chat_id, f"нет инстанса {name!r}")
    if callback_id:
        tg("answerCallbackQuery", {"callback_query_id": callback_id})


def cb_permission_reply(chat_id, action, name, pid, callback_id=None):
    inst = inst_by_name(name)
    if not inst:
        tg_send(chat_id, f"нет инстанса {name!r}")
    elif action not in ("once", "always", "reject"):
        tg_send(chat_id, f"неизвестное действие {action!r}")
    else:
        try:
            with http(f"{inst['url']}/permission/{pid}/reply", method="POST",
                      body={"reply": action}, headers=auth_headers(inst)) as r:
                r.read()
            labels = {"once": "разрешено", "always": "разрешено всегда", "reject": "отклонено"}
            tg_send(chat_id, f"Разрешение {labels[action]} ({name})")
            with lock:
                notified_perms.discard(pid)
        except Exception as e:
            tg_send(chat_id, f"не удалось ответить на разрешение: {e}")
    if callback_id:
        tg("answerCallbackQuery", {"callback_query_id": callback_id})


def handle_callback(cb):
    chat_id = ((cb.get("message") or {}).get("chat") or {}).get("id")
    callback_id = cb.get("id")
    from_id = (cb.get("from") or {}).get("id")
    data = (cb.get("data") or "").strip()
    if not chat_id or from_id not in ALLOWED_IDS:
        return
    print(f"[cb] chat={chat_id} data={data!r}", flush=True)
    if data == "menu":
        cb_show_menu(chat_id, callback_id)
    elif data == "help":
        cb_help(chat_id, callback_id)
    elif data == "sess:list":
        cb_session_list(chat_id, callback_id)
    elif data == "sess:new":
        cb_session_new(chat_id, callback_id)
    elif data == "sess:abort":
        cb_session_abort(chat_id, callback_id)
    elif data == "sess:clear":
        cb_session_clear(chat_id, callback_id)
    elif data == "sess:stopall":
        cb_session_stopall(chat_id, callback_id)
    elif data == "inst:list":
        cb_instance_list(chat_id, callback_id)
    elif data.startswith("perm:"):
        parts = data.split(":", 3)
        if len(parts) == 4:
            cb_permission_reply(chat_id, parts[1], parts[2], parts[3], callback_id)
    elif data.startswith("sess:use:"):
        name, _, sid = data.split("sess:use:", 1)[1].partition(":")
        inst = inst_by_name(name)
        if not inst:
            tg_send(chat_id, f"нет инстанса {name!r}")
        else:
            with lock:
                pinned[(chat_id, inst["name"])] = sid
                save_pins()
            tg_send(chat_id, f"Сессия закреплена ({inst['name']}). Пишите — уйдёт в неё.")
        if callback_id:
            tg("answerCallbackQuery", {"callback_query_id": callback_id})
    elif data.startswith("inst:use:"):
        cb_instance_use(chat_id, data.split("inst:use:", 1)[1], callback_id)


def handle_command(chat_id, text):
    print(f"[cmd] chat={chat_id} text={text[:80]!r}", flush=True)
    parts = text.split()
    cmd = parts[0].lower().split("@")[0]
    if cmd in ("/start", "/menu"):
        cb_show_menu(chat_id)
    elif cmd == "/help":
        cb_help(chat_id)
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
        cb_session_stopall(chat_id)
    elif cmd == "/version":
        tg_send(chat_id, f"pid={os.getpid()} started={STARTED_AT}\n"
                         f"code={bot_version()}\nactive={get_active()['name']}")
    else:
        tg_send(chat_id, "неизвестная команда; /help — список")


def prompt_flow(chat_id, text):
    inst = get_active()
    try:
        sid = target_session(chat_id, inst)
        print(f"[inject:{inst['name']}] chat={chat_id} sid={sid} text={text[:50]!r}", flush=True)
    except Exception as e:
        pending_reply.clear()
        tg_send(chat_id, f"ошибка opencode ({inst['name']}): {e}")
        return

    def send(target_sid):
        with lock:
            pending_reply[(inst["name"], target_sid)] = chat_id
        oc_send_prompt(inst, target_sid, text)

    try:
        try:
            send(sid)
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            with lock:
                pending_reply.pop((inst["name"], sid), None)
                if pinned.get((chat_id, inst["name"])) == sid:
                    pinned.pop((chat_id, inst["name"]), None)
                    save_pins()
            sid = oc_find_session(inst)
            print(f"[inject:{inst['name']}] сессия пропала, повтор в {sid}", flush=True)
            send(sid)
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
            with _tg_open(urllib.request.Request(url)) as r:
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
    acquire_singleton()
    if not TOKEN or "PASTE_TOKEN" in TOKEN:
        print("нет bot_token в config.json", file=sys.stderr)
        sys.exit(1)
    load_pins()
    tg("setMyCommands", {"commands": json.dumps(
        [{"command": name, "description": desc} for name, desc in BOT_COMMANDS]
    )})
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
    check_pending_permissions()
    for inst in instances:
        threading.Thread(target=event_loop, args=(inst,), daemon=True).start()
    print(f"бот запущен; активен: {get_active()['name']}", flush=True)
    bot_loop()


if __name__ == "__main__":
    main()
