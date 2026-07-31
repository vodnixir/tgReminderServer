"""Отправка напоминаний себе через обычного бота (Bot API).

Сообщения со своего аккаунта Telegram считает исходящими и никогда не
показывает на них пуш-уведомления. Сообщение от бота — входящее, поэтому
оно приходит на телефон как обычное уведомление.
"""
import asyncio
import json
import urllib.parse
import urllib.request


def _send_sync(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    with urllib.request.urlopen(url, data=data, timeout=30) as resp:
        payload = json.loads(resp.read().decode())
    if not payload.get("ok"):
        raise RuntimeError(f"Bot API отказал: {payload}")


async def send_via_bot(token, chat_id, text):
    await asyncio.to_thread(_send_sync, token, chat_id, text)
