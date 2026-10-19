# -*- coding: utf-8 -*-
"""Расписание врачей: свободные окна, запись, отмена.

Время хранится строкой 'YYYY-MM-DD HH:MM' в часовом поясе клиники —
строки такого вида сравниваются как даты, поэтому пересечения ищем прямо в SQL.
"""
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import db

BASE = Path(__file__).resolve().parent
CLINIC = json.loads((BASE / "clinic.json").read_text())

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(CLINIC.get("timezone", "Europe/Moscow"))
except Exception:  # нет базы часовых поясов — считаем МСК
    from datetime import timezone
    TZ = timezone(timedelta(hours=3))

STEP = 30          # шаг сетки, минут
LEAD = 60          # не записываем ближе чем за час
PER_DAY = 4        # сколько окон на день показывать, чтобы варианты были на разные дни
FMT = "%Y-%m-%d %H:%M"
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_RU = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

SERVICES = {s["id"]: s for s in CLINIC["services"]}
DOCTORS = {d["id"]: d for d in CLINIC["doctors"]}


def now():
    """Текущее время клиники без tzinfo — в одной системе с хранимыми строками."""
    return datetime.now(TZ).replace(tzinfo=None, second=0, microsecond=0)


def human(ts):
    """'2026-10-20 10:00' → 'вт 20.10 в 10:00'."""
    dt = datetime.strptime(ts, FMT) if isinstance(ts, str) else ts
    return "%s %s в %s" % (DAY_RU[dt.weekday()], dt.strftime("%d.%m"), dt.strftime("%H:%M"))


def norm_phone(s):
    d = re.sub(r"\D", "", s or "")
    if len(d) == 11 and d[0] in "78":
        d = "7" + d[1:]
    elif len(d) == 10 and d[0] == "9":
        d = "7" + d
    else:
        return None
    return "+" + d


def _hours(doctor, day):
    """Рабочие часы врача в этот день: (начало, конец) или None."""
    if day.strftime("%Y-%m-%d") in CLINIC.get("holidays", []):
        return None
    span = doctor["schedule"].get(DAYS[day.weekday()])
    if not span:
        return None
    a, b = span.split("-")
    d = day.strftime("%Y-%m-%d ")
    return datetime.strptime(d + a, FMT), datetime.strptime(d + b, FMT)


def _busy(c, doctor_id, day):
    d = day.strftime("%Y-%m-%d")
    rows = c.execute(
        "SELECT start, end FROM bookings WHERE doctor_id=? AND status='active' AND start LIKE ?",
        (doctor_id, d + "%"),
    ).fetchall()
    return [(datetime.strptime(r["start"], FMT), datetime.strptime(r["end"], FMT)) for r in rows]


def doctors_for(service_id):
    return [d for d in DOCTORS.values() if service_id in d["services"]]


def free_slots(service_id, doctor_id=None, date_from=None, days=7, part_of_day=None, limit=8):
    """Ближайшие свободные окна под услугу.

    part_of_day: morning (до 12), day (12–17), evening (с 17) — фильтр по началу приёма.
    """
    svc = SERVICES[service_id]
    dur = timedelta(minutes=svc["minutes"])
    docs = doctors_for(service_id)
    if doctor_id:
        docs = [d for d in docs if d["id"] == doctor_id]
    earliest = now() + timedelta(minutes=LEAD)
    start_day = max(date_from or earliest.date(), earliest.date())
    days = max(1, min(int(days or 7), 21))

    out = []
    with db.conn() as c:
        for i in range(days):
            day = datetime.combine(start_day + timedelta(days=i), datetime.min.time())
            day_slots = []
            for doc in docs:
                hrs = _hours(doc, day)
                if not hrs:
                    continue
                busy = _busy(c, doc["id"], day)
                t = hrs[0]
                while t + dur <= hrs[1]:
                    if t >= earliest and not any(t < e and t + dur > s for s, e in busy):
                        if part_of_day == "morning" and t.hour >= 12 \
                                or part_of_day == "day" and not 12 <= t.hour < 17 \
                                or part_of_day == "evening" and t.hour < 17:
                            pass
                        else:
                            day_slots.append((t, doc))
                    t += timedelta(minutes=STEP)
            day_slots.sort(key=lambda x: x[0])
            # Разные часы дня, а не четыре окна подряд: берём равномерно по списку
            if len(day_slots) > PER_DAY:
                k = len(day_slots) / PER_DAY
                day_slots = [day_slots[int(j * k)] for j in range(PER_DAY)]
            out += day_slots
            if len(out) >= limit:
                break
    return [{"start": t.strftime(FMT), "when": human(t), "doctor_id": d["id"], "doctor": d["name"]}
            for t, d in out[:limit]]


