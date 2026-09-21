"""Human-friendly commands written in the private reminders topic."""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from telethon.tl.functions.messages import DeleteScheduledMessagesRequest
from telethon.tl.types import User

import db


UNITS = {
    "секунда": 1, "секунду": 1, "секунды": 1, "секунд": 1,
    "сек": 1, "с": 1, "s": 1, "c": 1,
    "минута": 60, "минуту": 60, "минуты": 60, "минут": 60,
    "мин": 60, "м": 60, "m": 60,
    "часов": 3600, "часа": 3600, "час": 3600, "ч": 3600, "h": 3600,
    "дней": 86400, "дня": 86400, "день": 86400, "д": 86400, "d": 86400,
}
UNIT_NAMES = "|".join(re.escape(s) for s in sorted(UNITS, key=len, reverse=True))
PART = re.compile(rf"(\d+)\s*({UNIT_NAMES})\.?(?=$|[^a-zа-яё])", re.I)
BARE_UNIT = re.compile(rf"({UNIT_NAMES})(?=$|[^a-zа-яё])", re.I)
CLOCK = r"(?P<hour>\d{1,2})\s*:\s*(?P<minute>\d{2})"
DATE_TIME = re.compile(
    rf"(?P<day>\d{{1,2}})\s*\.\s*(?P<month>\d{{1,2}})"
    rf"(?:\s*\.\s*(?P<year>\d{{4}}))?\s+(?:в\s+)?{CLOCK}(?!\d)", re.I,
)
TIME_ONLY = re.compile(rf"(?:в\s+)?{CLOCK}(?!\d)", re.I)
TARGET = re.compile(r"(?i)^(я|мне|себе|me|@[a-z0-9_]+)(?=\s|$)")
EVERY = re.compile(r"(?i)^(каждые|каждый|каждую)(?=\s|$)")
WEEKDAYS = {
    "понедельник": 0, "вторник": 1, "среду": 2, "среда": 2,
    "четверг": 3, "пятницу": 4, "пятница": 4,
    "субботу": 5, "суббота": 5, "воскресенье": 6,
}
WEEKLY = re.compile(
    r"(?i)^(по\s+будням|по\s+выходным|каждый\s+"
    r"(?:понедельник|вторник|среду|среда|четверг|пятницу|пятница|субботу|суббота|воскресенье))\s+"
)

HELP = """Пишите команды в этой теме. / в начале необязателен.

Напомни [я или @username] <когда> [каждые <интервал> [N раз]] <текст>
Когда: через 10 мин; 09:00; завтра в 9:00; 07.07 в 12:00.
Интервал: 30с, 2 часа, 1 день 3 часа, каждый день.
Текст можно отделить пробелом, двоеточием или /.

Примеры:
напомни через 10 минут вынести мусор
/напиши я завтра в 9:00: зарядка
напомни @ivan через 2 часа / Пришли документы
напомни мне в 8:00 каждый день проверить почту
напомни @ivan через 1ч каждые 2 часа 5 раз: встреча
напомни по будням в 09:00 проверить почту

список — активные напоминания
история — отправленные и пропущенные напоминания
готово — ответьте на напоминание, чтобы отметить выполнение
отложи на 10 минут — ответьте на напоминание, чтобы повторить позже
измени 3 на завтра в 09:00 — поменять время или текст
пауза 3 / возобнови 3 — временно выключить и включить
удали 3 — удалить напоминание №3
помощь — эта подсказка

Если задан GROQ_API_KEY, можно писать свободнее: «перенеси третье на пятницу в 18»."""


@dataclass(frozen=True)
class ReminderSpec:
    target: str
    when: datetime
    text: str
    interval: int | None = None
    repeats: int = 1
    weekdays: tuple[int, ...] | None = None


def _duration_prefix(text):
    total = 0
    pos = 0
    found = False
    while True:
        pos += len(re.match(r"\s*", text[pos:]).group())
        match = PART.match(text, pos)
        if not match:
            break
        total += int(match.group(1)) * UNITS[match.group(2).lower()]
        pos = match.end()
        found = True
    return (total, pos) if found else (None, 0)


