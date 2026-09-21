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
CREATE TABLE IF NOT EXISTS notices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL
);
"""


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
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


def legacy_scheduled(conn):
    return conn.execute(
        "SELECT * FROM reminders WHERE scheduled_msg_id IS NOT NULL"
    ).fetchall()


def clear_scheduled_msg_id(conn, reminder_id):
    conn.execute(
        "UPDATE reminders SET scheduled_msg_id = NULL WHERE id = ?",
        (reminder_id,),
    )
    conn.commit()


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
        step_seconds = row["interval_seconds"]
        now = datetime.now()
        if next_run <= now:
            elapsed = int((now - next_run).total_seconds())
            next_run += timedelta(seconds=(elapsed // step_seconds + 1) * step_seconds)
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


def skip_missed(conn, row, now):
    """Skip overdue occurrences and return their count."""
    if row["interval_seconds"] is None:
        delete_reminder(conn, row["id"])
        return 1

    start = datetime.strptime(row["next_run"], DATETIME_FMT)
    step_seconds = row["interval_seconds"]
    elapsed = int((now - start).total_seconds())
    count = elapsed // step_seconds + 1
    if row["repeats_left"] > 0:
        count = min(count, row["repeats_left"])
        remaining = row["repeats_left"] - count
        if remaining == 0:
            delete_reminder(conn, row["id"])
            return count
    else:
        remaining = -1

    next_run = start + timedelta(seconds=(elapsed // step_seconds + 1) * step_seconds)
    conn.execute(
        "UPDATE reminders SET next_run = ?, repeats_left = ?, scheduled_msg_id = NULL"
        " WHERE id = ?",
        (next_run.strftime(DATETIME_FMT), remaining, row["id"]),
    )
    conn.commit()
    return count


def add_notice(conn, text):
    conn.execute("INSERT INTO notices (text) VALUES (?)", (text,))
    conn.commit()


def pending_notices(conn):
    return conn.execute("SELECT * FROM notices ORDER BY id").fetchall()


def delete_notice(conn, notice_id):
    conn.execute("DELETE FROM notices WHERE id = ?", (notice_id,))
    conn.commit()
