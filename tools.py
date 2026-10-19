# -*- coding: utf-8 -*-
"""Инструменты, которые модель вызывает сама: окна, запись, отмена, вызов администратора."""
import json
from datetime import datetime

import db
import schedule as S

SERVICE_IDS = list(S.SERVICES)
DOCTOR_IDS = list(S.DOCTORS)

TOOLS = [
    {"type": "function", "function": {
        "name": "find_slots",
        "description": "Свободные окна под услугу. Вызывай всегда перед тем, как предлагать время — не придумывай окна сам.",
        "parameters": {"type": "object", "properties": {
            "service_id": {"type": "string", "enum": SERVICE_IDS},
            "doctor_id": {"type": "string", "enum": DOCTOR_IDS, "description": "Если пациент хочет к конкретному врачу"},
            "date_from": {"type": "string", "description": "YYYY-MM-DD, с какого дня искать (по умолчанию сегодня)"},
            "days": {"type": "integer", "description": "Сколько дней просматривать, 1–21 (по умолчанию 7)"},
            "part_of_day": {"type": "string", "enum": ["morning", "day", "evening"]},
        }, "required": ["service_id"]},
    }},
    {"type": "function", "function": {
        "name": "book_appointment",
        "description": "Записать пациента. Только после того, как пациент выбрал окно, назвал имя и телефон и дал согласие на обработку персональных данных.",
        "parameters": {"type": "object", "properties": {
            "service_id": {"type": "string", "enum": SERVICE_IDS},
            "doctor_id": {"type": "string", "enum": DOCTOR_IDS},
            "start": {"type": "string", "description": "YYYY-MM-DD HH:MM — ровно как в ответе find_slots"},
            "name": {"type": "string"},
            "phone": {"type": "string"},
            "consent": {"type": "boolean", "description": "Пациент явно согласился на обработку персональных данных"},
            "comment": {"type": "string", "description": "Жалоба или пожелание пациента своими словами — для врача"},
        }, "required": ["service_id", "doctor_id", "start", "name", "phone", "consent"]},
    }},
    {"type": "function", "function": {
        "name": "my_appointments",
        "description": "Предстоящие записи этого пациента (для переноса, отмены, напоминания о времени).",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "cancel_appointment",
        "description": "Отменить запись пациента. Для переноса: сначала запиши в новое окно, потом отмени старое.",
        "parameters": {"type": "object", "properties": {
            "booking_id": {"type": "integer"},
        }, "required": ["booking_id"]},
    }},
    {"type": "function", "function": {
        "name": "call_admin",
        "description": "Передать диалог живому администратору: жалоба, ДМС, вопрос не из базы, пациент просит человека, срочная ситуация.",
        "parameters": {"type": "object", "properties": {
            "reason": {"type": "string", "description": "Суть вопроса одной-двумя фразами"},
            "phone": {"type": "string", "description": "Телефон для обратного звонка, если пациент дал"},
            "urgent": {"type": "boolean"},
        }, "required": ["reason"]},
    }},
]


def run(name, args, ctx):
    """Выполнить инструмент. ctx: chat_id, user (подпись из Telegram), notify(text) — сообщение админу."""
    try:
        if name == "find_slots":
            d = args.get("date_from")
            d = datetime.strptime(d, "%Y-%m-%d").date() if d else None
            slots = S.free_slots(args["service_id"], args.get("doctor_id"), d,
                                 args.get("days") or 7, args.get("part_of_day"))
            return {"slots": slots} if slots else {"slots": [], "hint": "окон нет — предложи другие дни, врача или время суток"}

        if name == "book_appointment":
            if not args.get("consent"):
                return {"error": "нет согласия на обработку персональных данных — спроси его у пациента"}
            b = S.book(ctx["chat_id"], args.get("service_id"), args.get("doctor_id"), args.get("start"),
                       args.get("name"), args.get("phone"), args.get("comment", ""))
            if "error" not in b:
                ctx["notify"]("🦷 Новая запись #%d\nПациент: %s, %s\nУслуга: %s\nВрач: %s\nКогда: %s%s\nTelegram: %s" % (
                    b["id"], b["name"], b["phone"], b["service"], b["doctor"], b["when"],
                    "\nЖалоба: " + b["comment"] if b["comment"] else "", ctx.get("user", "")))
            return b

        if name == "my_appointments":
            return {"appointments": S.upcoming(chat_id=ctx["chat_id"])}

        if name == "cancel_appointment":
            b = S.cancel(int(args["booking_id"]), chat_id=ctx["chat_id"])
            if "error" not in b:
                ctx["notify"]("❌ Отмена записи #%d\n%s, %s\n%s — %s, %s" % (
                    b["id"], b["name"], b["phone"], b["service"], b["doctor"], b["when"]))
            return b

        if name == "call_admin":
            phone = S.norm_phone(args.get("phone")) or args.get("phone") or ""
            db.save_handoff(ctx["chat_id"], args["reason"], phone, S.now().strftime(S.FMT))
            ctx["notify"]("%s Нужен администратор\n%s\nТелефон: %s\nTelegram: %s" % (
                "🚨" if args.get("urgent") else "🙋", args["reason"], phone or "—", ctx.get("user", "")))
            return {"ok": True, "hint": "скажи пациенту, что администратор свяжется в рабочее время клиники"}

        return {"error": "нет такого инструмента"}
    except (KeyError, ValueError, TypeError) as e:
        return {"error": "неверные параметры: %s" % e}


def run_json(name, raw_args, ctx):
    try:
        args = json.loads(raw_args or "{}")
    except ValueError:
        return json.dumps({"error": "аргументы не JSON"}, ensure_ascii=False)
    return json.dumps(run(name, args, ctx), ensure_ascii=False)
