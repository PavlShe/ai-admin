# -*- coding: utf-8 -*-
"""SQLite: записи на приём, история диалогов, вызовы администратора."""
import os
import sqlite3
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB = Path(os.environ.get("AI_ADMIN_DB") or BASE / "admin.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS bookings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    name       TEXT NOT NULL,
    phone      TEXT NOT NULL,
    service_id TEXT NOT NULL,
    doctor_id  TEXT NOT NULL,
    start      TEXT NOT NULL,          -- 'YYYY-MM-DD HH:MM', местное время клиники
    end        TEXT NOT NULL,
    comment    TEXT DEFAULT '',
    status     TEXT DEFAULT 'active',  -- active | cancelled
    reminded   INTEGER DEFAULT 0,
    created    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS bookings_doc ON bookings(doctor_id, start);
CREATE TABLE IF NOT EXISTS messages (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    role    TEXT NOT NULL,
    content TEXT NOT NULL,
    ts      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_chat ON messages(chat_id, id);
CREATE TABLE IF NOT EXISTS handoffs (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    reason  TEXT NOT NULL,
    phone   TEXT DEFAULT '',
    ts      TEXT NOT NULL
);
"""


def conn():
    c = sqlite3.connect(str(DB), timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init():
    with conn() as c:
        c.executescript(SCHEMA)


def history(chat_id, limit=16):
    with conn() as c:
        rows = c.execute(
            "SELECT role, content FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def save_message(chat_id, role, content, ts):
    with conn() as c:
        c.execute("INSERT INTO messages(chat_id, role, content, ts) VALUES (?,?,?,?)",
                  (chat_id, role, content, ts))


def clear_history(chat_id):
    with conn() as c:
        c.execute("DELETE FROM messages WHERE chat_id=?", (chat_id,))


def save_handoff(chat_id, reason, phone, ts):
    with conn() as c:
        c.execute("INSERT INTO handoffs(chat_id, reason, phone, ts) VALUES (?,?,?,?)",
                  (chat_id, reason, phone, ts))