def parse_duration(text):
    stripped = text.strip()
    total, pos = _duration_prefix(stripped)
    return total if total is not None and pos == len(stripped) else None


def _clock(match, now, day_offset=None):
    hour, minute = int(match.group("hour")), int(match.group("minute"))
    if hour > 23 or minute > 59:
        raise ValueError("Проверьте время: часы 0–23, минуты 0–59.")
    base = now + timedelta(days=day_offset or 0)
    when = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if day_offset is None and when <= now:
        when += timedelta(days=1)
    return when


def _when_prefix(text, now):
    text = text.lstrip()
    relative = re.match(r"(?i)^через\s+", text)
    if relative:
        duration, consumed = _duration_prefix(text[relative.end():])
        if duration is None or duration <= 0:
            raise ValueError("После «через» укажите срок, например: через 10 минут.")
        return now + timedelta(seconds=duration), relative.end() + consumed

    day_word = re.match(r"(?i)^(сегодня|завтра)\s+", text)
    if day_word:
        match = TIME_ONLY.match(text, day_word.end())
        if not match:
            raise ValueError("После «сегодня» или «завтра» укажите время: 09:00.")
        return _clock(match, now, 0 if day_word.group(1).lower() == "сегодня" else 1), match.end()

    match = DATE_TIME.match(text)
    if match:
        day, month = int(match.group("day")), int(match.group("month"))
        hour, minute = int(match.group("hour")), int(match.group("minute"))
        years = [int(match.group("year"))] if match.group("year") else range(now.year, now.year + 9)
        for year in years:
            try:
                when = datetime(year, month, day, hour, minute)
            except ValueError:
                continue
            if when > now:
                return when, match.end()
        raise ValueError("Дата уже прошла или не существует. Укажите будущую дату.")

    match = TIME_ONLY.match(text)
    if match:
        return _clock(match, now), match.end()
    raise ValueError("Не понял время. Примеры: через 10 мин, завтра в 9:00, 07.07 12:00.")


def parse_reminder(text, now=None):
    now = now or datetime.now()
    text = text.strip()
    target = "me"
    match = TARGET.match(text)
    if match:
        raw = match.group(1)
        target = "me" if raw.casefold() in ("я", "мне", "себе", "me") else raw
        text = text[match.end():].lstrip()

    weekly = WEEKLY.match(text)
    weekdays = None
    if weekly:
        label = re.sub(r"\s+", " ", weekly.group(1).casefold())
        if label == "по будням":
            weekdays = (0, 1, 2, 3, 4)
        elif label == "по выходным":
            weekdays = (5, 6)
        else:
            weekdays = (WEEKDAYS[label.split()[-1]],)
        match = TIME_ONLY.match(text, weekly.end())
        if not match:
            raise ValueError("После дней недели укажите время, например: в 09:00.")
        hour, minute = int(match.group("hour")), int(match.group("minute"))
        if hour > 23 or minute > 59:
            raise ValueError("Проверьте время: часы 0–23, минуты 0–59.")
        when = db.next_weekday(now, weekdays, f"{hour:02d}:{minute:02d}")
        consumed = match.end()
    else:
        when, consumed = _when_prefix(text, now)
    if when <= now:
        raise ValueError("Время уже прошло. Укажите будущее время.")
    rest = text[consumed:].lstrip()
    interval, repeats = None, 1
    every = EVERY.match(rest)
    if every and weekdays:
        raise ValueError("Нельзя одновременно указать дни недели и интервал.")
    if every:
        rest = rest[every.end():].lstrip()
        interval, pos = _duration_prefix(rest)
        if interval is None:
            unit = BARE_UNIT.match(rest)
            if unit:
                interval, pos = UNITS[unit.group(1).lower()], unit.end()
        if not interval:
            raise ValueError("После «каждые» укажите интервал: 2 часа или день.")
        rest = rest[pos:].lstrip()
        repeats = -1
        count = re.match(r"^(\d+)\s*(раз|раза)?(?=\s|$|[/:-])", rest, re.I)
        if count and (count.group(2) or re.match(r"^\d+\s*[/:-]", rest)):
            repeats = int(count.group(1))
            if repeats == 0:
                raise ValueError("Число повторений должно быть больше нуля.")
            rest = rest[count.end():].lstrip()

    rest = re.sub(r"^[/\u2014:\-]\s*", "", rest).strip()
    if not rest:
        raise ValueError("Добавьте текст напоминания после времени.")
    if weekdays:
        repeats = -1
    return ReminderSpec(target, when, rest, interval, repeats, weekdays)


