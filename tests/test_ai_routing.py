import json
import unittest
from datetime import datetime, timedelta

import db
from ai import GroqInterpreter
from commands import handle, handle_recipient_reply


class OwnerAiRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_plain_completion_uses_recent_sent_reminder_with_ai(self):
        class Interpreter:
            async def interpret(self, text, now, reminders, reply, recent, pending):
                return {"action": "complete", "reminder_id": recent[0]["reminder_id"]}

        conn = db.connect(":memory:")
        try:
            sent_id = db.record_history(conn, 7, "me", "помыть посуду",
                                        datetime.now(), "sent", message_id=123,
                                        chat_id=-100123)
            response = await handle("да сделал", None, conn,
                                    interpreter=Interpreter(), chat_id=-100123)
            self.assertIn("#7", response)
            self.assertTrue(db.has_action(conn, sent_id, "done"))
        finally:
            conn.close()

    async def test_natural_list_request_uses_ai(self):
        class Interpreter:
            async def interpret(self, *args):
                return {"action": "list"}

        conn = db.connect(":memory:")
        try:
            db.add_reminder(conn, "me", "помыть посуду",
                            datetime.now() + timedelta(hours=1), None, 1)
            response = await handle("что у меня запланировано?", None, conn,
                                    interpreter=Interpreter())
            self.assertIn("помыть посуду", response)
        finally:
            conn.close()

    async def test_natural_delete_request_uses_ai(self):
        class Interpreter:
            async def interpret(self, *args):
                return {"action": "delete", "reminder_id": 7}

        conn = db.connect(":memory:")
        try:
            db.add_reminder(conn, "me", "помыть посуду",
                            datetime.now() + timedelta(hours=1), None, 1)
            conn.execute("UPDATE reminders SET id = 7 WHERE id = 1")
            conn.commit()
            response = await handle("убери напоминание 7", None, conn,
                                    interpreter=Interpreter())
            self.assertIn("#7", response)
            self.assertIsNone(db.get_reminder(conn, 7))
        finally:
            conn.close()

    async def test_model_cannot_delete_when_delete_word_is_reminder_text(self):
        class Interpreter:
            async def interpret(self, *args):
                return {"action": "delete", "reminder_id": 7}

        conn = db.connect(":memory:")
        try:
            db.add_reminder(conn, "me", "помыть посуду",
                            datetime.now() + timedelta(hours=1), None, 1)
            conn.execute("UPDATE reminders SET id = 7 WHERE id = 1")
            conn.commit()
            response = await handle("напомни завтра в 09:00 отменить встречу",
                                    None, conn, interpreter=Interpreter())
            self.assertIn("Напоминание #", response)
            self.assertIsNotNone(db.get_reminder(conn, 7))
            self.assertEqual(len(db.list_reminders(conn)), 2)
        finally:
            conn.close()

    async def test_exact_reminder_command_works_when_groq_fails(self):
        class Interpreter:
            async def interpret(self, *args):
                raise OSError("offline")

        conn = db.connect(":memory:")
        try:
            response = await handle("напомни через 30 секунд помыть посуду",
                                    None, conn, interpreter=Interpreter())
            self.assertIn("Напоминание #", response)
            self.assertEqual(db.list_reminders(conn)[0]["text"], "помыть посуду")
        finally:
            conn.close()


class RecipientAiRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_ai_selects_one_of_multiple_pending_by_message_content(self):
        class Interpreter:
            async def interpret_response(self, text, now, pending):
                self.assertion = len(pending)
                return {"action": "complete", "reminder_id": 7}

        conn = db.connect(":memory:")
        try:
            for rid, body in ((7, "помыть посуду"), (8, "принять таблетку")):
                pending_id = db.open_pending(conn, rid, "@ivan", body, 55)
                db.record_history(conn, rid, "@ivan", body, datetime.now(),
                                  "sent", message_id=100 + rid, chat_id=55,
                                  pending_id=pending_id, confirmation_requested=True)
            interpreter = Interpreter()
            response = await handle_recipient_reply("посуду помыл", 55, 55, None,
                                                     None, conn, interpreter)
            self.assertIn("#7", response)
            self.assertEqual(interpreter.assertion, 2)
            self.assertIsNone(db.get_pending_by_reminder(conn, 7))
            self.assertIsNotNone(db.get_pending_by_reminder(conn, 8))
        finally:
            conn.close()

    async def test_ai_cannot_choose_a_reminder_outside_recipient_pending_list(self):
        class Interpreter:
            async def interpret_response(self, text, now, pending):
                return {"action": "complete", "reminder_id": 999}

        conn = db.connect(":memory:")
        try:
            pending_id = db.open_pending(conn, 7, "@ivan", "помыть посуду", 55)
            db.record_history(conn, 7, "@ivan", "помыть посуду", datetime.now(),
                              "sent", message_id=107, chat_id=55,
                              pending_id=pending_id, confirmation_requested=True)
            response = await handle_recipient_reply("посуду помыл", 55, 55, None,
                                                     None, conn, Interpreter())
            self.assertIn("конкретное напоминание", response)
            self.assertIsNotNone(db.get_pending(conn, pending_id))
        finally:
            conn.close()

    async def test_plain_completion_works_without_groq_when_only_one_recent_delivery(self):
        conn = db.connect(":memory:")
        try:
            sent_id = db.record_history(conn, 7, "me", "помыть посуду",
                                        datetime.now(), "sent", message_id=123,
                                        chat_id=-100123)
            response = await handle("да сделал", None, conn, chat_id=-100123)
            self.assertIn("#7", response)
            self.assertTrue(db.has_action(conn, sent_id, "done"))
        finally:
            conn.close()

    async def test_unrelated_chat_delivery_is_not_completed(self):
        conn = db.connect(":memory:")
        try:
            sent_id = db.record_history(conn, 7, "me", "помыть посуду",
                                        datetime.now(), "sent", message_id=123,
                                        chat_id=-100123)
            await handle("да сделал", None, conn, chat_id=-100999)
            self.assertFalse(db.has_action(conn, sent_id, "done"))
        finally:
            conn.close()

    async def test_reply_to_delivery_before_chat_migration_uses_message_id(self):
        conn = db.connect(":memory:")
        try:
            first = db.record_history(conn, 7, "me", "помыть посуду",
                                      datetime.now(), "sent", message_id=123)
            second = db.record_history(conn, 8, "me", "принять таблетку",
                                       datetime.now(), "sent", message_id=124)
            response = await handle("готово", None, conn, reply_to_msg_id=123,
                                    chat_id=-100123)
            self.assertIn("#7", response)
            self.assertTrue(db.has_action(conn, first, "done"))
            self.assertFalse(db.has_action(conn, second, "done"))
        finally:
            conn.close()

    async def test_old_occurrence_is_not_inferred_after_newer_one_was_completed(self):
        conn = db.connect(":memory:")
        try:
            old = db.record_history(conn, 7, "me", "помыть посуду",
                                    datetime.now() - timedelta(minutes=20), "sent",
                                    chat_id=-100123)
            new = db.record_history(conn, 7, "me", "помыть посуду",
                                    datetime.now(), "sent", chat_id=-100123)
            db.record_history(conn, 7, "me", "помыть посуду", datetime.now(),
                              "done", source_id=new)
            await handle("да сделал", None, conn, chat_id=-100123)
            self.assertFalse(db.has_action(conn, old, "done"))
        finally:
            conn.close()

    async def test_model_receives_recent_sent_one_time_reminder(self):
        class FakeGroq(GroqInterpreter):
            def _request(self, payload):
                self.context = json.loads(payload["messages"][1]["content"])
                action = {field: None for field in (
                    "reminder_id", "when", "text", "target", "interval_seconds",
                    "repeats", "duration_seconds", "limit", "confirmation_required")}
                action.update(action="complete", reminder_id=7, weekdays=[], reason="")
                return {"choices": [{"message": {"content": json.dumps(action)}}]}

        conn = db.connect(":memory:")
        try:
            db.record_history(conn, 7, "me", "помыть посуду", datetime.now(),
                              "sent", message_id=123, chat_id=-100123)
            interpreter = FakeGroq("example-key")
            response = await handle("сделал посуду", None, conn,
                                    interpreter=interpreter, chat_id=-100123)
            self.assertIn("#7", response)
            self.assertEqual(interpreter.context["recent_deliveries"][0]["reminder_id"], 7)
            self.assertEqual(interpreter.context["recent_deliveries"][0]["text"],
                             "помыть посуду")
        finally:
            conn.close()

    async def test_ambiguous_plain_completion_asks_which_reminder(self):
        conn = db.connect(":memory:")
        try:
            for rid in (7, 8):
                db.record_history(conn, rid, "me", f"задача {rid}",
                                  datetime.now() - timedelta(minutes=2), "sent",
                                  chat_id=-100123)
            response = await handle("да сделал", None, conn, chat_id=-100123)
            self.assertIn("#7", response)
            self.assertIn("#8", response)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
