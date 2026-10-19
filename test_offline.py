#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проверка без сети и без ключей: расписание, запись, инструменты и цикл агента на поддельной модели.

    python3 test_offline.py
"""
import json
import os
import tempfile
from datetime import datetime, timedelta

os.environ["AI_ADMIN_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")

import agent  # noqa: E402
import db  # noqa: E402
import schedule as S  # noqa: E402
import tools  # noqa: E402

db.init()
sent = []
CTX = {"chat_id": 111, "notify": sent.append, "user": "@test"}


def check(cond, what):
    print(("ok   " if cond else "FAIL ") + what)
    if not cond:
        raise SystemExit(1)


# --- телефоны
check(S.norm_phone("8 (921) 123-45-67") == "+79211234567", "телефон 8… → +7…")
check(S.norm_phone("9211234567") == "+79211234567", "телефон без кода страны")
check(S.norm_phone("12345") is None, "мусор вместо телефона отвергается")

# --- окна
slots = S.free_slots("hygiene", days=14)
check(len(slots) > 0, "есть окна на гигиену: %d" % len(slots))
earliest = S.now() + timedelta(minutes=S.LEAD)
check(all(datetime.strptime(s["start"], S.FMT) >= earliest for s in slots), "окна не раньше чем через час")
check(all(s["doctor_id"] == "morozova" for s in slots), "гигиену ведёт только гигиенист")
check(all(datetime.strptime(s["start"], S.FMT).weekday() < 5 for s in slots), "гигиенист не работает в выходные")
eve = S.free_slots("consult", days=14, part_of_day="evening")
check(eve and all(datetime.strptime(s["start"], S.FMT).hour >= 17 for s in eve), "фильтр «вечер»")

# --- запись и конфликт
s0 = slots[0]
r = json.loads(tools.run_json("book_appointment", json.dumps({
    "service_id": "hygiene", "doctor_id": s0["doctor_id"], "start": s0["start"],
    "name": "Анна", "phone": "89211234567", "consent": False}), CTX))
check("error" in r, "без согласия на ПД не записывает")
r = tools.run("book_appointment", {
    "service_id": "hygiene", "doctor_id": s0["doctor_id"], "start": s0["start"],
    "name": "Анна", "phone": "89211234567", "consent": True, "comment": "кровоточат дёсны"}, CTX)
check(r.get("id") and r["phone"] == "+79211234567", "запись создана #%s" % r.get("id"))
check(sent and "Новая запись" in sent[-1] and "кровоточат" in sent[-1], "администратор получил уведомление")
r2 = S.book(222, "hygiene", s0["doctor_id"], s0["start"], "Борис", "+79990000000")
check("error" in r2, "то же окно второй раз не занять")
check(s0["start"] not in [s["start"] for s in S.free_slots("hygiene", days=14)], "занятое окно пропало из выдачи")
check("error" in S.book(111, "implant_consult", "morozova", s0["start"], "Анна", "+79211234567"),
      "услуга не того врача отвергается")
check("error" in S.book(111, "consult", "ivanova", "2020-01-01 10:00", "Анна", "+79211234567"),
      "прошедшее время отвергается")

# --- мои записи, чужая отмена, своя отмена
mine = tools.run("my_appointments", {}, CTX)["appointments"]
check(len(mine) == 1 and mine[0]["id"] == r["id"], "my_appointments видит запись")
check("error" in S.cancel(r["id"], chat_id=999), "чужую запись не отменить")
c = tools.run("cancel_appointment", {"booking_id": r["id"]}, CTX)
check(c.get("status") == "cancelled" and "Отмена" in sent[-1], "отмена и уведомление")
check(s0["start"] in [s["start"] for s in S.free_slots("hygiene", days=14)], "окно снова свободно")

# --- вызов администратора
h = tools.run("call_admin", {"reason": "вопрос по ДМС Согаз", "phone": "+7 921 000 00 00", "urgent": False}, CTX)
check(h.get("ok") and "ДМС" in sent[-1] and "+79210000000" in sent[-1], "call_admin доходит до администратора")


# --- цикл агента на поддельной модели: найти окно → записать → ответить текстом
class FakeLLM:
    def __init__(self):
        self.step = 0
        self.seen = []

    def chat(self, messages, tools_=None):
        self.seen.append(messages)
        self.step += 1
        if self.step == 1:
            return {"content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {
                "name": "find_slots", "arguments": json.dumps({"service_id": "acute"})}}]}
        if self.step == 2:
            first = json.loads(messages[-1]["content"])["slots"][0]
            return {"content": None, "tool_calls": [{"id": "c2", "type": "function", "function": {
                "name": "book_appointment", "arguments": json.dumps({
                    "service_id": "acute", "doctor_id": first["doctor_id"], "start": first["start"],
                    "name": "Олег", "phone": "+7 999 111-22-33", "consent": True})}}]}
        return {"content": "Записала вас, Олег!"}


fake = FakeLLM()
ans = agent.reply(fake, 333, "болит зуб, запишите на ближайшее", sent.append, "@oleg")
check(ans == "Записала вас, Олег!", "агент вернул текст после двух инструментов")
check(len(S.upcoming(chat_id=333)) == 1, "агент реально записал пациента")
check(db.history(333)[-1]["content"] == ans, "история диалога сохранена")
sys_prompt = fake.seen[0][0]["content"]
check("acute" in sys_prompt and "Морозова" in sys_prompt and "Сейчас:" in sys_prompt, "в промпте база клиники и текущее время")

print("\nВсё прошло.")
