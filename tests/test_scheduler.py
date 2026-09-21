import asyncio
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import db
import scheduler
from scheduler import _process_due, _send_notices, cancel_legacy_scheduled


class FakeClient:
    def __init__(self):
        self.sent = []

    async def send_message(self, *args, **kwargs):
        self.sent.append((args, kwargs))


class FakeControl:
    def __init__(self):
        self.sent = []

    async def send(self, text):
        self.sent.append(text)


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.client = FakeClient()
        self.control = FakeControl()

    def tearDown(self):
        self.conn.close()

    async def test_other_people_are_sent_directly_without_schedule(self):
        rid = db.add_reminder(
            self.conn, "@ivan", "Текст", datetime.now() - timedelta(seconds=2), None, 1,
        )
        await _process_due(self.client, self.conn, 0, self.control)
        self.assertIsNone(db.get_reminder(self.conn, rid))
        self.assertEqual(self.client.sent, [(('@ivan', 'Текст'), {'parse_mode': None})])
        self.assertEqual(self.control.sent, [])

    async def test_personal_reminder_goes_to_control_topic(self):
        db.add_reminder(self.conn, "me", "Вода", datetime.now() - timedelta(seconds=2), None, 1)
        await _process_due(self.client, self.conn, 0, self.control)
        self.assertIn("Вода", self.control.sent[0])
        self.assertEqual(self.client.sent, [])

    async def test_missed_repeat_is_reported_and_next_future_occurrence_kept(self):
        rid = db.add_reminder(
            self.conn, "@ivan", "Текст", datetime.now() - timedelta(minutes=5), 60, 10,
        )
        await _process_due(self.client, self.conn, 0, self.control)
        row = db.get_reminder(self.conn, rid)
        self.assertIsNotNone(row)
        self.assertLess(row["repeats_left"], 10)
        self.assertGreater(datetime.strptime(row["next_run"], db.DATETIME_FMT), datetime.now())
        self.assertEqual(self.client.sent, [])
        await _send_notices(self.conn, self.control)
        self.assertIn("Пропущено", self.control.sent[0])
        self.assertEqual(db.pending_notices(self.conn), [])

    async def test_missed_one_time_reminder_is_only_reported(self):
        rid = db.add_reminder(
            self.conn, "me", "Текст", datetime.now() - timedelta(minutes=3), None, 1,
        )
        await _process_due(self.client, self.conn, 0, self.control)
        self.assertIsNone(db.get_reminder(self.conn, rid))
        self.assertEqual(self.client.sent, [])
        self.assertEqual(len(db.pending_notices(self.conn)), 1)

    async def test_network_recovery_is_reported_in_topic(self):
        class Client:
            calls = 0

            async def get_me(self):
                self.calls += 1
                if self.calls == 1:
                    raise OSError("offline")
                return SimpleNamespace(id=123)

        seen = asyncio.Event()

        class Control(FakeControl):
            async def send(self, text):
                await super().send(text)
                seen.set()

        control = Control()
        with patch.object(scheduler, "RETRY_DELAY", 0):
            task = asyncio.create_task(scheduler.scheduler_loop(
                Client(), self.conn, asyncio.Event(), 0, control,
            ))
            try:
                await asyncio.wait_for(seen.wait(), 1)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertIn("Связь с Telegram восстановлена", control.sent[0])

    async def test_old_scheduled_message_is_cancelled_before_direct_delivery(self):
        rid = db.add_reminder(
            self.conn, "@ivan", "Текст", datetime.now() + timedelta(hours=1), None, 1,
        )
        self.conn.execute(
            "UPDATE reminders SET scheduled_msg_id = 777 WHERE id = ?", (rid,),
        )
        self.conn.commit()

        class Client:
            async def get_input_entity(self, target):
                return target

            async def __call__(self, request):
                self.request = request

        client = Client()
        await cancel_legacy_scheduled(client, self.conn)
        self.assertEqual(client.request.id, [777])
        self.assertIsNone(db.get_reminder(self.conn, rid)["scheduled_msg_id"])


if __name__ == "__main__":
    unittest.main()
