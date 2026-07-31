"""Настройки приложения (читаются из .env рядом с этим файлом)."""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

try:
    API_ID = int(os.environ["API_ID"])
    API_HASH = os.environ["API_HASH"]
except KeyError as e:
    raise SystemExit(
        f"Не задана переменная {e.args[0]}. "
        "Скопируйте .env.example в .env и заполните API_ID и API_HASH "
        "(их выдают на https://my.telegram.org)."
    )

SESSION_PATH = str(BASE_DIR / "reminder")  # создаст файл reminder.session
DB_PATH = str(BASE_DIR / "reminders.db")

SEND_DELAY = float(os.getenv("SEND_DELAY", "4"))  # пауза между отправками подряд

# Токен бота от @BotFather (необязательно). Если задан, напоминания «себе»
# приходят от бота — как входящие, с пуш-уведомлением на телефоне.
BOT_TOKEN = os.getenv("BOT_TOKEN") or None
