"""Хранилище напоминаний (SQLite).

repeats_left: сколько отправок осталось; -1 означает «бессрочно».
interval_seconds: NULL для одноразовых напоминаний.
Время хранится наивным локальным временем хоста — приложение работает
на одной машине, часовой пояс которой должен быть настроен правильно.
"""
import sqlite3
from datetime import datetime, timedelta

DATETIME_FMT = "%Y-%m-%d %H:%M:%S"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target TEXT NOT NULL,
    text TEXT NOT NULL,
    next_run TEXT NOT NULL,
    interval_seconds INTEGER,
    repeats_left INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    scheduled_msg_id INTEGER
);
"""


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(_SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


def _migrate(conn):
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(reminders)")]
    # Ранняя версия хранила интервал в минутах (interval_minutes).
    if "interval_minutes" in cols:
        conn.execute(
            "ALTER TABLE reminders RENAME COLUMN interval_minutes TO interval_seconds"
        )
        conn.execute(
            "UPDATE reminders SET interval_seconds = interval_seconds * 60"
            " WHERE interval_seconds IS NOT NULL"
        )
    # id сообщения в серверном планировщике Telegram появился позже.
    if "scheduled_msg_id" not in cols:
        conn.execute("ALTER TABLE reminders ADD COLUMN scheduled_msg_id INTEGER")


def add_reminder(conn, target, text, next_run, interval_seconds, repeats):
    cur = conn.execute(
        "INSERT INTO reminders"
        " (target, text, next_run, interval_seconds, repeats_left, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            target,
            text,
            next_run.strftime(DATETIME_FMT),
            interval_seconds,
            repeats,
            datetime.now().strftime(DATETIME_FMT),
        ),
    )
    conn.commit()
    return cur.lastrowid


def list_reminders(conn):
    return conn.execute("SELECT * FROM reminders ORDER BY next_run").fetchall()


def get_reminder(conn, reminder_id):
    return conn.execute(
        "SELECT * FROM reminders WHERE id = ?", (reminder_id,)
    ).fetchone()


def set_scheduled_msg_id(conn, reminder_id, msg_id):
    conn.execute(
        "UPDATE reminders SET scheduled_msg_id = ? WHERE id = ?",
        (msg_id, reminder_id),
    )
    conn.commit()


def needing_schedule(conn, not_before):
    """Напоминания, не стоящие в серверном планировщике, до которых ещё
    достаточно времени, чтобы туда попасть."""
    return conn.execute(
        "SELECT * FROM reminders"
        " WHERE scheduled_msg_id IS NULL AND next_run >= ?",
        (not_before.strftime(DATETIME_FMT),),
    ).fetchall()


def delete_reminder(conn, reminder_id):
    cur = conn.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
    conn.commit()
    return cur.rowcount > 0


def next_run_time(conn):
    """Время ближайшего напоминания или None, если их нет."""
    row = conn.execute("SELECT MIN(next_run) AS m FROM reminders").fetchone()
    if row["m"] is None:
        return None
    return datetime.strptime(row["m"], DATETIME_FMT)


def due_reminders(conn, now):
    return conn.execute(
        "SELECT * FROM reminders WHERE next_run <= ? ORDER BY next_run",
        (now.strftime(DATETIME_FMT),),
    ).fetchall()


def advance(conn, row):
    """Списывает одно срабатывание: сдвигает next_run или удаляет напоминание."""
    repeats_left = row["repeats_left"]
    if repeats_left > 0:
        repeats_left -= 1

    if repeats_left == 0 or row["interval_seconds"] is None:
        conn.execute("DELETE FROM reminders WHERE id = ?", (row["id"],))
    else:
        # Если хост был выключен и пропущено несколько интервалов,
        # догонять их не нужно — берём ближайшее будущее время.
        next_run = datetime.strptime(row["next_run"], DATETIME_FMT)
        step = timedelta(seconds=row["interval_seconds"])
        now = datetime.now()
        while next_run <= now:
            next_run += step
        conn.execute(
            "UPDATE reminders SET next_run = ?, repeats_left = ?,"
            " scheduled_msg_id = NULL WHERE id = ?",
            (next_run.strftime(DATETIME_FMT), repeats_left, row["id"]),
        )
    conn.commit()


def postpone(conn, row, minutes):
    """Откладывает напоминание после неудачной отправки."""
    next_run = datetime.now() + timedelta(minutes=minutes)
    conn.execute(
        "UPDATE reminders SET next_run = ? WHERE id = ?",
        (next_run.strftime(DATETIME_FMT), row["id"]),
    )
    conn.commit()