def format_interval(seconds):
    parts = []
    for unit, size in (("д", 86400), ("ч", 3600), ("м", 60), ("с", 1)):
        if seconds >= size:
            parts.append(f"{seconds // size}{unit}")
            seconds %= size
    return "".join(parts) or "0с"


def format_list(rows):
    if not rows:
        return "Активных напоминаний нет. Напишите «помощь» для примеров."
    lines = ["Активные напоминания:"]
    for row in rows:
        who = "себе" if row["target"] == "me" else row["target"]
        extra = ""
        if row["interval_seconds"]:
            count = "∞" if row["repeats_left"] < 0 else str(row["repeats_left"])
            extra = f", каждые {format_interval(row['interval_seconds'])}, осталось {count}"
        if row["weekdays"]:
            names = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
            days = ", ".join(names[int(day)] for day in row["weekdays"].split(","))
            extra = f", {days}, осталось {'∞' if row['repeats_left'] < 0 else row['repeats_left']}"
        if row["paused"]:
            extra += ", пауза"
        body = row["text"] if len(row["text"]) <= 40 else row["text"][:40] + "…"
        lines.append(f"#{row['id']} → {who}, {row['next_run'][:16]}{extra}: {body}")
    return "\n".join(lines)


async def _add(spec, client, conn):
    if len(spec.text) > 3800:
        return "Текст напоминания слишком длинный. Сократите его до 3800 символов."
    if spec.target != "me":
        try:
            recipient = await client.get_entity(spec.target)
        except Exception:
            return f"Не нашёл получателя «{spec.target}». Укажите его @username и проверьте связь."
        if not isinstance(recipient, User):
            return "Получатель должен быть человеком. Укажите его @username."
    rid = db.add_reminder(
        conn, spec.target, spec.text, spec.when, spec.interval, spec.repeats,
        weekdays=spec.weekdays,
    )
    who = "себе" if spec.target == "me" else spec.target
    schedule = f"{spec.when:%d.%m.%Y %H:%M:%S}"
    if spec.interval:
        count = "без ограничения" if spec.repeats < 0 else f"{spec.repeats} раз"
        schedule += f", каждые {format_interval(spec.interval)}, {count}"
    if spec.weekdays:
        schedule += ", по дням недели"
    return f"✅ Напоминание #{rid} {who}: {schedule}."


def format_history(rows):
    if not rows:
        return "История пока пуста."
    labels = {
        "sent": "отправлено", "missed": "пропущено", "failed": "ошибка",
        "done": "готово", "snoozed": "отложено", "edited": "изменено",
        "paused": "пауза", "resumed": "возобновлено",
    }
    lines = ["История (сначала новые):"]
    for row in rows:
        who = "себе" if row["target"] == "me" else row["target"]
        body = row["text"][:75] + ("…" if len(row["text"]) > 75 else "")
        lines.append(
            f"{row['occurred_at'][:16]} · #{row['reminder_id']} → {who} · "
            f"{labels.get(row['status'], row['status'])} "
            f"(срок {row['scheduled_for'][:16]}): {body}"
        )
    return "\n".join(lines)


