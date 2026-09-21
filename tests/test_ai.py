import json
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import ai
from ai import GroqInterpreter, validate_action


class AiTests(unittest.IsolatedAsyncioTestCase):
    async def test_recipient_reply_interpretation_is_limited_to_confirmation(self):
        class Fake(GroqInterpreter):
            def _request(self, payload):
                self.payload = payload
                return {"choices": [{"message": {"content": json.dumps({
                    "action": "snooze", "reminder_id": 7, "when": None,
                    "text": None, "target": None, "weekdays": [],
                    "interval_seconds": None, "repeats": None,
                    "duration_seconds": 180, "limit": None, "reason": "",
                    "confirmation_required": None,
                })}}]}

        interpreter = Fake("example-key")
        action = await interpreter.interpret_response(
            "через три минутки ещё раз", datetime(2026, 9, 21, 12),
            {"reminder_id": 7, "text": "Вода", "target": "@ivan"})
        self.assertEqual(action["duration_seconds"], 180)
        self.assertIn("Вода", interpreter.payload["messages"][1]["content"])

    def test_request_uses_official_groq_client(self):
        class FakeCompletions:
            def create(self, **kwargs):
                self.payload = kwargs
                return SimpleNamespace(model_dump=lambda: {"choices": []})

        completions = FakeCompletions()
        fake = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        with patch.object(ai, "Groq", return_value=fake) as factory:
            interpreter = GroqInterpreter("example-key")
            response = interpreter._request({"model": "openai/gpt-oss-20b"})
        factory.assert_called_once()
        self.assertEqual(completions.payload["model"], "openai/gpt-oss-20b")
        self.assertEqual(response, {"choices": []})

    def test_rejects_unsafe_or_incomplete_model_output(self):
        with self.assertRaises(ValueError):
            validate_action({"action": "delete"})
        with self.assertRaises(ValueError):
            validate_action({"action": "create", "when": "2026-09-22T10:00:00",
                             "target": "@person; rm -rf /", "text": "test"})
        with self.assertRaises(ValueError):
            validate_action({"action": "snooze", "duration_seconds": -10})

    async def test_interpreter_sends_reply_context_and_validates_response(self):
        class Fake(GroqInterpreter):
            def _request(self, payload):
                self.payload = payload
                return {"choices": [{"message": {"content": json.dumps({
                    "action": "complete", "reminder_id": 4, "when": None,
                    "text": None, "target": None, "weekdays": [],
                    "interval_seconds": None, "repeats": None,
                    "duration_seconds": None, "limit": None, "reason": "",
                })}}]}

        interpreter = Fake("example-key")
        reply = {"reminder_id": 4, "text": "Вода", "target": "me"}
        action = await interpreter.interpret("я уже выпил", datetime(2026, 9, 21, 12),
                                             [], reply)
        self.assertEqual(action["action"], "complete")
        self.assertIn("Вода", interpreter.payload["messages"][1]["content"])
        self.assertEqual(interpreter.payload["model"], "openai/gpt-oss-20b")


if __name__ == "__main__":
    unittest.main()
