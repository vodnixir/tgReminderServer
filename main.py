"""Telegram login, commands in one topic, and supervised scheduler."""

import asyncio
import logging

from telethon import TelegramClient, events

import commands
import db
from ai import GroqInterpreter
from config import (
    API_HASH, API_ID, CONTROL_GROUP, CONTROL_TOPIC, DB_PATH, GROQ_API_KEY,
    GROQ_MODEL, SEND_DELAY,
    SESSION_PATH,
)
from control import find_control_topic
from scheduler import cancel_legacy_scheduled, scheduler_loop


log = logging.getLogger(__name__)


async def main():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    )
    conn = db.connect(DB_PATH)
    client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
    scheduler_task = None
    disconnect_task = None
    try:
        await client.start()
        me = await client.get_me()
        control = await find_control_topic(client, CONTROL_GROUP, CONTROL_TOPIC)
        log.info("Вошли как %s (id=%d)", me.first_name, me.id)
        log.info("Управление: %s / %s", CONTROL_GROUP, CONTROL_TOPIC)

        await cancel_legacy_scheduled(client, conn)
        wake_event = asyncio.Event()
        interpreter = GroqInterpreter(GROQ_API_KEY, GROQ_MODEL) if GROQ_API_KEY else None

        @client.on(events.NewMessage(outgoing=True, chats=control.peer))
        async def on_command(event):
            if event.sender_id != me.id or not control.contains(event):
                return
            try:
                reply = event.message.reply_to
                response = await commands.handle(
                    event.raw_text or "", client, conn,
                    reply_to_msg_id=reply.reply_to_msg_id if reply else None,
                    interpreter=interpreter,
                )
                if response:
                    wake_event.set()
                    await control.send(response)
            except Exception as exc:
                log.exception("Ошибка обработки команды")
                try:
                    await control.send(
                        f"Не удалось обработать команду ({type(exc).__name__}). "
                        "Повторите попытку или проверьте журнал приложения."
                    )
                except Exception:
                    log.exception("Не удалось сообщить об ошибке в тему")

        await control.send("✅ Приложение запущено. Команды и личные напоминания работают в этой теме.")
        scheduler_task = asyncio.create_task(
            scheduler_loop(client, conn, wake_event, SEND_DELAY, control),
            name="reminder-scheduler",
        )
        disconnect_task = asyncio.create_task(client.run_until_disconnected())
        done, _ = await asyncio.wait(
            (scheduler_task, disconnect_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            try:
                await task
            except Exception as exc:
                log.exception("Фоновая задача завершилась с ошибкой")
                try:
                    await control.send(
                        f"⚠️ Приложение перезапускается: {type(exc).__name__}. "
                        "Проверьте журнал, если сообщение повторяется."
                    )
                except Exception:
                    log.exception("Не удалось сообщить о перезапуске")
                raise
        raise RuntimeError("Соединение с Telegram завершилось; требуется перезапуск")
    finally:
        for task in (scheduler_task, disconnect_task):
            if task is not None:
                task.cancel()
        await asyncio.gather(
            *(task for task in (scheduler_task, disconnect_task) if task is not None),
            return_exceptions=True,
        )
        await client.disconnect()
        conn.close()


if __name__ == "__main__":
    asyncio.run(main())