def _sent_context(conn, reply_to_msg_id, reminder_id=None):
    if reply_to_msg_id:
        sent = db.get_sent_by_message(conn, reply_to_msg_id)
        if sent is not None:
            return sent
    if reminder_id:
        return db.get_last_sent(conn, reminder_id)
    return None


def _number(text):
    match = re.fullmatch(r"#?(\d+)", text.strip())
    return int(match.group(1)) if match else None


def _done(conn, sent):
    if sent is None:
        return "Ответьте на конкретное напоминание или укажите его номер: «готово 3»."
    if db.has_action(conn, sent["id"], "done"):
        return "Это напоминание уже отмечено выполненным."
    db.record_history(conn, sent["reminder_id"], sent["target"], sent["text"],
                      sent["scheduled_for"], "done", source_id=sent["id"])
    return f"✅ Отмечено выполнение #{sent['reminder_id']}."


def _snooze(conn, sent, seconds):
    if sent is None:
        return "Ответьте на напоминание или укажите номер: «отложи 3 на 10 минут»."
    if sent["target"] != "me":
        return "Отложить ответом можно только личное напоминание."
    if not isinstance(seconds, int) or not 0 < seconds <= 365 * 86400:
        return "Укажите срок от 1 секунды до 365 дней."
    if db.has_action(conn, sent["id"], "snoozed"):
        return "Это напоминание уже отложено."
    when = datetime.now() + timedelta(seconds=seconds)
    rid = db.add_reminder(conn, "me", sent["text"], when, None, 1)
    db.record_history(conn, sent["reminder_id"], "me", sent["text"],
                      sent["scheduled_for"], "snoozed", detail=f"новое #{rid}",
                      source_id=sent["id"])
    return f"⏰ Напоминание #{sent['reminder_id']} отложено до {when:%d.%m %H:%M}; новое #{rid}."


def _snooze_command(conn, rest, reply_to_msg_id):
    match = re.match(r"^#?(\d+)\b", rest)
    rid = int(match.group(1)) if match else None
    duration_text = rest[match.end():].strip() if match else rest
    duration_text = re.sub(r"^(?:на|через)\s+", "", duration_text, flags=re.I)
    seconds = parse_duration(duration_text)
    if not seconds:
        return "Укажите срок: «отложи на 10 минут» ответом на напоминание."
    return _snooze(conn, _sent_context(conn, reply_to_msg_id, rid), seconds)


async def _edit(conn, client, reminder_id, spec):
    row = db.get_reminder(conn, reminder_id)
    if row is None:
        return "Напоминание с таким номером не найдено. Проверьте «список»."
    if row["scheduled_msg_id"]:
        return "Сначала снимите старую запланированную отправку Telegram."
    if len(spec.text) > 3800:
        return "Текст напоминания слишком длинный."
    if spec.target != "me":
        try:
            recipient = await client.get_entity(spec.target)
        except Exception:
            return f"Не нашёл получателя «{spec.target}»."
        if not isinstance(recipient, User):
            return "Получатель должен быть человеком."
    db.replace_reminder(conn, reminder_id, spec.target, spec.text, spec.when,
                        spec.interval, spec.repeats, spec.weekdays)
    db.record_history(conn, reminder_id, spec.target, spec.text, spec.when, "edited")
    return f"✅ Напоминание #{reminder_id} изменено: {spec.when:%d.%m.%Y %H:%M}."


def _parse_edit(rest, row):
    rest = re.sub(r"^(?:на|в)\s+", "", rest.strip(), flags=re.I)
    # Если указан только новый срок, оставляем прежний текст и повторение.
    try:
        spec = parse_reminder(rest)
        return ReminderSpec(row["target"], spec.when, spec.text,
                            spec.interval or row["interval_seconds"],
                            spec.repeats if spec.interval or spec.weekdays else row["repeats_left"],
                            spec.weekdays or (tuple(map(int, row["weekdays"].split(",")))
                                              if row["weekdays"] else None))
    except ValueError as exc:
        if "Добавьте текст" not in str(exc):
            raise
        spec = parse_reminder(rest + " : " + row["text"])
        return ReminderSpec(row["target"], spec.when, row["text"],
                            spec.interval or row["interval_seconds"], row["repeats_left"],
                            spec.weekdays or (tuple(map(int, row["weekdays"].split(",")))
                                              if row["weekdays"] else None))


