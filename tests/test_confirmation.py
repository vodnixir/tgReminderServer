import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import db
from telethon.tl.types import User
from ai import validate_action
from commands import handle, handle_recipient_reply, parse_reminder
from scheduler import _process_due, _process_pending


class ConfirmationStorageTests(unittest.TestCase):
    def test_existing_reminders_default_to_no_confirmation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "old.db"
            old = sqlite3.connect(path)
            old.execute("CREATE TABLE reminders (id INTEGER PRIMARY KEY, target TEXT, text TEXT,"
                        " next_run TEXT, interval_seconds INTEGER, repeats_left INTEGER,"
                        " created_at TEXT, scheduled_msg_id INTEGER, weekdays TEXT,"
                        " weekly_time TEXT, paused INTEGER NOT NULL DEFAULT 0)")
            old.execute("INSERT INTO reminders VALUES (1, 'me', 'Вода',"
                        " '2026-09-23 09:00:00', NULL, 1, '2026-09-21 12:00:00', NULL,"
                        " NULL, NULL, 0)")
            old.commit()
            old.close()
            conn = db.connect(path)
            try:
                self.assertEqual(conn.execute(
                    "SELECT confirmation_required FROM reminders WHERE id = 1"
                ).fetchone()[0], 0)
            finally:
                conn.close()


class ConfirmationParsingTests(unittest.TestCase):
    def test_opt_in_and_opt_out_are_before_time(self):
        now = datetime(2026, 9, 21, 12)
        yes = parse_reminder("с подтверждением завтра в 09:00 выпить воду", now)
        no = parse_reminder("без подтверждения завтра в 09:00 выпить воду", now)
        self.assertTrue(yes.confirmation_required)
        self.assertFalse(no.confirmation_required)
        self.assertEqual(yes.text, "выпить воду")

    def test_ai_confirmation_must_be_boolean(self):
        with self.assertRaises(ValueError):
            validate_action({"action": "create", "when": "2026-09-22T09:00:00",
                             "text": "Вода", "confirmation_required": "yes"})


class ConfirmationFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_reminder_does_not_ask_and_opted_in_reminder_asks(self):
        conn = db.connect(":memory:")
        sent = []

        class Control:
            chat_id = -100123

            async def send(self, text):
                sent.append(text)
                return SimpleNamespace(id=100 + len(sent))

        try:
            db.add_reminder(conn, "me", "Вода", datetime.now() - timedelta(seconds=2),
                            None, 1)
            db.add_reminder(conn, "me", "Таблетки", datetime.now() - timedelta(seconds=2),
                            None, 1, confirmation_required=True)
            await _process_due(None, conn, 0, Control())
            self.assertEqual(len(sent), 2)
            self.assertNotIn("Сделали?", sent[0])
            self.assertIn("Сделали?", sent[1])
            self.assertEqual(db.get_sent_by_message(conn, 102)["confirmation_requested"], 1)
        finally:
            conn.close()

    async def test_toggle_changes_only_selected_personal_reminder(self):
        conn = db.connect(":memory:")
        try:
            first = db.add_reminder(conn, "me", "Вода", datetime.now() + timedelta(hours=1),
                                    None, 1)
            second = db.add_reminder(conn, "me", "Зарядка", datetime.now() + timedelta(hours=2),
                                     None, 1)
            response = await handle(f"подтверждение {first} вкл", None, conn)
            self.assertIn("включено", response)
            self.assertEqual(db.get_reminder(conn, first)["confirmation_required"], 1)
            self.assertEqual(db.get_reminder(conn, second)["confirmation_required"], 0)
            response = await handle(f"подтверждение {first} выкл", None, conn)
            self.assertIn("выключено", response)
            self.assertEqual(db.get_reminder(conn, first)["confirmation_required"], 0)
        finally:
            conn.close()

    async def test_reply_no_records_not_done(self):
        conn = db.connect(":memory:")
        try:
            db.record_history(conn, 7, "me", "Вода", datetime.now(), "sent",
                              message_id=321, confirmation_requested=True)
            response = await handle("нет", None, conn, reply_to_msg_id=321)
            self.assertIn("не выполнено", response)
            self.assertTrue(db.has_action(conn, db.get_sent_by_message(conn, 321)["id"],
                                          "not_done"))
        finally:
            conn.close()

    async def test_snooze_keeps_confirmation_setting(self):
        conn = db.connect(":memory:")
        try:
            db.record_history(conn, 7, "me", "Вода", datetime.now(), "sent",
                              message_id=321, confirmation_requested=True)
            await handle("отложи на 10 минут", None, conn, reply_to_msg_id=321)
            self.assertEqual(db.list_reminders(conn)[0]["confirmation_required"], 1)
        finally:
            conn.close()

    async def test_other_people_can_opt_in_to_confirmation(self):
        class Client:
            async def get_entity(self, target):
                return User(id=55, username="ivan")

        conn = db.connect(":memory:")
        try:
            response = await handle("напомни @ivan с подтверждением через 10 минут: вода",
                                    Client(), conn)
            self.assertIn("Напоминание #", response)
            self.assertEqual(db.list_reminders(conn)[0]["confirmation_required"], 1)
        finally:
            conn.close()

    async def test_other_person_is_reminded_until_confirmed(self):
        class Client:
            def __init__(self):
                self.sent = []

            async def get_entity(self, target):
                return User(id=55, username="ivan")

            async def get_input_entity(self, chat_id):
                return chat_id

            async def send_message(self, target, text, parse_mode=None):
                self.sent.append(text)
                return SimpleNamespace(id=100 + len(self.sent), chat_id=55)

        class Control:
            async def send(self, text):
                return None

        conn = db.connect(":memory:")
        client = Client()
        try:
            rid = db.add_reminder(conn, "@ivan", "Вода", datetime.now() - timedelta(seconds=2),
                                  None, 1, confirmation_required=True)
            await _process_due(client, conn, 0, Control())
            self.assertIsNone(db.get_reminder(conn, rid))
            self.assertIn("Сделали?", client.sent[0])
            pending = db.pending_for_chat(conn, 55)
            self.assertEqual(len(pending), 1)
            self.assertIn("Ожидают подтверждения", await handle("список", client, conn))
            conn.execute("UPDATE pending_confirmations SET next_retry = ? WHERE id = ?",
                         ((datetime.now() - timedelta(seconds=2)).strftime(db.DATETIME_FMT),
                          pending[0]["id"]))
            conn.commit()
            await _process_pending(client, conn, 0, Control())
            self.assertEqual(len(client.sent), 2)
            response = await handle_recipient_reply("да сделал", 55, 55, 102,
                                                     client, conn)
            self.assertIn("отмечено выполнение", response.lower())
            self.assertEqual(db.pending_for_chat(conn, 55), [])
        finally:
            conn.close()

    async def test_other_person_can_choose_shorter_retry_in_free_text(self):
        class Interpreter:
            async def interpret_response(self, text, now, pending):
                return {"action": "snooze", "duration_seconds": 180}

        conn = db.connect(":memory:")
        try:
            pending_id = db.open_pending(conn, 7, "@ivan", "Вода", 55)
            db.record_history(conn, 7, "@ivan", "Вода", datetime.now(), "sent",
                              message_id=200, chat_id=55, pending_id=pending_id,
                              confirmation_requested=True)
            response = await handle_recipient_reply("через три минутки ещё раз", 55,
                                                     55, 200, None, conn,
                                                     interpreter=Interpreter())
            self.assertIn("3м", response)
            when = datetime.strptime(db.pending_for_chat(conn, 55)[0]["next_retry"],
                                     db.DATETIME_FMT)
            self.assertAlmostEqual((when - datetime.now()).total_seconds(), 180,
                                   delta=2)
        finally:
            conn.close()

    async def test_reply_from_another_chat_cannot_complete_reminder(self):
        conn = db.connect(":memory:")
        try:
            pending_id = db.open_pending(conn, 7, "@ivan", "Вода", 55)
            db.record_history(conn, 7, "@ivan", "Вода", datetime.now(), "sent",
                              message_id=200, chat_id=55, pending_id=pending_id,
                              confirmation_requested=True)
            self.assertIsNone(await handle_recipient_reply(
                "да сделал", 66, 66, 200, None, conn))
            self.assertIsNotNone(db.get_pending(conn, pending_id))
        finally:
            conn.close()

    async def test_recipient_can_change_requested_retry_before_it_fires(self):
        conn = db.connect(":memory:")
        try:
            pending_id = db.open_pending(conn, 7, "@ivan", "Вода", 55)
            db.record_history(conn, 7, "@ivan", "Вода", datetime.now(), "sent",
                              message_id=200, chat_id=55, pending_id=pending_id,
                              confirmation_requested=True)
            await handle_recipient_reply("напомни через 3 мин", 55, 55, 200, None, conn)
            response = await handle_recipient_reply("напомни через 5 мин", 55, 55,
                                                     200, None, conn)
            self.assertIn("5м", response)
            next_retry = datetime.strptime(db.get_pending(conn, pending_id)["next_retry"],
                                           db.DATETIME_FMT)
            self.assertAlmostEqual((next_retry - datetime.now()).total_seconds(), 300,
                                   delta=2)
        finally:
            conn.close()

    async def test_delete_one_time_confirmation_stops_followups(self):
        conn = db.connect(":memory:")
        try:
            pending_id = db.open_pending(conn, 7, "@ivan", "Вода", 55)
            response = await handle("удали 7", None, conn)
            self.assertIn("удалено", response)
            self.assertIsNone(db.get_pending(conn, pending_id))
        finally:
            conn.close()

    async def test_new_occurrence_replaces_old_pending_reply(self):
        conn = db.connect(":memory:")
        try:
            old_id = db.open_pending(conn, 7, "@ivan", "Вода", 55)
            db.record_history(conn, 7, "@ivan", "Вода", datetime.now(), "sent",
                              message_id=200, chat_id=55, pending_id=old_id,
                              confirmation_requested=True)
            new_id = db.open_pending(conn, 7, "@ivan", "Вода", 55)
            db.record_history(conn, 7, "@ivan", "Вода", datetime.now(), "sent",
                              message_id=201, chat_id=55, pending_id=new_id,
                              confirmation_requested=True)
            self.assertNotEqual(old_id, new_id)
            self.assertIsNone(await handle_recipient_reply(
                "да сделал", 55, 55, 200, None, conn))
            self.assertIsNotNone(db.get_pending(conn, new_id))
        finally:
            conn.close()

    async def test_missed_followups_are_reported_without_catchup(self):
        class Client:
            def __init__(self):
                self.sent = []

            async def get_input_entity(self, chat_id):
                return chat_id

            async def send_message(self, target, text, parse_mode=None):
                self.sent.append(text)

        conn = db.connect(":memory:")
        client = Client()
        try:
            pending_id = db.open_pending(conn, 7, "@ivan", "Вода", 55)
            conn.execute("UPDATE pending_confirmations SET next_retry = ? WHERE id = ?",
                         ((datetime.now() - timedelta(minutes=12)).strftime(db.DATETIME_FMT),
                          pending_id))
            conn.commit()
            await _process_pending(client, conn, 0, None)
            self.assertEqual(client.sent, [])
            self.assertIn("2 повторов", db.pending_notices(conn)[0]["text"])
            next_retry = datetime.strptime(db.get_pending(conn, pending_id)["next_retry"],
                                           db.DATETIME_FMT)
            self.assertGreater(next_retry, datetime.now())
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
