"""Точка входа: логин в Telegram, обработка команд, запуск планировщика.

Первый запуск интерактивный — Telethon спросит номер телефона и код
подтверждения, после чего создаст файл reminder.session и дальше
будет входить автоматически.
"""
import asyncio
import logging
import time

from telethon import TelegramClient, events

import commands
import db
from config import API_ID, API_HASH, BOT_TOKEN, DB_PATH, SEND_DELAY, SESSION_PATH
from scheduler import scheduler_loop


class SelfMessageGuard:
    """Не даёт принять собственные напоминания вида «/...» за команды.

    Прямые отправки узнаём по id сообщения; доставленные серверным
    планировщиком Telegram — по тексту (их id заранее не известен).
    """

    def __init__(self):
        self._ids = set()
        self._texts = {}  # текст -> unix-время, до которого игнорировать

    def register_id(self, msg_id):
        self._ids.add(msg_id)

    def register_text(self, text, ttl=180):
        if text.lstrip().startswith("/"):
            self._texts[text] = time.time() + ttl

    def should_ignore(self, event):
        if event.message.id in self._ids:
            self._ids.discard(event.message.id)
            return True
        expires = self._texts.pop(event.raw_text, None)
        return expires is not None and time.time() <= expires


async def main():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    conn = db.connect(DB_PATH)
    client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
    await client.start()

    me = await client.get_me()
    logging.info("Вошли как %s (id=%d)", me.first_name, me.id)
    logging.info(
        "Дальние напоминания доставляет планировщик Telegram (с уведомлением); "
        "короткие (< 15 с) шлёт скрипт %s",
        "через бота" if BOT_TOKEN else "в «Избранное» без уведомления",
    )
    logging.info("Напишите /помощь себе в «Избранное», чтобы увидеть команды")

    guard = SelfMessageGuard()
    # Будит планировщик, когда команды меняют расписание.
    wake_event = asyncio.Event()

    @client.on(events.NewMessage(outgoing=True, chats="me", pattern=r"^/"))
    async def on_command(event):
        if guard.should_ignore(event):
            return
        response = await commands.handle(event, client, conn)
        if response:
            wake_event.set()
            await event.respond(response)

    asyncio.create_task(
        scheduler_loop(client, conn, wake_event, SEND_DELAY, BOT_TOKEN, me.id, guard)
    )
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
