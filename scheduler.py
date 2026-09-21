"""Send reminders at their due time and report missed occurrences."""

import asyncio
import logging
from datetime import datetime, timedelta

from telethon.errors import FloodWaitError
from telethon.tl.functions.messages import DeleteScheduledMessagesRequest

import db


log = logging.getLogger(__name__)
MAX_SLEEP = 30
MISSED_AFTER = timedelta(minutes=1)
RETRY_DELAY = 15


async def cancel_legacy_scheduled(client, conn):
    """Remove messages scheduled by older releases before direct delivery."""
    for row in db.legacy_scheduled(conn):
        try:
            peer = await client.get_input_entity(row["target"])
            await client(DeleteScheduledMessagesRequest(
                peer, id=[row["scheduled_msg_id"]],
            ))
            db.clear_scheduled_msg_id(conn, row["id"])
            log.info("Старое запланированное сообщение #%d снято", row["id"])
        except Exception as exc:
            log.exception("Не удалось снять старое запланированное сообщение #%d", row["id"])
            db.add_notice(
                conn,
                f"Не удалось снять старое запланированное сообщение #{row['id']} "
                f"({type(exc).__name__}). Проверьте его в Telegram.",
            )


async def scheduler_loop(client, conn, wake_event, send_delay, control):
    outage_started = None
    last_problem_notice = None
    last_health_check = None
    while True:
        try:
            now = datetime.now()
            connected = getattr(client, "is_connected", lambda: True)()
            if not connected or last_health_check is None or now - last_health_check >= timedelta(seconds=30):
                me = await asyncio.wait_for(client.get_me(), timeout=10)
                if me is None:
                    raise RuntimeError("Сессия Telegram больше не авторизована")
                last_health_check = datetime.now()
        except (OSError, ConnectionError, asyncio.TimeoutError) as exc:
            last_health_check = None
            if outage_started is None:
                outage_started = datetime.now()
                log.warning("Связь с Telegram потеряна: %s", type(exc).__name__)
            await asyncio.sleep(RETRY_DELAY)
            continue

        if outage_started is not None:
            since = outage_started.strftime("%d.%m %H:%M:%S")
            db.add_notice(conn, f"Связь с Telegram восстановлена. Перерыв начался {since}.")
            outage_started = None

        try:
            await _process_due(client, conn, send_delay, control)
            await _process_pending(client, conn, send_delay, control)
            await _send_notices(conn, control)
            last_problem_notice = None
        except (OSError, ConnectionError, asyncio.TimeoutError):
            last_health_check = None
            if outage_started is None:
                outage_started = datetime.now()
                log.warning("Связь с Telegram потеряна при отправке")
            await asyncio.sleep(RETRY_DELAY)
            continue
        except Exception as exc:
            log.exception("Ошибка в цикле планировщика")
            now = datetime.now()
            if last_problem_notice is None or now - last_problem_notice > timedelta(minutes=5):
                try:
                    db.add_notice(conn, f"Ошибка планировщика: {type(exc).__name__}. Повторю попытку.")
                    last_problem_notice = now
                except Exception:
                    log.exception("Не удалось сохранить уведомление о сбое")
        await _sleep_until_next(conn, wake_event)


async def _send_notices(conn, control):
    for notice in db.pending_notices(conn):
        await control.send("⚠️ " + notice["text"])
        db.delete_notice(conn, notice["id"])


def _delivery_text(row, followup=False):
    header = f"⏰ {'Повторное напоминание' if followup else 'Напоминание'} #{row['reminder_id'] if followup else row['id']}\n"
    body = row["text"]
    if followup or row["confirmation_required"]:
        body += ("\n\nСделали? Ответьте «готово» или «не сделал». "
                 "Можно написать «напомни через 3 мин».")
    return header + body