def book(chat_id, service_id, doctor_id, start, name, phone, comment=""):
    """Записывает пациента. Возвращает dict записи или {'error': ...}."""
    if service_id not in SERVICES:
        return {"error": "нет такой услуги"}
    doc = DOCTORS.get(doctor_id)
    if not doc or service_id not in doc["services"]:
        return {"error": "этот врач не ведёт приём по такой услуге"}
    try:
        t = datetime.strptime(start, FMT)
    except (TypeError, ValueError):
        return {"error": "время в формате YYYY-MM-DD HH:MM"}
    ph = norm_phone(phone)
    if not ph:
        return {"error": "телефон не похож на российский номер, попросите его ещё раз"}
    if not (name or "").strip():
        return {"error": "нужно имя пациента"}
    if t < now() + timedelta(minutes=LEAD):
        return {"error": "это время уже прошло или слишком близко, предложите другое"}
    end = t + timedelta(minutes=SERVICES[service_id]["minutes"])
    hrs = _hours(doc, t)
    if not hrs or t < hrs[0] or end > hrs[1] or (t.minute % STEP):
        return {"error": "врач в это время не принимает, возьмите окно из find_slots"}

    c = db.conn()
    c.isolation_level = None
    try:
        c.execute("BEGIN IMMEDIATE")  # между проверкой и вставкой никто не займёт окно
        clash = c.execute(
            "SELECT 1 FROM bookings WHERE doctor_id=? AND status='active' AND start < ? AND end > ?",
            (doctor_id, end.strftime(FMT), t.strftime(FMT)),
        ).fetchone()
        if clash:
            c.execute("ROLLBACK")
            return {"error": "это окно только что заняли, предложите другое"}
        cur = c.execute(
            "INSERT INTO bookings(chat_id, name, phone, service_id, doctor_id, start, end, comment, created)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (chat_id, name.strip(), ph, service_id, doctor_id, t.strftime(FMT), end.strftime(FMT),
             (comment or "").strip(), now().strftime(FMT)),
        )
        c.execute("COMMIT")
        bid = cur.lastrowid
    finally:
        c.close()
    return get(bid)


def get(booking_id):
    with db.conn() as c:
        r = c.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
    return _view(r) if r else None


def _view(r):
    return {
        "id": r["id"], "name": r["name"], "phone": r["phone"],
        "service": SERVICES[r["service_id"]]["name"], "service_id": r["service_id"],
        "doctor": DOCTORS[r["doctor_id"]]["name"], "doctor_id": r["doctor_id"],
        "start": r["start"], "when": human(r["start"]), "status": r["status"],
        "comment": r["comment"], "chat_id": r["chat_id"],
    }


def upcoming(chat_id=None, date_from=None, date_to=None):
    """Будущие активные записи: пациента (chat_id) или все за период."""
    q, args = "SELECT * FROM bookings WHERE status='active' AND start >= ?", [now().strftime(FMT)]
    if chat_id is not None:
        q += " AND chat_id=?"
        args.append(chat_id)
    if date_from:
        q += " AND start >= ?"
        args.append(date_from)
    if date_to:
        q += " AND start < ?"
        args.append(date_to)
    with db.conn() as c:
        rows = c.execute(q + " ORDER BY start", args).fetchall()
    return [_view(r) for r in rows]


def cancel(booking_id, chat_id=None):
    """Отмена. С chat_id — только свою запись (пациент), без — любую (администратор)."""
    b = get(booking_id)
    if not b or b["status"] != "active" or (chat_id is not None and b["chat_id"] != chat_id):
        return {"error": "активной записи с таким номером нет"}
    with db.conn() as c:
        c.execute("UPDATE bookings SET status='cancelled' WHERE id=?", (booking_id,))
    b["status"] = "cancelled"
    return b


def due_reminders():
    """Записи, которым пора напомнить: за сутки до приёма.

    Записанным меньше чем за сутки напоминание не шлём — они только что договорились.
    """
    t = now()
    with db.conn() as c:
        rows = c.execute(
            "SELECT * FROM bookings WHERE status='active' AND reminded=0 AND start > ? AND start <= ?",
            (t.strftime(FMT), (t + timedelta(hours=24)).strftime(FMT)),
        ).fetchall()
        out = []
        for r in rows:
            fresh = datetime.strptime(r["start"], FMT) - datetime.strptime(r["created"], FMT) < timedelta(hours=24)
            c.execute("UPDATE bookings SET reminded=1 WHERE id=?", (r["id"],))
            if not fresh:
                out.append(_view(r))
    return out
