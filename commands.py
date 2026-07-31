"""Разбор и выполнение команд, которые вы пишете себе в «Избранное»."""
import logging
import re
from datetime import datetime, timedelta

from telethon.tl.functions.messages import DeleteScheduledMessagesRequest

import db

log = logging.getLogger(__name__)

UNIT_SECONDS = {
    "с": 1, "м": 60, "ч": 3600, "д": 86400,
    # Латинские единицы: и двойники-ловушки («c» неотличима от «с»
    # на глаз), и просто английская раскладка.
    "s": 1, "c": 1, "m": 60, "h": 3600, "d": 86400,
}

# Составная длительность: «30с», «1ч30м», «1д12ч30м15с»
_UNIT_CHARS = "".join(UNIT_SECONDS)
DURATION = rf"(?:\d+\s*[{_UNIT_CHARS}])+"

HELP = """Команды (пишите их себе в «Избранное»):

/напиши <кому> <когда> [каждые <интервал> [<повторений>]] / <текст>
  кому:     я — себе в «Избранное», @username — контакту
  когда:    09:00 · 07.07 09:00 · 07.07.2026 09:00 · через 1ч30м
  интервал: 30с, 45м, 2ч, 1д и сочетания: 1ч30м, 1д12ч
  повтор:   каждые 1д — бессрочно, каждые 2ч 5 — всего 5 раз

Примеры:
  /напиши я через 30м / Выключить духовку
  /напиши я 09:00 каждые 1д / Зарядка!
  /напиши @ivan 07.07 12:00 каждые 1ч30м 3 / Пришли, пожалуйста, документы

/список — показать активные напоминания
/удали 3 — удалить напоминание #3
/помощь — эта справка"""

REMIND_RE = re.compile(
    r"^/напиши\s+(?P<target>\S+)\s+(?P<when>.+?)"
    rf"(?:\s+каждые\s+(?P<every>{DURATION})(?:\s+(?P<times>\d+))?)?"
    r"\s*/\s*(?P<text>.+)$",
    re.IGNORECASE | re.DOTALL,
)


def normalize_target(raw):
    if raw.lower() in ("я", "мне", "себе", "me"):
        return "me"
    return raw


def parse_duration(s):
    """«1ч30м» -> секунды."""
    total = 0
    for num, unit in re.findall(rf"(\d+)\s*([{_UNIT_CHARS}])", s.lower()):
        total += int(num) * UNIT_SECONDS[unit]
    return total


def parse_when(s, now):
    """Возвращает datetime или None, если формат не распознан."""
    s = s.strip().lower()
    try:
        m = re.fullmatch(rf"через\s+({DURATION})", s)
        if m:
            return now + timedelta(seconds=parse_duration(m.group(1)))

        m = re.fullmatch(r"(\d{1,2}):(\d{2})", s)
        if m:
            run = now.replace(
                hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0
            )
            if run <= now:
                run += timedelta(days=1)
            return run

        m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?\s+(\d{1,2}):(\d{2})", s)
        if m:
            day, month, year = int(m.group(1)), int(m.group(2)), m.group(3)
            run = datetime(
                int(year) if year else now.year,
                month,
                day,
                int(m.group(4)),
                int(m.group(5)),
            )
            if year is None and run <= now:
                run = run.replace(year=now.year + 1)
            return run
    except ValueError:
        return None
    return None


def format_interval(seconds):
    parts = []
    for unit, size in (("д", 86400), ("ч", 3600), ("м", 60), ("с", 1)):
        if seconds >= size:
            parts.append(f"{seconds // size}{unit}")
            seconds %= size
    return "".join(parts) or "0с"


def format_list(rows):
    if not rows:
        return "Активных напоминаний нет."
    lines = ["Активные напоминания:"]
    for r in rows:
        when = r["next_run"][:16]
        if r["interval_seconds"]:
            times = "∞" if r["repeats_left"] < 0 else str(r["repeats_left"])
            extra = f", каждые {format_interval(r['interval_seconds'])}, осталось {times}"
        else:
            extra = ""
        text = r["text"] if len(r["text"]) <= 40 else r["text"][:40] + "…"
        lines.append(f"#{r['id']} → {r['target']}, {when}{extra}: {text}")
    return "\n".join(lines)


async def _add_reminder(m, client, conn):
    target = normalize_target(m.group("target"))
    when = parse_when(m.group("when"), datetime.now())
    if when is None:
        return "Не понял время. Примеры: 09:00 · 07.07 09:00 · через 1ч30м"

    interval = None
    repeats = 1
    if m.group("every"):
        interval = parse_duration(m.group("every"))
        if interval == 0:
            return "Интервал повторения не может быть нулевым."
        repeats = int(m.group("times")) if m.group("times") else -1
        if repeats == 0:
            return "Число повторений не может быть нулевым."

    if target != "me":
        try:
            await client.get_entity(target)
        except Exception:
            return (
                f"Не нашёл получателя «{target}». "
                "Укажите @username человека, с которым у вас есть переписка."
            )

    rid = db.add_reminder(conn, target, m.group("text").strip(), when, interval, repeats)

    if interval:
        times = "бессрочно" if repeats == -1 else f"{repeats} раз(а)"
        schedule = f"с {when:%d.%m %H:%M:%S}, каждые {format_interval(interval)}, {times}"
    else:
        schedule = f"{when:%d.%m %H:%M:%S}"
    who = "себе" if target == "me" else target
    return f"✅ Напоминание #{rid} {who}: {schedule}"


async def _cancel_scheduled(client, row):
    """Снимает сообщение с серверного планировщика Telegram."""
    try:
        peer = await client.get_input_entity(row["target"])
        await client(
            DeleteScheduledMessagesRequest(peer, id=[row["scheduled_msg_id"]])
        )
    except Exception:
        # Скорее всего уже доставлено — удалению из базы это не мешает.
        log.warning(
            "Не удалось снять #%d с планировщика Telegram", row["id"], exc_info=True
        )


async def handle(event, client, conn):
    """Возвращает текст ответа или None, если реагировать не нужно."""
    text = event.raw_text.strip()
    low = text.lower()

    if low in ("/помощь", "/help"):
        return HELP

    if low in ("/список", "/list"):
        return format_list(db.list_reminders(conn))

    if low.startswith("/удали"):
        m = re.fullmatch(r"/удали\s+(\d+)", low)
        if not m:
            return "Формат: /удали <номер> (номер смотрите в /список)"
        row = db.get_reminder(conn, int(m.group(1)))
        if not row:
            return "Нет напоминания с таким номером."
        if row["scheduled_msg_id"]:
            await _cancel_scheduled(client, row)
        db.delete_reminder(conn, row["id"])
        return "Удалено."

    if low.startswith("/напиши"):
        m = REMIND_RE.fullmatch(text)
        if not m:
            return "Не понял формат. Наберите /помощь — там есть примеры."
        return await _add_reminder(m, client, conn)

    # Прочие сообщения с «/» — не наши команды, молчим.
    return None