def correct_ai_when(raw_text, when, weekdays, now):
    """Calculate explicit calendar phrases locally instead of trusting model arithmetic."""
    text = raw_text.casefold()
    relative = re.search(r"\bчерез\s+", text)
    if relative:
        seconds, _ = _duration_prefix(text[relative.end():])
        if seconds and seconds > 0:
            return now + timedelta(seconds=seconds)
    day_word = re.search(r"\b(сегодня|завтра|послезавтра)\b", text)
    if day_word:
        offset = {"сегодня": 0, "завтра": 1, "послезавтра": 2}[day_word.group(1)]
        date = (now + timedelta(days=offset)).date()
        return datetime(date.year, date.month, date.day, when.hour, when.minute)
    if weekdays:
        return db.next_weekday(now, weekdays, when.strftime("%H:%M"))
    for label, day in WEEKDAYS.items():
        if re.search(rf"\b{re.escape(label)}\b", text):
            return db.next_weekday(now, (day,), when.strftime("%H:%M"))
    return when


async def _apply_ai(action, client, conn, reply_to_msg_id, raw_text):
    kind = action["action"]
    rid = action.get("reminder_id")
    sent = _sent_context(conn, reply_to_msg_id, rid)
    if kind == "none":
        return action.get("reason") or "Не понял запрос. Напишите «помощь» для примеров."
    if kind == "history":
        return format_history(db.list_history(conn, min(action.get("limit") or 20, 50)))
    if kind == "complete":
        return _done(conn, sent)
    if kind == "snooze":
        return _snooze(conn, sent, action.get("duration_seconds"))
    if kind in ("pause", "resume"):
        row = db.get_reminder(conn, rid) if rid else None
        if row is None:
            return "Укажите номер существующего напоминания."
        if row["scheduled_msg_id"]:
            return "Сначала снимите старую запланированную отправку Telegram."
        db.set_paused(conn, rid, kind == "pause")
        db.record_history(conn, rid, row["target"], row["text"], row["next_run"],
                          "paused" if kind == "pause" else "resumed")
        return f"Напоминание #{rid}: {'пауза' if kind == 'pause' else 'возобновлено'}."
    if kind not in ("create", "edit"):
        return "Не понял действие. Напишите «помощь» для примеров."
    row = db.get_reminder(conn, rid) if kind == "edit" and rid else None
    if kind == "edit" and row is None:
        return "Укажите номер существующего напоминания."
    when_text = action.get("when")
    when = datetime.fromisoformat(when_text) if when_text else (
        datetime.strptime(row["next_run"], db.DATETIME_FMT) if row else None)
    if when is None or when.tzinfo is not None:
        return "Укажите будущее время для напоминания."
    weekdays = action.get("weekdays") or (tuple(map(int, row["weekdays"].split(",")))
                                          if row and row["weekdays"] else None)
    target = action.get("target") or (row["target"] if row else "me")
    if target != "me" and (row is None or target != row["target"]) and target.casefold() not in raw_text.casefold():
        return "Укажите адресата явно через @username."
    if row and target != row["target"] and target == "me" and not re.search(
        r"(?i)\b(?:мне|себе|для меня)\b", raw_text
    ):
        target = row["target"]
    body = action.get("text") or (row["text"] if row else None)
    interval = action.get("interval_seconds") or (row["interval_seconds"] if row else None)
    repeats = action.get("repeats") or (row["repeats_left"] if row else (-1 if weekdays or interval else 1))
    if not body or len(body) > 3800 or (weekdays and interval):
        return "Проверьте текст и расписание напоминания."
    if when_text or action.get("weekdays"):
        when = correct_ai_when(raw_text, when, weekdays, datetime.now())
    if when <= datetime.now():
        return "Укажите будущее время для напоминания."
    spec = ReminderSpec(target, when, body, interval, repeats, tuple(weekdays) if weekdays else None)
    return await (_add(spec, client, conn) if kind == "create" else _edit(conn, client, rid, spec))


