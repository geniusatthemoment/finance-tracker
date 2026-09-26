import os
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

import bot


class ExpenseParserTest(unittest.TestCase):
    def test_one_expense(self):
        self.assertEqual(bot.parse_expenses("еда 500"), [("Еда", "", 50000)])

    def test_words_between_category_and_amount_become_comment(self):
        self.assertEqual(
            bot.parse_expenses("еда обед с Колей 850"),
            [("Еда", "обед с Колей", 85000)],
        )

    def test_many_expenses(self):
        self.assertEqual(
            bot.parse_expenses("еда 500, супермаркеты 2300, мой кот 99.50"),
            [
                ("Еда", "", 50000),
                ("Супермаркеты", "", 230000),
                ("Мой", "кот", 9950),
            ],
        )

    def test_custom_category(self):
        self.assertEqual(
            bot.parse_expenses("другое какая угодно категория 42"),
            [("Другое", "какая угодно категория", 4200)],
        )

    def test_bad_format_rejected_atomically(self):
        with self.assertRaises(ValueError):
            bot.parse_expenses("еда 500, тут нет суммы")

    def test_allowed_telegram_users_are_accepted(self):
        old_user_ids = bot.ALLOWED_USER_IDS
        bot.ALLOWED_USER_IDS = frozenset({563057258, 656675199})
        try:
            self.assertTrue(bot.is_allowed({"from": {"id": 563057258}}))
            self.assertTrue(bot.is_allowed({"from": {"id": 656675199}}))
            self.assertFalse(bot.is_allowed({"from": {"id": 42}}))
            self.assertFalse(bot.is_allowed({}))
        finally:
            bot.ALLOWED_USER_IDS = old_user_ids

    def test_allowed_user_ids_parser(self):
        self.assertEqual(
            bot.parse_allowed_user_ids("563057258, 656675199"),
            frozenset({563057258, 656675199}),
        )

    def test_expense_without_date_uses_today(self):
        today = date(2026, 9, 24)
        self.assertEqual(
            bot.parse_expense_message("еда 500", today),
            [("Еда", "", 50000, today, False)],
        )

    def test_date_prefix_applies_to_every_expense(self):
        today = date(2026, 9, 24)
        expected = date(2026, 9, 23)
        self.assertEqual(
            bot.parse_expense_message("23.09 еда 500, транспорт 250", today),
            [
                ("Еда", "", 50000, expected, False),
                ("Транспорт", "", 25000, expected, False),
            ],
        )

    def test_each_expense_can_have_its_own_date(self):
        today = date(2026, 9, 24)
        self.assertEqual(
            bot.parse_expense_message("еда 500 22.09, транспорт 250 23.09.2026", today),
            [
                ("Еда", "", 50000, date(2026, 9, 22), False),
                ("Транспорт", "", 25000, date(2026, 9, 23), False),
            ],
        )

    def test_category_is_normalized(self):
        self.assertEqual(
            bot.parse_expenses("тРаНсПоРт 500"),
            [("Транспорт", "", 50000)],
        )

    def test_monthly_flag(self):
        today = date(2026, 9, 24)
        self.assertEqual(
            bot.parse_expense_message("зал 2500 ЕЖЕМЕСЯЧНО", today),
            [("Зал", "", 250000, today, True)],
        )

    def test_future_date_is_rejected(self):
        with self.assertRaises(ValueError):
            bot.parse_expense_message("25.09 еда 500", date(2026, 9, 24))

    def test_invalid_date_is_rejected(self):
        with self.assertRaises(ValueError):
            bot.parse_expense_message("31.02 еда 500", date(2026, 9, 24))


class StorageTest(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp()
        os.close(handle)
        self.old_path = bot.DB_PATH
        bot.DB_PATH = self.path
        bot.init_db()

    def tearDown(self):
        bot.DB_PATH = self.old_path
        os.unlink(self.path)

    def test_save_and_total(self):
        bot.ensure_user(1)
        day = date(2026, 9, 24)
        bot.save_expenses(1, [("еда", "обед", 50000), ("транспорт", "", 25000)], day)
        self.assertEqual(bot.spent(1, day, day), 75000)

    def test_users_data_is_isolated(self):
        day = date(2026, 9, 24)
        bot.ensure_user(563057258)
        bot.ensure_user(656675199)
        bot.save_expenses(563057258, [("Еда", "обед", 50000)], day)
        bot.save_expenses(656675199, [("Транспорт", "такси", 25000)], day)

        self.assertEqual(bot.spent(563057258, day, day), 50000)
        self.assertEqual(bot.spent(656675199, day, day), 25000)
        self.assertEqual(
            [row["category"] for row in bot.category_totals(563057258, day, day)],
            ["Еда"],
        )
        self.assertEqual(
            [row["category"] for row in bot.category_totals(656675199, day, day)],
            ["Транспорт"],
        )

    def test_daily_totals_are_grouped_and_sorted(self):
        bot.ensure_user(1)
        bot.save_expenses(1, [("еда", "", 12000)], date(2026, 9, 22))
        bot.save_expenses(
            1,
            [("еда", "", 30000), ("транспорт", "", 20000)],
            date(2026, 9, 23),
        )
        rows = bot.daily_totals(1, date(2026, 9, 1), date(2026, 9, 30))
        self.assertEqual(
            [(row["spent_on"], row["total"]) for row in rows],
            [("2026-09-22", 12000), ("2026-09-23", 50000)],
        )

    def test_monthly_expense_is_spread_exactly_across_month(self):
        bot.ensure_user(1)
        daily, days = bot.save_monthly_expense(
            1, "Зал", "абонемент", 250000, date(2026, 9, 24)
        )
        rows = bot.daily_totals(1, date(2026, 9, 1), date(2026, 9, 30))
        self.assertEqual(days, 30)
        self.assertEqual(daily, 8333)
        self.assertEqual(len(rows), 30)
        self.assertEqual(sum(row["total"] for row in rows), 250000)

    def test_history_opens_comment_and_is_isolated(self):
        day = date(2026, 9, 24)
        bot.save_expenses(1, [("Еда", "обед с Колей", 85000)], day)
        expense_id = bot.recent_expenses(1)[0]["id"]

        details = bot.expense_details(1, expense_id)
        self.assertEqual(details["category"], "Еда")
        self.assertEqual(details["comment"], "обед с Колей")
        self.assertEqual(details["total"], 85000)
        self.assertIsNone(bot.expense_details(2, expense_id))

    def test_done_button_acknowledges_and_deletes_reminder(self):
        old_user_ids = bot.ALLOWED_USER_IDS
        bot.ALLOWED_USER_IDS = frozenset({563057258, 656675199})
        callback = {
            "id": "callback-1",
            "from": {"id": 563057258},
            "data": "done:2026-09-24",
            "message": {"message_id": 99, "chat": {"id": 563057258}},
        }
        try:
            with patch.object(bot, "api") as api_mock:
                bot.handle_callback(callback)
            calls = [call.args[0] for call in api_mock.call_args_list]
            self.assertEqual(calls, ["answerCallbackQuery", "deleteMessage"])
            self.assertTrue(bot.is_acknowledged(563057258, date(2026, 9, 24)))
            self.assertEqual(
                bot.spent(563057258, date(2026, 9, 24), date(2026, 9, 24)),
                0,
            )
        finally:
            bot.ALLOWED_USER_IDS = old_user_ids


if __name__ == "__main__":
    unittest.main()