async def _process_due(client, conn, send_delay, control):
    checked_at = datetime.now()
    sent = 0
    for row in db.due_reminders(conn, checked_at):
        due_at = datetime.strptime(row["next_run"], db.DATETIME_FMT)
        if row["scheduled_msg_id"]:
            # Старое сообщение осталось в серверном планировщике после
            # неудачного снятия. Повторно напрямую его не отправляем.
            db.advance(conn, row)
            continue
        if checked_at - due_at > MISSED_AFTER:
            count = db.skip_missed(conn, row, checked_at)
            db.record_history(conn, row["id"], row["target"], row["text"], due_at,
                              "missed", detail=f"{count} отправок")
            who = "себе" if row["target"] == "me" else row["target"]
            db.add_notice(
                conn,
                f"Пропущено #{row['id']} → {who}: {count} отправок "
                f"с {due_at:%d.%m %H:%M:%S}. Текст: {row['text'][:120]}",
            )
            continue
        if sent:
            await asyncio.sleep(send_delay)
        try:
            confirmed = bool(row["confirmation_required"])
            if row["target"] == "me":
                message = await control.send(_delivery_text(row))
                chat_id = getattr(control, "chat_id", None)
            else:
                recipient = await client.get_entity(row["target"]) if confirmed else None
                message = await client.send_message(
                    recipient or row["target"],
                    _delivery_text(row) if confirmed else row["text"], parse_mode=None,
                )
                chat_id = recipient.id if recipient else getattr(message, "chat_id", None)
            message_id = getattr(message, "id", None)
            pending_id = (db.open_pending(conn, row["id"], row["target"], row["text"], chat_id)
                          if confirmed else None)
            db.record_history(conn, row["id"], row["target"], row["text"], due_at,
                              "sent", message_id=message_id, chat_id=chat_id,
                              pending_id=pending_id, confirmation_requested=confirmed)
            db.advance(conn, row)
            sent += 1
            log.info("Отправлено напоминание #%d → %s", row["id"], row["target"])
        except FloodWaitError as exc:
            log.warning("Лимит Telegram: ожидание %d секунд", exc.seconds)
            await asyncio.sleep(exc.seconds + 5)
            return
        except (OSError, ConnectionError, asyncio.TimeoutError):
            raise
        except Exception as exc:
            log.exception("Не удалось отправить напоминание #%d", row["id"])
            db.record_history(conn, row["id"], row["target"], row["text"], due_at,
                              "failed", detail=type(exc).__name__)
            db.postpone(conn, row, 5)
            db.add_notice(
                conn,
                f"Не отправлено #{row['id']} → {row['target']} "
                f"({type(exc).__name__}). Повторю через 5 минут.",
            )


async def _process_pending(client, conn, send_delay, control):
    checked_at = datetime.now()
    sent = 0
    for row in db.due_pending(conn, checked_at):
        due_at = datetime.strptime(row["next_retry"], db.DATETIME_FMT)
        if checked_at - due_at > MISSED_AFTER:
            count = db.skip_pending_missed(conn, row, checked_at)
            db.record_history(conn, row["reminder_id"], row["target"], row["text"],
                              due_at, "missed", detail=f"{count} повторов подтверждения")
            db.add_notice(conn, f"Пропущено #{row['reminder_id']} → {row['target']}: "
                                f"{count} повторов подтверждения. Следующий по расписанию.")
            continue
        if sent:
            await asyncio.sleep(send_delay)
        try:
            if row["target"] == "me":
                message = await control.send(_delivery_text(row, followup=True))
            else:
                recipient = await client.get_input_entity(row["chat_id"])
                message = await client.send_message(
                    recipient, _delivery_text(row, followup=True), parse_mode=None,
                )
            db.record_history(conn, row["reminder_id"], row["target"], row["text"],
                              due_at, "sent", message_id=getattr(message, "id", None),
                              chat_id=row["chat_id"], pending_id=row["id"],
                              confirmation_requested=True)
            db.defer_pending(conn, row["id"], row["retry_seconds"])
            sent += 1
        except FloodWaitError as exc:
            await asyncio.sleep(exc.seconds + 5)
            return
        except (OSError, ConnectionError, asyncio.TimeoutError):
            raise
        except Exception as exc:
            log.exception("Не удалось повторить напоминание #%d", row["reminder_id"])
            db.record_history(conn, row["reminder_id"], row["target"], row["text"],
                              due_at, "failed", detail=type(exc).__name__)
            db.defer_pending(conn, row["id"], 300)
            db.add_notice(conn, f"Не отправлено повторное напоминание #{row['reminder_id']} "
                                f"({type(exc).__name__}). Повторю через 5 минут.")


async def _sleep_until_next(conn, wake_event):
    next_run = db.next_run_time(conn)
    if next_run is None:
        timeout = MAX_SLEEP
    else:
        timeout = min(max((next_run - datetime.now()).total_seconds(), 0.05), MAX_SLEEP)
    try:
        await asyncio.wait_for(wake_event.wait(), timeout)
    except asyncio.TimeoutError:
        pass
    wake_event.clear()
