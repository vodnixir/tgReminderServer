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

список — активные напоминания
удали 3 — удалить напоминание №3
помощь — эта подсказка"""


@dataclass(frozen=True)
class ReminderSpec:
    target: str
    when: datetime
    text: str
    interval: int | None = None
    repeats: int = 1


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

    when, consumed = _when_prefix(text, now)
    if when <= now:
        raise ValueError("Время уже прошло. Укажите будущее время.")
    rest = text[consumed:].lstrip()
    interval, repeats = None, 1
    every = EVERY.match(rest)
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
    return ReminderSpec(target, when, rest, interval, repeats)


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
    )
    who = "себе" if spec.target == "me" else spec.target
    schedule = f"{spec.when:%d.%m.%Y %H:%M:%S}"
    if spec.interval:
        count = "без ограничения" if spec.repeats < 0 else f"{spec.repeats} раз"
        schedule += f", каждые {format_interval(spec.interval)}, {count}"
    return f"✅ Напоминание #{rid} {who}: {schedule}."


async def handle(raw_text, client, conn):
    text = raw_text.strip()
    match = re.match(r"^/?\s*(напомни|напиши|список|list|удали|помощь|help)\b", text, re.I)
    if not match:
        return None
    command = match.group(1).lower()
    rest = text[match.end():].strip()
    if command in ("помощь", "help"):
        return HELP
    if command in ("список", "list"):
        return format_list(db.list_reminders(conn))
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
    try:
        spec = parse_reminder(rest)
    except ValueError as exc:
        return f"{exc} Напишите «помощь» для примеров."
    return await _add(spec, client, conn)
