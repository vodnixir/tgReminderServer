import unittest
from datetime import datetime, timedelta

from telethon.tl.types import User

import db
from commands import handle, parse_duration, parse_reminder


NOW = datetime(2026, 9, 21, 12, 0)


class CommandParsingTests(unittest.TestCase):
    def test_duration_variants_and_spacing(self):
        for text in ("1ч30м", "1 ч 30 мин", "1 час 30 минут"):
            with self.subTest(text=text):
                self.assertEqual(parse_duration(text), 5400)
        self.assertEqual(parse_duration("2 дня"), 172800)
        self.assertEqual(parse_duration("5 мин."), 300)
        self.assertIsNone(parse_duration("2 банана"))

    def test_personal_reminder_without_target_or_separator(self):
        spec = parse_reminder("через 10 минут вынести мусор", NOW)
        self.assertEqual(spec.target, "me")
        self.assertEqual(spec.when, NOW + timedelta(minutes=10))
        self.assertEqual(spec.text, "вынести мусор")

    def test_recipient_and_repeat_count(self):
        spec = parse_reminder("@ivan через 1ч каждые 2 часа 5 раз: встреча", NOW)
        self.assertEqual((spec.target, spec.interval, spec.repeats, spec.text),
                         ("@ivan", 7200, 5, "встреча"))

    def test_daily_repeat_and_old_syntax(self):
        spec = parse_reminder("мне в 8:00 каждый день проверить почту", NOW)
        self.assertEqual((spec.interval, spec.repeats), (86400, -1))
        old = parse_reminder("я через 1ч30м / текст", NOW)
        self.assertEqual(old.text, "текст")

    def test_dates_and_spaces_around_separators(self):
        spec = parse_reminder("я 07 . 07 в 09 : 00 : встреча", NOW)
        self.assertEqual(spec.when, datetime(2027, 7, 7, 9, 0))
        leap = parse_reminder("29.02 09:00 проверка", NOW)
        self.assertEqual(leap.when, datetime(2028, 2, 29, 9, 0))

    def test_errors_are_field_specific(self):
        for text, fragment in (
            ("через 0 минут текст", "срок"),
            ("сегодня 09:00 текст", "прошло"),
            ("через 2 часа", "текст"),
            ("завтра 25:00 текст", "часы"),
        ):
            with self.subTest(text=text):
                with self.assertRaisesRegex(ValueError, fragment):
                    parse_reminder(text, NOW)


class CommandHandlingTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_and_old_command_names_add_reminders(self):
        class Client:
            async def get_entity(self, target):
                return User(id=123, username=target.lstrip("@"))

        conn = db.connect(":memory:")
        try:
            first = await handle("  напомни через 10 мин : вода  ", Client(), conn)
            second = await handle("/напиши @ivan через 2 ч / документы", Client(), conn)
            self.assertIn("Напоминание #1", first)
            self.assertIn("@ivan", second)
            self.assertEqual(len(db.list_reminders(conn)), 2)
            self.assertIsNone(await handle("случайный текст", Client(), conn))
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
