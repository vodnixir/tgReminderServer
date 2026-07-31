"""Фоновый цикл: следит за расписанием напоминаний.

Дальние напоминания (до которых больше MIN_SCHEDULE секунд) ставятся в
родной серверный планировщик Telegram (schedule_date): их доставляет сам
Telegram — точно в срок и даже если этот скрипт в тот момент не работает,
а напоминания себе приходят с пуш-уведомлением («Напоминание» из
«Избранного»). Цикл лишь фиксирует срабатывание и ставит в расписание
следующее повторение.

Ближние напоминания и быстрые повторы отправляются напрямую.
"""
import asyncio
import logging
from datetime import datetime, timedelta

from telethon.errors import FloodWaitError

import db
import notifier

log = logging.getLogger(__name__)

MAX_SLEEP = 60  # сек; страховочный потолок сна, чтобы цикл жил всегда
MIN_SCHEDULE = 15  # сек; минимальный запас для серверного планировщика


async def scheduler_loop(client, conn, wake_event, send_delay, bot_token, me_id, guard):
    while True:
        try:
            await _process_due(client, conn, send_delay, bot_token, me_id, guard)
            await _ensure_scheduled(client, conn)
        except Exception:
            log.exception("Ошибка в цикле планировщика")
        await _sleep_until_next(conn, wake_event)


async def _sleep_until_next(conn, wake_event):
    nxt = db.next_run_time(conn)
    if nxt is None:
        timeout = MAX_SLEEP
    else:
        timeout = (nxt - datetime.now()).total_seconds()
        timeout = min(max(timeout, 0.05), MAX_SLEEP)
    try:
        await asyncio.wait_for(wake_event.wait(), timeout)
    except asyncio.TimeoutError:
        pass
    wake_event.clear()


async def _process_due(client, conn, send_delay, bot_token, me_id, guard):
    sent_direct = 0
    for row in db.due_reminders(conn, datetime.now()):
        if row["scheduled_msg_id"]:
            # Это срабатывание доставил сам Telegram — только фиксируем
            # и (для повторяющихся) освобождаем место под следующее.
            if row["target"] == "me":
                guard.register_text(row["text"])
            db.advance(conn, row)
            log.info("Напоминание #%d доставлено Telegram по расписанию", row["id"])
            continue

        if sent_direct:
            # Пауза между несколькими прямыми отправками подряд
            # (микро-рассылка), чтобы не выглядеть как спам.
            await asyncio.sleep(send_delay)
        try:
            await _send_direct(client, row, bot_token, me_id, guard)
            db.advance(conn, row)
            sent_direct += 1
            log.info("Отправлено напоминание #%d → %s", row["id"], row["target"])
        except FloodWaitError as e:
            log.warning("Лимит Telegram (FloodWait): пауза %d с", e.seconds)
            await asyncio.sleep(e.seconds + 5)
            return
        except Exception:
            log.exception(
                "Не удалось отправить #%d, попробую снова через 5 минут", row["id"]
            )
            db.postpone(conn, row, 5)


async def _send_direct(client, row, bot_token, me_id, guard):
    if row["target"] != "me":
        await client.send_message(row["target"], row["text"])
        return

    if bot_token:
        try:
            await notifier.send_via_bot(bot_token, me_id, row["text"])
            return
        except Exception:
            log.exception(
                "Бот не смог отправить (вы нажали Start в чате с ботом?), "
                "отправляю в «Избранное»"
            )

    msg = await client.send_message("me", row["text"])
    guard.register_id(msg.id)


async def _ensure_scheduled(client, conn):
    """Ставит в серверный планировщик Telegram всё, что туда успевает."""
    horizon = datetime.now() + timedelta(seconds=MIN_SCHEDULE)
    for row in db.needing_schedule(conn, horizon):
        # Наивное локальное время -> aware, иначе Telethon сочтёт его UTC.
        when = datetime.strptime(row["next_run"], db.DATETIME_FMT).astimezone()
        try:
            msg = await client.send_message(row["target"], row["text"], schedule=when)
            if msg:
                db.set_scheduled_msg_id(conn, row["id"], msg.id)
                log.info(
                    "Напоминание #%d поставлено в планировщик Telegram на %s",
                    row["id"],
                    row["next_run"],
                )
        except FloodWaitError as e:
            log.warning("Лимит Telegram при планировании: пауза %d с", e.seconds)
            await asyncio.sleep(e.seconds + 5)
            return
        except Exception:
            # Не страшно: когда придёт срок, отправим напрямую.
            log.exception(
                "Не удалось запланировать #%d, отправлю напрямую в срок", row["id"]
            )
