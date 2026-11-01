import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import bot


class CreatineTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for target, value in (("DB_PATH", str(Path(self.temp.name) / "bot.db")),
                              ("ALLOWED_USER_IDS", frozenset({1, 2}))):
            patcher = patch.object(bot, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        bot.init_db()
        bot.ensure_user(1)
        bot.ensure_user(2)

        class FrozenDateTime(datetime):
            current = datetime(2026, 11, 2, 2, 30, tzinfo=ZoneInfo("Asia/Tomsk"))

            @classmethod
            def now(cls, tz=None):
                return cls.current.astimezone(tz)

        self.clock = FrozenDateTime
        patcher = patch.object(bot, "datetime", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def message(self, text, user=1, chat_type="private"):
        with patch.object(bot, "send", return_value={"message_id": 1}) as sender:
            bot.handle_message({"from": {"id": user}, "chat": {"id": user, "type": chat_type}, "text": text})
        return sender

    def test_additive_grams_calendar_day_and_expenses_unchanged(self):
        today = date(2026, 11, 2)
        bot.save_expenses(1, [("Еда", "", 50000)], today)
        self.message("КРЕАТИН 5")
        response = self.message("/creatine 10").call_args.args[1]
        self.assertIn("Всего за 02.11.2026: 15 г", response)
        self.assertEqual(bot.creatine_total(1, today), 15000)
        self.assertEqual(bot.creatine_total(1, date(2026, 11, 1)), 0)
        self.assertEqual(bot.spent(1, today, today), 50000)
        bot.init_db()
        self.assertEqual(bot.creatine_total(1, today), 15000)

    def test_prefix_suffix_dates_and_fractional_grams(self):
        self.message("01.11 креатин 2,5 гр")
        self.message("/creatine 1.125 г 01.11.2026")
        self.message("креатин 10.11")  # Decimal grams, not an ambiguous date query.
        self.assertEqual(bot.creatine_total(1, date(2026, 11, 1)), 3625)
        self.assertEqual(bot.creatine_total(1, date(2026, 11, 2)), 10110)
        self.assertIn("3.625 г", self.message("01.11 /creatine").call_args.args[1])

    def test_invalid_entries_never_become_expenses(self):
        for text in ("креатин 0", "креатин -5", "креатин пять", "креатин 1.0001",
                     "31.02 креатин 5", "креатин 5 03.11", "креатин 5, еда 500"):
            self.assertIsNotNone(self.message(text).call_args, text)
        with bot.db() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM supplement_intakes").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM expenses").fetchone()[0], 0)

    def test_users_are_isolated_and_unauthorized_or_groups_silent(self):
        self.message("креатин 5", 1)
        self.message("креатин 10", 2)
        self.message("креатин 15", 99).assert_not_called()
        self.message("креатин 15", 1, "group").assert_not_called()
        self.assertIn("Записано: 5 г", self.message("/creatine", 1).call_args.args[1])
        self.assertIn("Записано: 10 г", self.message("/creatine", 2).call_args.args[1])

    def test_reminders_at_three_hours_once_per_user_across_reinitialization(self):
        with patch.object(bot, "send", return_value={"message_id": 1}) as sender:
            for day, hour in ((2, 14), (2, 20), (3, 0)):
                self.clock.current = datetime(2026, 11, day, hour, tzinfo=ZoneInfo("Asia/Tomsk"))
                bot.run_schedule()
                bot.init_db()
                bot.run_schedule()
        reminders = [call for call in sender.call_args_list if call.args[1].startswith("Напоминание про креатин")]
        self.assertEqual(len(reminders), 6)
        self.assertEqual([call.args[0] for call in reminders], [1, 2, 1, 2, 1, 2])
        self.assertIn("За 03.11", reminders[-1].args[1])

    def test_reminder_retries_failed_delivery_and_ignores_expense_ack(self):
        user = bot.get_user(1)
        now = datetime(2026, 11, 2, 14, tzinfo=ZoneInfo("Asia/Tomsk"))
        bot.mark_zero(1, now.date())
        with patch.object(bot, "send", side_effect=[None, {"message_id": 1}]) as sender:
            bot.schedule_creatine(user, now)
            self.assertFalse(bot.was_sent(1, now.date(), "creatine_reminder_14"))
            bot.schedule_creatine(user, now)
            bot.schedule_creatine(user, now)
        self.assertEqual(sender.call_count, 2)

    def test_reminders_skip_other_times_and_nonallowed_chats(self):
        with patch.object(bot, "send") as sender:
            for hour, minute in ((13, 59), (14, 5), (22, 0), (0, 5)):
                bot.schedule_creatine(bot.get_user(1), datetime(2026, 11, 2, hour, minute))
            bot.ensure_user(-100)
            bot.schedule_creatine(bot.get_user(-100), datetime(2026, 11, 2, 14))
        sender.assert_not_called()

    def test_current_month_report_includes_today_zeros_and_all_days_average(self):
        self.message("креатин 5")
        self.message("креатин 2.5")
        self.message("креатин 10", 2)
        response = self.message("/creatine_report").call_args.args[1]
        self.assertIn("11.2026 (01.11–02.11)", response)
        self.assertIn("Всего записано: 7.5 г", response)
        self.assertIn("Дней с записями: 1 из 2", response)
        self.assertIn("В среднем в день: 3.75 г", response)
        self.assertIn("01.11 — 0 г", response)
        self.assertIn("02.11 — 7.5 г", response)
        self.assertNotIn("03.11 —", response)
        self.assertIn("Всего записано: 10 г", self.message("/creatine_report", 2).call_args.args[1])

    def test_past_month_complete_and_leap_year(self):
        self.message("креатин 5 01.10.2026")
        response = self.message("/creatine_report 10.2026").call_args.args[1]
        self.assertIn("01.10–31.10", response)
        self.assertIn("Всего записано: 5 г", response)
        self.assertIn("В среднем в день: 0.16 г", response)
        self.assertIn("31.10 — 0 г", response)
        response = self.message("/creatine_report 02.2024").call_args.args[1]
        self.assertIn("29.02 — 0 г", response)
        self.assertIn("В среднем в день: 0 г", response)
        self.assertEqual(response.count("•"), 29)

    def test_report_rejects_invalid_future_month_and_preserves_storage(self):
        self.message("креатин 5")
        for argument in ("13.2026", "12.2026", "11.0000", "ноябрь", "11.2026 extra"):
            response = self.message("/creatine_report " + argument).call_args.args[1]
            self.assertNotIn("По дням:", response)
        self.message("/creatine_report", 99).assert_not_called()
        self.message("/creatine_report", 1, "group").assert_not_called()
        with bot.db() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM supplement_intakes").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM expenses").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
