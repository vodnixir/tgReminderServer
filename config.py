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

SEND_DELAY = float(os.getenv("SEND_DELAY", "0"))
if SEND_DELAY < 0:
    raise SystemExit("SEND_DELAY не может быть отрицательным.")
CONTROL_GROUP = os.getenv("CONTROL_GROUP", "repeat until")
CONTROL_TOPIC = os.getenv("CONTROL_TOPIC", "reminders")
