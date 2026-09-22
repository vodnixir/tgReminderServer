import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import db
from commands import correct_ai_when, handle, parse_reminder
from scheduler import _process_due


class WeeklyScheduleTests(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_weekdays_and_next_occurrence(self):
        monday = datetime(2026, 9, 21, 12, 0)
        spec = parse_reminder("по будням в 09:00 проверить почту", monday)
        self.assertEqual(spec.when, datetime(2026, 9, 22, 9))
        self.assertEqual(spec.weekdays, (0, 1, 2, 3, 4))
        rid = db.add_reminder(self.conn, "me", spec.text, spec.when, None, -1,
                              weekdays=spec.weekdays)
        row = db.get_reminder(self.conn, rid)
        with patch.object(db, "datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 9, 25, 10)
            db.advance(self.conn, row)
        self.assertEqual(db.get_reminder(self.conn, rid)["next_run"],
                         "2026-09-28 09:00:00")

    def test_paused_reminder_is_not_due_and_resume_skips_old_weekdays(self):
        rid = db.add_reminder(self.conn, "me", "Вода", datetime(2026, 9, 21, 9),
                              None, -1, weekdays=(0, 1, 2, 3, 4))
        db.set_paused(self.conn, rid, True)
        self.assertEqual(db.due_reminders(self.conn, datetime(2026, 9, 22, 12)), [])
        db.set_paused(self.conn, rid, False, datetime(2026, 9, 22, 12))
        self.assertEqual(db.get_reminder(self.conn, rid)["next_run"],
                         "2026-09-23 09:00:00")

    def test_missed_weekdays_are_counted_without_catchup(self):
        rid = db.add_reminder(self.conn, "me", "Вода", datetime(2026, 9, 21, 9),
                              None, -1, weekdays=(0, 1, 2, 3, 4))
        row = db.get_reminder(self.conn, rid)
        self.assertEqual(db.skip_missed(self.conn, row, datetime(2026, 9, 25, 12)), 5)
        self.assertEqual(db.get_reminder(self.conn, rid)["next_run"],
                         "2026-09-28 09:00:00")

    def test_ai_date_is_corrected_from_user_weekday(self):
        now = datetime(2026, 9, 21, 12)
        wrong = datetime(2026, 9, 23, 18)
        self.assertEqual(correct_ai_when("перенеси на пятницу в 18", wrong, None, now),
                         datetime(2026, 9, 25, 18))
        self.assertEqual(correct_ai_when("напоминай по вторникам и четвергам в 10",
                                         datetime(2026, 9, 26, 10), (1, 3), now),
                         datetime(2026, 9, 22, 10))

    def test_ai_date_is_corrected_from_relative_phrase(self):
        now = datetime(2026, 9, 21, 12)
        wrong = datetime(2026, 10, 22, 9)
        self.assertEqual(correct_ai_when("завтра в 9 напомни", wrong, None, now),
                         datetime(2026, 9, 22, 9))
        self.assertEqual(correct_ai_when("напомни через 10 минут", wrong, None, now),
                         datetime(2026, 9, 21, 12, 10))


class HistoryTests(unittest.TestCase):
    def test_existing_database_is_migrated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.db"
            old = sqlite3.connect(path)
            old.execute("CREATE TABLE reminders (id INTEGER PRIMARY KEY, target TEXT, text TEXT,"
                        " next_run TEXT, interval_seconds INTEGER, repeats_left INTEGER,"
                        " created_at TEXT, scheduled_msg_id INTEGER)")
            old.execute("INSERT INTO reminders VALUES (1, 'me', 'Вода',"
                        " '2026-09-23 09:00:00', NULL, 1, '2026-09-21 12:00:00', NULL)")
            old.commit()
            old.close()
            conn = db.connect(path)
            try:
                row = db.get_reminder(conn, 1)
                self.assertEqual(row["paused"], 0)
                self.assertIsNone(row["weekdays"])
                self.assertEqual(db.list_history(conn), [])
            finally:
                conn.close()

    def test_sent_event_can_be_found_by_telegram_message(self):
        conn = db.connect(":memory:")
        try:
            event_id = db.record_history(conn, 9, "me", "Вода",
                                         datetime(2026, 9, 21, 9), "sent", message_id=123)
            self.assertEqual(db.get_sent_by_message(conn, 123)["id"], event_id)
            self.assertEqual(db.list_history(conn, 1)[0]["status"], "sent")
        finally:
            conn.close()


class ReplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_pause_cancels_telegram_notification(self):
        conn = db.connect(":memory:")
        try:
            rid = db.add_reminder(conn, "me", "Вода", datetime.now() + timedelta(hours=1),
                                  None, 1)
            conn.execute("UPDATE reminders SET scheduled_msg_id = 9 WHERE id = ?", (rid,))
            conn.commit()

            class Client:
                async def get_input_entity(self, target):
                    return target

                async def __call__(self, request):
                    self.request = request

            client = Client()
            response = await handle(f"пауза {rid}", client, conn)
            self.assertIn("пауза", response)
            self.assertEqual(client.request.id, [9])
            self.assertEqual(db.get_reminder(conn, rid)["paused"], 1)
            self.assertIsNone(db.get_reminder(conn, rid)["scheduled_msg_id"])
        finally:
            conn.close()

    async def test_ai_weekday_creation_corrects_wrong_model_date(self):
        class Interpreter:
            async def interpret(self, *args):
                return {"action": "create", "when": (datetime.now() + timedelta(days=4)).replace(
                    hour=10, minute=0, second=0, microsecond=0).isoformat(),
                    "text": "Полить цветы", "target": "me", "weekdays": [1, 3],
                    "interval_seconds": None, "repeats": -1}

        conn = db.connect(":memory:")
        try:
            response = await handle("напоминай по вторникам и четвергам в 10 полить цветы",
                                    None, conn, interpreter=Interpreter())
            self.assertIn("Напоминание #", response)
            row = db.list_reminders(conn)[0]
            self.assertEqual(row["weekdays"], "1,3")
            scheduled = datetime.strptime(row["next_run"], db.DATETIME_FMT)
            self.assertIn(scheduled.weekday(), (1, 3))
            self.assertGreater(scheduled, datetime.now())
        finally:
            conn.close()

    async def test_informal_command_uses_ai_and_does_not_invent_recipient(self):
        class Interpreter:
            async def interpret(self, *args):
                return {"action": "create", "when": (datetime.now() + timedelta(hours=1)).isoformat(),
                        "text": "Вода", "target": "@stranger", "weekdays": [],
                        "interval_seconds": None, "repeats": 1}

        conn = db.connect(":memory:")
        try:
            response = await handle("Через час воды бы выпить", None, conn,
                                    interpreter=Interpreter())
            self.assertIn("адресат", response)
            self.assertEqual(db.list_reminders(conn), [])
        finally:
            conn.close()

    async def test_natural_snooze_after_keyword_uses_ai(self):
        class Interpreter:
            async def interpret(self, *args):
                return {"action": "snooze", "duration_seconds": 600}

        conn = db.connect(":memory:")
        try:
            db.record_history(conn, 7, "me", "Вода", datetime.now(),
                              "sent", message_id=123)
            response = await handle("отложи минут на десять", None, conn,
                                    reply_to_msg_id=123, interpreter=Interpreter())
            self.assertIn("отложено", response)
        finally:
            conn.close()

    async def test_app_generated_message_is_not_sent_to_interpreter(self):
        class Interpreter:
            async def interpret(self, *args):
                raise AssertionError("app output must not reach Groq")

        conn = db.connect(":memory:")
        try:
            response = await handle("\u2063✅ Напоминание создано", None, conn,
                                    interpreter=Interpreter())
            self.assertIsNone(response)
        finally:
            conn.close()

    async def test_delivered_message_is_linked_to_history_for_reply(self):
        conn = db.connect(":memory:")
        try:
            rid = db.add_reminder(conn, "me", "Вода", datetime.now() - timedelta(seconds=2),
                                  None, 1)

            class Control:
                async def send(self, text):
                    return SimpleNamespace(id=123)

            await _process_due(None, conn, 0, Control())
            self.assertEqual(db.get_sent_by_message(conn, 123)["reminder_id"], rid)
            self.assertIsNone(db.get_reminder(conn, rid))
        finally:
            conn.close()

    async def test_snooze_delivered_one_time_reminder_and_mark_done(self):
        conn = db.connect(":memory:")
        try:
            db.record_history(conn, 7, "me", "Вода", datetime.now(),
                              "sent", message_id=123)
            response = await handle("отложи на 10 минут", None, conn,
                                    reply_to_msg_id=123)
            self.assertIn("отложено", response)
            self.assertEqual(db.list_reminders(conn)[0]["text"], "Вода")
            response = await handle("готово", None, conn, reply_to_msg_id=123)
            self.assertIn("Отмечено", response)
            self.assertEqual(len([r for r in db.list_history(conn) if r["status"] == "done"]), 1)
        finally:
            conn.close()

    async def test_history_shows_sent_and_missed(self):
        conn = db.connect(":memory:")
        try:
            db.record_history(conn, 1, "me", "Вода", datetime.now(), "sent")
            db.record_history(conn, 2, "@ivan", "Текст", datetime.now(), "missed")
            response = await handle("история", None, conn)
            self.assertIn("#1", response)
            self.assertIn("#2", response)
            self.assertIn("пропущено", response)
            self.assertIn("срок", response)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
