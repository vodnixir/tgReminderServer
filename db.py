"""Хранилище напоминаний (SQLite).

repeats_left: сколько отправок осталось; -1 означает «бессрочно».
interval_seconds: NULL для одноразовых напоминаний.
Время хранится наивным локальным временем хоста — приложение работает
на одной машине, часовой пояс которой должен быть настроен правильно.
"""
import sqlite3
from datetime import datetime, timedelta

DATETIME_FMT = "%Y-%m-%d %H:%M:%S"
CONFIRM_RETRY_SECONDS = 600

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target TEXT NOT NULL,
    text TEXT NOT NULL,
    next_run TEXT NOT NULL,
    interval_seconds INTEGER,
    repeats_left INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    scheduled_msg_id INTEGER,
    weekdays TEXT,
    weekly_time TEXT,
    paused INTEGER NOT NULL DEFAULT 0,
    confirmation_required INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS notices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reminder_id INTEGER NOT NULL,
    target TEXT NOT NULL,
    text TEXT NOT NULL,
    scheduled_for TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT,
    message_id INTEGER,
    source_id INTEGER,
    chat_id INTEGER,
    pending_id INTEGER,
    confirmation_requested INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS history_message_idx ON history(message_id);
CREATE INDEX IF NOT EXISTS history_reminder_idx ON history(reminder_id, id);
CREATE TABLE IF NOT EXISTS pending_confirmations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reminder_id INTEGER NOT NULL UNIQUE,
    target TEXT NOT NULL,
    text TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    next_retry TEXT NOT NULL,
    retry_seconds INTEGER NOT NULL DEFAULT 600,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS pending_next_retry_idx ON pending_confirmations(next_retry);
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
    if "weekdays" not in cols:
        conn.execute("ALTER TABLE reminders ADD COLUMN weekdays TEXT")
    if "weekly_time" not in cols:
        conn.execute("ALTER TABLE reminders ADD COLUMN weekly_time TEXT")
    if "paused" not in cols:
        conn.execute("ALTER TABLE reminders ADD COLUMN paused INTEGER NOT NULL DEFAULT 0")
    if "confirmation_required" not in cols:
        conn.execute("ALTER TABLE reminders ADD COLUMN confirmation_required INTEGER NOT NULL DEFAULT 0")
    history_cols = [r["name"] for r in conn.execute("PRAGMA table_info(history)")]
    if "chat_id" not in history_cols:
        conn.execute("ALTER TABLE history ADD COLUMN chat_id INTEGER")
    if "pending_id" not in history_cols:
        conn.execute("ALTER TABLE history ADD COLUMN pending_id INTEGER")
    if "confirmation_requested" not in history_cols:
        conn.execute(
            "ALTER TABLE history ADD COLUMN confirmation_requested INTEGER NOT NULL DEFAULT 0"
        )


