# Стек проекта

## Среда выполнения

| Область | Значение | Основание |
|---|---|---|
| Язык | Python 3.10+ (`int | None` в аннотациях) | `commands.py`, `README.md` |
| Зависимости | pip и `requirements.txt`; lock-файла нет | `requirements.txt` |
| Запуск | `python main.py`, один процесс asyncio | `main.py`, `README.md` |
| Развёртывание | пример systemd с `Restart=always` | `deploy/tgreminder.service` |

## Рабочие зависимости

| Пакет | Версия | Роль | Основание |
|---|---|---|---|
| Telethon | `>=1.45,<2` | MTProto, темы, сообщения и события | `requirements.txt`, `control.py`, `main.py` |
| python-dotenv | `>=1.0` | Чтение `.env` | `requirements.txt`, `config.py` |
| sqlite3 | стандартная библиотека | Напоминания и очередь отчётов | `db.py` |

## Инструменты и команды

- Установка: `python3 -m venv venv`, `venv/bin/pip install -r requirements.txt`.
- Запуск: `venv/bin/python main.py`.
- Проверка: `venv/bin/python -m unittest discover -s tests -v`.
- Линтер и форматтер не настроены `[TODO]`.

## Конфигурация

- Обязательные переменные: `API_ID`, `API_HASH`.
- Необязательные: `CONTROL_GROUP` (по умолчанию `repeat until`), `CONTROL_TOPIC` (`reminders`), `SEND_DELAY` (0 секунд).
- Локальные файлы: `reminder.session` и `reminders.db` рядом с `config.py`.
- Время хранится по локальным часам хоста; для постоянного запуска следует проверить часовой пояс.

## Доказательства

- `requirements.txt`
- `.env.example`
- `config.py`
- `README.md`
- `deploy/tgreminder.service`