async def handle(raw_text, client, conn, reply_to_msg_id=None, interpreter=None):
    text = raw_text.strip()
    if not text or text.startswith("\u2063"):
        return None
    match = re.match(
        r"^/?\s*(напомни|напиши|список|list|история|удали|помощь|help|"
        r"готово|сделано|отложи|измени|пауза|возобнови)\b", text, re.I,
    )
    command = match.group(1).lower() if match else None
    rest = text[match.end():].strip() if match else text
    if command in ("помощь", "help"):
        return HELP
    if command in ("список", "list"):
        return format_list(db.list_reminders(conn))
    if command == "история":
        limit = _number(rest) if rest else 20
        return format_history(db.list_history(conn, min(max(limit or 20, 1), 50)))
    if command in ("готово", "сделано"):
        return _done(conn, _sent_context(conn, reply_to_msg_id, _number(rest)))
    if command == "отложи":
        result = _snooze_command(conn, rest, reply_to_msg_id)
        if interpreter is None or not result.startswith("Укажите срок"):
            return result
    if command in ("пауза", "возобнови"):
        rid = _number(rest)
        row = db.get_reminder(conn, rid) if rid else None
        if row is None:
            if interpreter is None or rid is not None:
                return "Укажите номер существующего напоминания из «список»."
        else:
            if row["scheduled_msg_id"]:
                return "Сначала снимите старую запланированную отправку Telegram."
            db.set_paused(conn, rid, command == "пауза")
            db.record_history(conn, rid, row["target"], row["text"], row["next_run"],
                              "paused" if command == "пауза" else "resumed")
            return f"Напоминание #{rid}: {'пауза' if command == 'пауза' else 'возобновлено'}."
    if command == "измени":
        edit_match = re.match(r"^#?(\d+)\s+(.+)$", rest)
        if not edit_match:
            if interpreter is None:
                return "Напишите: «измени 3 на завтра в 09:00»."
        else:
            rid = int(edit_match.group(1))
            row = db.get_reminder(conn, rid)
            if row is None:
                return "Напоминание с таким номером не найдено."
            try:
                spec = _parse_edit(edit_match.group(2), row)
            except ValueError as exc:
                if interpreter is None:
                    return str(exc)
            else:
                return await _edit(conn, client, rid, spec)
    if command == "удали":
        if not re.fullmatch(r"#?\d+", rest):
            return "Укажите номер: «удали 3». Номера есть в команде «список»."
        row = db.get_reminder(conn, int(rest.lstrip("#")))
        if not row:
            return "Напоминание с таким номером не найдено. Проверьте «список»."
        if row["scheduled_msg_id"]:
            try:
                peer = await client.get_input_entity(row["target"])
                await client(DeleteScheduledMessagesRequest(peer, id=[row["scheduled_msg_id"]]))
            except Exception:
                return "Не удалось снять старую запланированную отправку в Telegram. Повторите удаление позже."
        db.delete_reminder(conn, row["id"])
        return f"Напоминание #{row['id']} удалено."
    if command in ("напомни", "напиши"):
        try:
            spec = parse_reminder(rest)
        except ValueError as exc:
            if interpreter is None:
                return f"{exc} Напишите «помощь» для примеров."
        else:
            return await _add(spec, client, conn)
    if interpreter is None:
        return None
    try:
        reply = _sent_context(conn, reply_to_msg_id)
        action = await interpreter.interpret(
            text, datetime.now(), db.list_reminders(conn), reply)
        return await _apply_ai(action, client, conn, reply_to_msg_id, text)
    except Exception:
        return "Не удалось разобрать запрос через Groq. Повторите позже или напишите «помощь»."
