import unittest
from types import SimpleNamespace

from telethon import types

from control import ControlTopic, find_control_topic


class TopicFilterTests(unittest.TestCase):
    def test_only_selected_topic_is_accepted(self):
        topic = ControlTopic(None, None, -100123, 42)
        def event(chat_id, reply_to):
            return SimpleNamespace(
                chat_id=chat_id,
                message=SimpleNamespace(reply_to=reply_to),
            )
        self.assertTrue(topic.contains(event(-100123, SimpleNamespace(
            reply_to_top_id=None, reply_to_msg_id=42,
        ))))
        self.assertTrue(topic.contains(event(-100123, SimpleNamespace(
            reply_to_top_id=42, reply_to_msg_id=55,
        ))))
        self.assertFalse(topic.contains(event(-100123, SimpleNamespace(
            reply_to_top_id=99, reply_to_msg_id=55,
        ))))
        self.assertFalse(topic.contains(event(-100999, SimpleNamespace(
            reply_to_top_id=42, reply_to_msg_id=42,
        ))))


class TopicLookupTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_group_and_topic_are_selected(self):
        class Client:
            async def iter_dialogs(self):
                yield SimpleNamespace(
                    name="Repeat Until",
                    input_entity="peer",
                    entity=types.PeerChannel(123),
                )

            async def __call__(self, request):
                self.request = request
                return SimpleNamespace(topics=[
                    SimpleNamespace(title="reminders", id=42, closed=False),
                ])

        client = Client()
        topic = await find_control_topic(client, "repeat until", "Reminders")
        self.assertEqual(topic.topic_id, 42)
        self.assertEqual(client.request.q, "Reminders")
        self.assertEqual(client.request.peer, "peer")

    async def test_long_status_stays_in_topic(self):
        class Client:
            sent = []

            async def send_message(self, *args, **kwargs):
                self.sent.append((args, kwargs))

        client = Client()
        topic = ControlTopic(client, "peer", -100123, 42)
        await topic.send("x" * 4001)
        self.assertEqual([len(args[1]) for args, _ in client.sent], [3901, 102])
        self.assertTrue(all(args[1].startswith("\u2063") for args, _ in client.sent))
        self.assertTrue(all(kwargs["reply_to"] == 42 for _, kwargs in client.sent))


if __name__ == "__main__":
    unittest.main()
