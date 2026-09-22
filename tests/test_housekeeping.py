import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import db
from commands import handle
from control import temporary_seconds
from scheduler import (
    _ensure_personal_notifications,
    _process_cleanup,
    _process_due,
    cancel_legacy_scheduled,
)


class NotificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")

    def tearDown(self):
        self.conn.close()

    async def test_personal_notification_is_scheduled_only_in_last_minute(self):
        now = datetime(2026, 9, 22, 12, 0, 0)
        late = db.add_reminder(self.conn, "me", "Вода", now + timedelta(minutes=2),
                               None, 1)

        class Client:
            def __init__(self):
                self.sent = []

            async def send_message(self, entity, text, **kwargs):
                self.sent.append((entity, text, kwargs))
                return SimpleNamespace(id=777)

        client = Client()
        await _ensure_personal_notifications(client, self.conn, now)
        self.assertEqual(client.sent, [])

        self.conn.execute("UPDATE reminders SET next_run = ? WHERE id = ?",
                          ((now + timedelta(seconds=55)).strftime(db.DATETIME_FMT), late))
        self.conn.commit()
        await _ensure_personal_notifications(client, self.conn, now)

        self.assertEqual(client.sent[0][0], "me")
        self.assertIn("Вода", client.sent[0][1])
        self.assertEqual(client.sent[0][2]["schedule"],
                         (now + timedelta(seconds=55)).astimezone())
        self.assertEqual(db.get_reminder(self.conn, late)["scheduled_msg_id"], 777)

    async def test_due_personal_reminder_posts_topic_after_notification_was_scheduled(self):
        rid = db.add_reminder(self.conn, "me", "Вода",
                              datetime.now() - timedelta(seconds=2), None, 1)
        self.conn.execute("UPDATE reminders SET scheduled_msg_id = 777 WHERE id = ?", (rid,))
        self.conn.commit()

        class Control:
            chat_id = -100123

            async def send(self, text):
                return SimpleNamespace(id=501)

        await _process_due(None, self.conn, 0, Control())
        sent = db.get_sent_by_message(self.conn, 501)
        self.assertEqual(sent["reminder_id"], rid)
        cleanups = db.list_cleanup(self.conn)
        self.assertEqual(cleanups[0]["scope"], "saved")
        self.assertEqual(cleanups[0]["message_id"], 777)

    async def test_restart_keeps_cleanup_for_an_alert_that_may_have_fired(self):
        rid = db.add_reminder(self.conn, "me", "Вода",
                              datetime.now() - timedelta(seconds=2), None, 1)
        self.conn.execute("UPDATE reminders SET scheduled_msg_id = 777 WHERE id = ?", (rid,))
        self.conn.commit()

        class Client:
            async def get_input_entity(self, target):
                return target

            async def __call__(self, request):
                self.request = request

        await cancel_legacy_scheduled(Client(), self.conn)

        cleanup = db.list_cleanup(self.conn)[0]
        self.assertEqual((cleanup["scope"], cleanup["message_id"]), ("saved", 777))
        self.assertIsNone(db.get_reminder(self.conn, rid)["scheduled_msg_id"])


class CleanupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")

    def tearDown(self):
        self.conn.close()

    async def test_done_queues_every_control_message_for_occurrence(self):
        pending_id = db.open_pending(self.conn, 7, "me", "Вода", -100123)
        scheduled_for = datetime.now()
        db.queue_cleanup(
            self.conn, "saved", 777, scheduled_for + timedelta(minutes=5),
            reminder_id=7, text="🔔 Напоминание #7\nВода",
            scheduled_for=scheduled_for,
        )
        db.record_history(
            self.conn, 7, "me", "Вода", scheduled_for, "sent",
            message_id=101, chat_id=-100123, pending_id=pending_id,
            confirmation_requested=True,
        )
        db.record_history(
            self.conn, 7, "me", "Вода", scheduled_for, "sent",
            message_id=102, chat_id=-100123, pending_id=pending_id,
            confirmation_requested=True,
        )

        response = await handle("готово", None, self.conn, reply_to_msg_id=102,
                                chat_id=-100123)

        self.assertIn("Отмечено", response)
        queued = {(row["scope"], row["message_id"]) for row in db.list_cleanup(self.conn)}
        self.assertEqual(queued, {("control", 101), ("control", 102), ("saved", 777)})
        saved = next(row for row in db.list_cleanup(self.conn) if row["scope"] == "saved")
        self.assertLessEqual(
            datetime.strptime(saved["delete_at"], db.DATETIME_FMT),
            datetime.now(),
        )

    async def test_cleanup_deletes_control_and_saved_messages(self):
        now = datetime.now()
        db.queue_cleanup(self.conn, "control", 101, now - timedelta(seconds=1))
        db.queue_cleanup(self.conn, "saved", 777, now - timedelta(seconds=1),
                         text="🔔 Напоминание #7\nВода", scheduled_for=now)

        class Client:
            def __init__(self):
                self.deleted = []

            async def delete_messages(self, entity, ids, revoke=True):
                self.deleted.append((entity, tuple(ids), revoke))

            async def get_messages(self, entity, limit):
                return [SimpleNamespace(
                    id=888, raw_text="🔔 Напоминание #7\nВода",
                    date=now.astimezone().astimezone(timezone.utc),
                )]

        client = Client()
        control = SimpleNamespace(peer="control-peer")
        await _process_cleanup(client, self.conn, control, now)

        self.assertIn(("control-peer", (101,), True), client.deleted)
        self.assertIn(("me", (777, 888), True), client.deleted)
        self.assertEqual(db.list_cleanup(self.conn), [])


class TemporaryMessageTests(unittest.TestCase):
    def test_help_and_lists_live_longer_than_short_acknowledgements(self):
        self.assertEqual(temporary_seconds("помощь", "текст"), 120)
        self.assertEqual(temporary_seconds("список", "текст"), 120)
        self.assertEqual(temporary_seconds("готово", "✅ Отмечено"), 30)
        self.assertEqual(temporary_seconds("напомни через час воду", "✅ Создано"), 30)

    def test_errors_stay_visible_for_an_hour(self):
        self.assertEqual(temporary_seconds("команда", "⚠️ Ошибка соединения"), 3600)
        self.assertEqual(temporary_seconds("команда", "Не удалось обработать"), 3600)


if __name__ == "__main__":
    unittest.main()