def add_reminder(conn, target, text, next_run, interval_seconds, repeats, weekdays=None,
                 confirmation_required=False):
    cur = conn.execute(
        "INSERT INTO reminders"
        " (target, text, next_run, interval_seconds, repeats_left, created_at, weekdays,"
        " weekly_time, confirmation_required) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            target,
            text,
            next_run.strftime(DATETIME_FMT),
            interval_seconds,
            repeats,
            datetime.now().strftime(DATETIME_FMT),
            ",".join(map(str, weekdays)) if weekdays else None,
            next_run.strftime("%H:%M") if weekdays else None,
            int(confirmation_required),
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
    row = conn.execute(
        "SELECT MIN(t) AS m FROM ("
        " SELECT next_run AS t FROM reminders WHERE paused = 0"
        " UNION ALL SELECT next_retry AS t FROM pending_confirmations)"
    ).fetchone()
    if row["m"] is None:
        return None
    return datetime.strptime(row["m"], DATETIME_FMT)


def due_reminders(conn, now):
    return conn.execute(
        "SELECT * FROM reminders WHERE paused = 0 AND next_run <= ? ORDER BY next_run",
        (now.strftime(DATETIME_FMT),),
    ).fetchall()


def next_weekday(after, weekdays, time_text):
    """First selected local weekday strictly after `after`."""
    hour, minute = map(int, time_text.split(":"))
    selected = set(int(day) for day in weekdays)
    for offset in range(8):
        day = after.date() + timedelta(days=offset)
        candidate = datetime(day.year, day.month, day.day, hour, minute)
        if candidate.weekday() in selected and candidate > after:
            return candidate
    raise ValueError("Не выбраны дни недели")


def _days(row):
    return tuple(map(int, row["weekdays"].split(",")))


def advance(conn, row):
    """Списывает одно срабатывание: сдвигает next_run или удаляет напоминание."""
    repeats_left = row["repeats_left"]
    if repeats_left > 0:
        repeats_left -= 1

    if repeats_left == 0 or (row["interval_seconds"] is None and not row["weekdays"]):
        conn.execute("DELETE FROM reminders WHERE id = ?", (row["id"],))
    elif row["weekdays"]:
        next_run = next_weekday(datetime.now(), _days(row), row["weekly_time"])
        conn.execute(
            "UPDATE reminders SET next_run = ?, repeats_left = ?, scheduled_msg_id = NULL"
            " WHERE id = ?",
            (next_run.strftime(DATETIME_FMT), repeats_left, row["id"]),
        )
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
    if row["weekdays"]:
        candidate = datetime.strptime(row["next_run"], DATETIME_FMT)
        count = 0
        while candidate <= now and (row["repeats_left"] < 0 or count < row["repeats_left"]):
            count += 1
            candidate = next_weekday(candidate, _days(row), row["weekly_time"])
        remaining = row["repeats_left"] - count if row["repeats_left"] > 0 else -1
        if remaining == 0:
            delete_reminder(conn, row["id"])
        else:
            conn.execute(
                "UPDATE reminders SET next_run = ?, repeats_left = ? WHERE id = ?",
                (candidate.strftime(DATETIME_FMT), remaining, row["id"]),
            )
            conn.commit()
        return count
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


def replace_reminder(conn, reminder_id, target, text, next_run, interval_seconds, repeats,
                     weekdays=None, confirmation_required=False):
    conn.execute(
        "UPDATE reminders SET target = ?, text = ?, next_run = ?, interval_seconds = ?,"
        " repeats_left = ?, weekdays = ?, weekly_time = ?, confirmation_required = ?,"
        " paused = 0 WHERE id = ?",
        (target, text, next_run.strftime(DATETIME_FMT), interval_seconds, repeats,
         ",".join(map(str, weekdays)) if weekdays else None,
         next_run.strftime("%H:%M") if weekdays else None, int(confirmation_required),
         reminder_id),
    )
    conn.commit()


def set_paused(conn, reminder_id, paused, now=None):
    now = now or datetime.now()
    row = get_reminder(conn, reminder_id)
    if row is None:
        return False
    next_run = datetime.strptime(row["next_run"], DATETIME_FMT)
    if not paused and next_run <= now:
        if row["weekdays"]:
            next_run = next_weekday(now, _days(row), row["weekly_time"])
        elif row["interval_seconds"]:
            step = row["interval_seconds"]
            elapsed = int((now - next_run).total_seconds())
            next_run += timedelta(seconds=(elapsed // step + 1) * step)
        else:
            next_run = now + timedelta(minutes=1)
    conn.execute(
        "UPDATE reminders SET paused = ?, next_run = ? WHERE id = ?",
        (int(paused), next_run.strftime(DATETIME_FMT), reminder_id),
    )
    conn.commit()
    return True


def record_history(conn, reminder_id, target, text, scheduled_for, status,
                   detail=None, message_id=None, source_id=None, chat_id=None,
                   pending_id=None, confirmation_requested=False):
    scheduled = (scheduled_for.strftime(DATETIME_FMT) if isinstance(scheduled_for, datetime)
                 else scheduled_for)
    cur = conn.execute(
        "INSERT INTO history (reminder_id, target, text, scheduled_for, occurred_at,"
        " status, detail, message_id, source_id, chat_id, pending_id,"
        " confirmation_requested) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (reminder_id, target, text, scheduled, datetime.now().strftime(DATETIME_FMT),
         status, detail, message_id, source_id, chat_id, pending_id,
         int(confirmation_requested)),
    )
    conn.commit()
    return cur.lastrowid


def list_history(conn, limit=20):
    return conn.execute("SELECT * FROM history ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def get_sent_by_message(conn, message_id, chat_id=None):
    if chat_id is None:
        return conn.execute(
            "SELECT * FROM history WHERE message_id = ? AND status = 'sent'"
            " ORDER BY id DESC LIMIT 1", (message_id,),
        ).fetchone()
    return conn.execute(
        "SELECT * FROM history WHERE message_id = ? AND status = 'sent'"
        " AND (chat_id = ? OR (chat_id IS NULL AND target = 'me'))"
        " ORDER BY (chat_id = ?) DESC, id DESC LIMIT 1",
        (message_id, chat_id, chat_id),
    ).fetchone()


def get_last_sent(conn, reminder_id):
    return conn.execute(
        "SELECT * FROM history WHERE reminder_id = ? AND status = 'sent'"
        " ORDER BY id DESC LIMIT 1", (reminder_id,),
    ).fetchone()


def recent_sent(conn, chat_id=None, limit=12, hours=24):
    """Recent personal deliveries awaiting a completion mark."""
    since = (datetime.now() - timedelta(hours=hours)).strftime(DATETIME_FMT)
    chat_filter = " AND (h.chat_id = ? OR h.chat_id IS NULL)" if chat_id is not None else ""
    params = (since, chat_id, limit) if chat_id is not None else (since, limit)
    return conn.execute(
        "SELECT h.* FROM history h WHERE h.status = 'sent' AND h.target = 'me'"
        " AND h.occurred_at >= ?"
        " AND NOT EXISTS (SELECT 1 FROM history newer WHERE newer.status = 'sent'"
        " AND newer.target = 'me' AND newer.reminder_id = h.reminder_id"
        " AND newer.id > h.id)"
        " AND NOT EXISTS (SELECT 1 FROM history a WHERE a.source_id = h.id"
        " AND a.status = 'done')" + chat_filter + " ORDER BY h.id DESC LIMIT ?",
        params,
    ).fetchall()


def has_action(conn, source_id, status):
    return conn.execute(
        "SELECT 1 FROM history WHERE source_id = ? AND status = ? LIMIT 1",
        (source_id, status),
    ).fetchone() is not None


def set_confirmation(conn, reminder_id, required):
    conn.execute(
        "UPDATE reminders SET confirmation_required = ? WHERE id = ?",
        (int(required), reminder_id),
    )
    conn.commit()


def open_pending(conn, reminder_id, target, text, chat_id):
    now = datetime.now()
    next_retry = now + timedelta(seconds=CONFIRM_RETRY_SECONDS)
    # A new scheduled occurrence supersedes replies to the previous one.
    conn.execute("DELETE FROM pending_confirmations WHERE reminder_id = ?", (reminder_id,))
    conn.execute(
        "INSERT INTO pending_confirmations"
        " (reminder_id, target, text, chat_id, next_retry, retry_seconds, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (reminder_id, target, text, chat_id, next_retry.strftime(DATETIME_FMT),
         CONFIRM_RETRY_SECONDS, now.strftime(DATETIME_FMT)),
    )
    conn.commit()
    return conn.execute(
        "SELECT id FROM pending_confirmations WHERE reminder_id = ?", (reminder_id,)
    ).fetchone()["id"]


def get_pending(conn, pending_id):
    return conn.execute(
        "SELECT * FROM pending_confirmations WHERE id = ?", (pending_id,)
    ).fetchone()


def get_pending_by_reminder(conn, reminder_id):
    return conn.execute(
        "SELECT * FROM pending_confirmations WHERE reminder_id = ?", (reminder_id,)
    ).fetchone()


def list_pending(conn):
    return conn.execute(
        "SELECT * FROM pending_confirmations ORDER BY next_retry"
    ).fetchall()


def pending_for_chat(conn, chat_id):
    return conn.execute(
        "SELECT * FROM pending_confirmations WHERE chat_id = ? ORDER BY next_retry",
        (chat_id,),
    ).fetchall()


def due_pending(conn, now):
    return conn.execute(
        "SELECT * FROM pending_confirmations WHERE next_retry <= ? ORDER BY next_retry",
        (now.strftime(DATETIME_FMT),),
    ).fetchall()


def close_pending(conn, pending_id):
    conn.execute("DELETE FROM pending_confirmations WHERE id = ?", (pending_id,))
    conn.commit()


def close_pending_for_reminder(conn, reminder_id):
    conn.execute(
        "DELETE FROM pending_confirmations WHERE reminder_id = ?", (reminder_id,)
    )
    conn.commit()


def defer_pending(conn, pending_id, seconds):
    when = datetime.now() + timedelta(seconds=seconds)
    conn.execute(
        "UPDATE pending_confirmations SET next_retry = ? WHERE id = ?",
        (when.strftime(DATETIME_FMT), pending_id),
    )
    conn.commit()
    return when


def skip_pending_missed(conn, row, now):
    start = datetime.strptime(row["next_retry"], DATETIME_FMT)
    step = row["retry_seconds"]
    count = int((now - start).total_seconds()) // step + 1
    future = start + timedelta(seconds=count * step)
    conn.execute(
        "UPDATE pending_confirmations SET next_retry = ? WHERE id = ?",
        (future.strftime(DATETIME_FMT), row["id"]),
    )
    conn.commit()
    return count


def last_sent_for_pending(conn, pending_id):
    return conn.execute(
        "SELECT * FROM history WHERE pending_id = ? AND status = 'sent'"
        " ORDER BY id DESC LIMIT 1", (pending_id,),
    ).fetchone()
