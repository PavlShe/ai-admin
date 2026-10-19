#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ИИ-администратор стоматологии в Telegram.

Long-polling getUpdates, каждое сообщение — в своём потоке (по чату — строго по очереди).
Чат администратора (admin_chat_id) получает уведомления о записях и отменах
и понимает команды: /today /tomorrow /week /cancel N.
Фоном раз в 5 минут — напоминания пациентам за сутки до приёма.
"""
import json
import logging
import threading
import time
import urllib.parse
import urllib.request
from datetime import timedelta
from pathlib import Path

import agent
import db
import schedule as S
from llm import LLM

BASE = Path(__file__).resolve().parent
CFG = json.loads((BASE / "config.json").read_text())
(BASE / "logs").mkdir(exist_ok=True)
logging.basicConfig(
    filename=str(BASE / "logs" / "bot.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("bot")

TG = "https://api.telegram.org/bot%s/" % CFG["telegram_token"]
ADMIN = int(CFG["admin_chat_id"])
_tg_proxy = CFG.get("telegram_proxy")
_tg = urllib.request.build_opener(
    *([urllib.request.ProxyHandler({"https": _tg_proxy, "http": _tg_proxy})] if _tg_proxy else []))
llm = LLM(CFG["llm_base_url"], CFG["llm_api_key"], CFG["llm_model"], CFG.get("llm_proxy"))

_locks = {}
_locks_guard = threading.Lock()

GREETING = ("Здравствуйте! Я виртуальный администратор клиники «%s» 🦷\n\n"
            "Подскажу цены и свободное время, запишу к врачу, перенесу или отменю приём — в любое время суток.\n\n"
            "Расскажите, что вас беспокоит или к какому врачу хотите записаться?") % S.CLINIC["name"].split("«")[-1].rstrip("»")
DEMO_NOTE = "\n\n(Это демонстрация: клиника, врачи и цены вымышленные.)"


def tg(method, timeout=40, **params):
    data = urllib.parse.urlencode({k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
                                   for k, v in params.items()}).encode()
    with _tg.open(TG + method, data=data, timeout=timeout) as r:
        return json.loads(r.read())


def send(chat_id, text):
    for i in range(0, len(text), 4000):  # лимит Telegram — 4096 символов
        try:
            tg("sendMessage", chat_id=chat_id, text=text[i:i + 4000], disable_web_page_preview="true")
        except Exception as e:
            log.error("sendMessage %s: %s", chat_id, e)


def notify_admin(text):
    send(ADMIN, text)


def _lock(chat_id):
    with _locks_guard:
        return _locks.setdefault(chat_id, threading.Lock())


def _who(msg):
    u = msg.get("from") or {}
    name = " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x)
    return ("@" + u["username"] + " " if u.get("username") else "") + (name or str(u.get("id", "")))


def _list(title, rows):
    if not rows:
        return title + ": записей нет"
    return title + ":\n" + "\n".join("#%d %s — %s, %s (%s)%s" % (
        b["id"], b["when"], b["name"], b["phone"], b["service"].lower(),
        ", " + b["doctor"].split()[0]) for b in rows)


def admin_command(text):
    t = S.now()
    day = lambda n: (t + timedelta(days=n)).strftime("%Y-%m-%d")  # noqa: E731
    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0]
    if cmd == "/today":
        return _list("Сегодня", S.upcoming(date_from=day(0), date_to=day(1)))
    if cmd == "/tomorrow":
        return _list("Завтра", S.upcoming(date_from=day(1), date_to=day(2)))
    if cmd == "/week":
        return _list("Ближайшие 7 дней", S.upcoming(date_from=day(0), date_to=day(7)))
    if cmd == "/cancel":
        if not arg.strip().isdigit():
            return "Формат: /cancel 12"
        b = S.cancel(int(arg))
        if "error" in b:
            return b["error"]
        send(b["chat_id"], "Ваша запись на %s к врачу %s отменена клиникой. Напишите, если подобрать другое время." % (
            b["when"], b["doctor"]))
        return "Запись #%d отменена, пациент уведомлён" % b["id"]
    return "Команды: /today /tomorrow /week /cancel N"


def handle(msg):
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()
    with _lock(chat_id):
        if chat_id == ADMIN:
            if text.startswith("/"):
                send(chat_id, admin_command(text))
            return
        if msg["chat"].get("type") != "private":
            return
        if not text:
            send(chat_id, "Пока я понимаю только текст — напишите, пожалуйста, словами 🙂")
            return
        if text.startswith("/start"):
            db.clear_history(chat_id)
            send(chat_id, GREETING + (DEMO_NOTE if S.CLINIC.get("demo") else ""))
            return
        if text.startswith("/reset"):
            db.clear_history(chat_id)
            send(chat_id, "Начнём сначала. Чем могу помочь?")
            return
        try:
            tg("sendChatAction", chat_id=chat_id, action="typing")
        except Exception:
            pass
        try:
            answer = agent.reply(llm, chat_id, text[:2000], notify_admin, _who(msg))
        except Exception as e:
            log.exception("reply %s", chat_id)
            answer = "Извините, у меня техническая заминка. Позвоните нам: %s — или напишите чуть позже." % S.CLINIC["phone"]
            notify_admin("⚠️ Ошибка бота в диалоге с %s: %s" % (_who(msg), str(e)[:300]))
        send(chat_id, answer)


def reminders():
    while True:
        try:
            for b in S.due_reminders():
                send(b["chat_id"], "Напоминаем: %s, вы записаны в «%s» — %s, врач %s.\nАдрес: %s\n"
                                   "Если планы изменились — напишите здесь, перенесём." % (
                         b["name"], S.CLINIC["name"].split("«")[-1].rstrip("»"), b["when"], b["doctor"],
                         S.CLINIC["address"]))
                log.info("напоминание #%d", b["id"])
        except Exception:
            log.exception("reminders")
        time.sleep(300)


def main():
    db.init()
    threading.Thread(target=reminders, daemon=True).start()
    offset = None
    log.info("старт, модель %s", CFG["llm_model"])
    while True:
        try:
            params = {"timeout": 30, "allowed_updates": ["message"]}
            if offset:
                params["offset"] = offset
            res = tg("getUpdates", timeout=40, **params)
            for upd in res.get("result", []):
                offset = upd["update_id"] + 1
                if "message" in upd:
                    threading.Thread(target=handle, args=(upd["message"],), daemon=True).start()
        except Exception as e:
            log.warning("getUpdates: %s", e)
            time.sleep(3)


if __name__ == "__main__":
    main()
