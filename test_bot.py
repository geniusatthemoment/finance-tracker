import os
import tempfile
import unittest
from datetime import date, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

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

    def test_expense_day_changes_at_four_am(self):
        tz = ZoneInfo("Asia/Tomsk")
        self.assertEqual(
            bot.business_day(datetime(2026, 10, 1, 3, 59, tzinfo=tz)),
            date(2026, 9, 30),
        )
        self.assertEqual(
            bot.business_day(datetime(2026, 10, 1, 4, 0, tzinfo=tz)),
            date(2026, 10, 1),
        )

    def test_explicit_calendar_date_is_allowed_after_midnight(self):
        self.assertEqual(
            bot.parse_expense_message(
                "01.10.2026 еда 500", date(2026, 9, 30), date(2026, 10, 1)
            ),
            [("Еда", "", 50000, date(2026, 10, 1), False)],
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

    def send_command(self, chat_id, text):
        old_ids = bot.ALLOWED_USER_IDS
        bot.ALLOWED_USER_IDS = frozenset({chat_id})
        try:
            with patch.object(bot, "send") as send_mock:
                bot.handle_message({"from": {"id": chat_id}, "chat": {"id": chat_id}, "text": text})
            return send_mock.call_args.args[1]
        finally:
            bot.ALLOWED_USER_IDS = old_ids

    def test_extra_income_and_refund_adjust_balance_without_deleting_purchase(self):
        bot.ensure_user(1)
        today = bot.user_today(bot.get_user(1))
        bot.save_expenses(1, [("Еда", "покупка", 100000)], today)
        self.assertIn("Записал доход", self.send_command(1, "/income подработка 500"))
        self.assertIn("Записал возврат", self.send_command(1, "/refund Еда 300"))
        self.assertEqual(bot.spent(1, today, today), 70000)
        self.assertEqual(bot.income_total(1, today, today), 50000)
        self.assertEqual(len(bot.recent_expenses(1)), 2)

    def test_refund_can_reference_purchase_and_cannot_exceed_it(self):
        bot.ensure_user(1)
        today = bot.user_today(bot.get_user(1))
        bot.save_expenses(1, [("Еда", "обед", 100000)], today)
        purchase_id = bot.recent_expenses(1)[0]["id"]
        self.assertIn("Записал возврат", self.send_command(1, f"/refund {purchase_id} 800"))
        self.assertIn("больше суммы", self.send_command(1, f"/refund {purchase_id} 300"))
        self.assertEqual(bot.spent(1, today, today), 20000)

    def test_goal_and_plan_reduce_free_balance_but_not_actual_spending(self):
        bot.ensure_user(1)
        self.send_command(1, "/salary 30000")
        today = bot.user_today(bot.get_user(1))
        plan_day = today.replace(day=bot.calendar.monthrange(today.year, today.month)[1])
        self.send_command(1, "/goal 5000")
        self.send_command(1, f"/plan {plan_day:%d.%m.%Y} ремонт 3000")
        self.assertEqual(bot.spent(1, today.replace(day=1), today), 0)
        self.assertIn("22 000 ₽", self.send_command(1, "/free"))
        self.assertIn("ремонт", self.send_command(1, "/plans"))

    def test_salary_history_keeps_old_daily_rate(self):
        bot.ensure_user(1)
        with bot.db() as connection:
            connection.execute("INSERT INTO salary_history VALUES (1, '2026-09-01', 3000000)")
            connection.execute("INSERT INTO salary_history VALUES (1, '2026-10-01', 6200000)")
            connection.execute("UPDATE users SET salary_cents = 6200000 WHERE chat_id = 1")
        self.assertEqual(bot.salary_for_day(1, date(2026, 9, 30)), 3000000)
        self.assertEqual(bot.salary_for_day(1, date(2026, 10, 1)), 6200000)
        self.assertEqual(bot.salary_month_total(1, date(2026, 9, 30)), 3000000)
        with patch.object(bot, "send", return_value={"message_id": 1}) as send_mock:
            bot.send_daily_profit(bot.get_user(1), date(2026, 9, 30))
        self.assertIn("Зарплата за день: 1 000 ₽", send_mock.call_args.args[1])

    def test_csv_import_is_atomic_and_idempotent_per_user(self):
        bot.ensure_user(1)
        bot.ensure_user(2)
        csv_content = b"date,category,comment,amount_rub\n2026-09-01,food,lunch,50.00\n"
        self.assertEqual(bot.import_csv_bytes(1, csv_content), 1)
        self.assertEqual(bot.import_csv_bytes(1, csv_content), 0)
        self.assertEqual(bot.import_csv_bytes(2, csv_content), 1)
        bad = b"date,category,comment,amount_rub\n2026-09-01,food,lunch,50.00\nbad,food,lunch,20.00\n"
        with self.assertRaises(ValueError):
            bot.import_csv_bytes(1, bad)
        self.assertEqual(bot.spent(1, date(2026, 9, 1), date(2026, 9, 1)), 5000)

    def test_month_comparison_and_weekly_digest(self):
        bot.ensure_user(1)
        bot.save_expenses(1, [("Еда", "", 10000)], date(2026, 9, 20))
        bot.save_expenses(1, [("Еда", "", 20000)], date(2026, 9, 27))
        self.assertIn("Еда", bot.weekly_digest_text(1, date(2026, 9, 28)))
        self.assertIn("Сравнение месяцев", bot.month_comparison_text(1, date(2026, 9, 28)))

    def test_anomaly_uses_prior_four_weeks(self):
        bot.ensure_user(1)
        bot.save_expenses(1, [("Такси", "", 400000)], date(2026, 9, 7))
        bot.save_expenses(1, [("Такси", "", 250000)], date(2026, 9, 28))
        self.assertIn("больше чем вдвое", bot.anomaly_text(1, "Такси", date(2026, 9, 28)))

    def test_settings_are_per_user_and_custom_cutoff_works(self):
        bot.ensure_user(1)
        bot.ensure_user(2)
        self.send_command(1, "/settings cutoff 6")
        self.send_command(1, "/settings reminders 19 21 1")
        self.assertEqual(bot.get_user(1)["day_cutoff_hour"], 6)
        self.assertEqual(bot.get_user(2)["day_cutoff_hour"], 4)
        self.assertEqual(bot.get_user(1)["reminder_hours"], "19,21,1")
        self.assertEqual(bot.business_day(datetime(2026, 10, 1, 5), 6), date(2026, 9, 30))

    def test_import_document_uses_telegram_caption(self):
        old_ids = bot.ALLOWED_USER_IDS
        bot.ALLOWED_USER_IDS = frozenset({1})
        try:
            with patch.object(bot, "handle_import_document") as importer:
                bot.handle_message({
                    "from": {"id": 1}, "chat": {"id": 1}, "caption": "/import",
                    "document": {"file_id": "file", "file_name": "expenses.csv"},
                })
            importer.assert_called_once()
        finally:
            bot.ALLOWED_USER_IDS = old_ids

    def test_export_import_roundtrip_includes_monthly_and_refund(self):
        bot.ensure_user(1)
        bot.ensure_user(2)
        day = date(2026, 9, 24)
        bot.save_monthly_expense(1, "Зал", "", 250000, day)
        bot.save_expenses(1, [("Зал", "Возврат", -50000)], day)
        content = bot.export_csv(1)
        self.assertEqual(bot.import_csv_bytes(2, content), 31)
        self.assertEqual(bot.spent(1, date(2026, 9, 1), date(2026, 9, 30)), 200000)
        self.assertEqual(bot.spent(2, date(2026, 9, 1), date(2026, 9, 30)), 200000)

    def test_salary_command_keeps_earlier_dates(self):
        bot.ensure_user(1)
        with bot.db() as connection:
            connection.execute("INSERT INTO salary_history VALUES (1, '2026-09-01', 3000000)")
            connection.execute("UPDATE users SET salary_cents = 3000000 WHERE chat_id = 1")
        self.send_command(1, "/salary 60000")
        self.assertEqual(bot.salary_for_day(1, date(2026, 9, 1)), 3000000)
        self.assertEqual(bot.salary_for_day(1, bot.user_today(bot.get_user(1))), 6000000)

    def test_upgrade_preserves_existing_salary_and_expenses(self):
        bot.ensure_user(1)
        day = date(2026, 9, 12)
        bot.save_expenses(1, [("Еда", "обед", 50000)], day)
        with bot.db() as connection:
            connection.execute("UPDATE users SET salary_cents = 4000000 WHERE chat_id = 1")
        bot.init_db()
        self.assertEqual(bot.salary_for_day(1, day), 4000000)
        self.assertEqual(bot.spent(1, day, day), 50000)
        self.assertEqual(bot.get_user(1)["day_cutoff_hour"], 4)

    def test_bus_reclassification_is_one_time_and_only_for_first_user(self):
        first_user, second_user = 563057258, 656675199
        day = date(2026, 9, 27)
        for chat_id in (first_user, second_user):
            bot.ensure_user(chat_id)
        bot.save_expenses(first_user, [
            ("транспорт", "", 4000),
            ("Транспорт", "", 4100),
            ("Транспорт", "", 8000),
            ("Транспорт", "", 12500),
            ("Такси", "", 4000),
        ], day)
        bot.save_expenses(second_user, [("Транспорт", "", 4000)], day)
        with bot.db() as connection:
            connection.execute(
                """INSERT INTO expenses(chat_id, category, comment, amount_cents, spent_on, created_at, batch_id)
                   VALUES (?, 'Транспорт', '', 4000, ?, '2026-09-27', 'monthly')""",
                (first_user, day.isoformat()),
            )
            connection.execute(
                "DELETE FROM data_migrations WHERE name = ?",
                ("563057258_transport_small_fares_to_bus_2026_09_30",),
            )

        bot.init_db()
        with bot.db() as connection:
            rows = connection.execute(
                "SELECT category, amount_cents FROM expenses WHERE chat_id = ? ORDER BY id",
                (first_user,),
            ).fetchall()
            count = connection.execute(
                "SELECT affected_rows FROM data_migrations WHERE name = ?",
                ("563057258_transport_small_fares_to_bus_2026_09_30",),
            ).fetchone()["affected_rows"]
            audited = connection.execute(
                "SELECT COUNT(*) AS count FROM data_migration_expenses WHERE migration_name = ?",
                ("563057258_transport_small_fares_to_bus_2026_09_30",),
            ).fetchone()["count"]
        self.assertEqual(
            [(row["category"], row["amount_cents"]) for row in rows],
            [("Автобус", 4000), ("Автобус", 4100), ("Автобус", 8000),
             ("Транспорт", 12500), ("Такси", 4000), ("Транспорт", 4000)],
        )
        self.assertEqual(count, 3)
        self.assertEqual(audited, 3)
        self.assertEqual(bot.category_totals(second_user, day, day)[0]["category"], "Транспорт")

        bot.save_expenses(first_user, [("Транспорт", "", 4000)], day)
        bot.init_db()
        self.assertEqual(
            [(row["category"], row["total"]) for row in bot.category_totals(first_user, day, day)],
            [("Транспорт", 20500), ("Автобус", 16100), ("Такси", 4000)],
        )

    def test_remaining_transport_becomes_taxi_once_for_first_user(self):
        first_user, second_user = 563057258, 656675199
        day = date(2026, 9, 29)
        bot.ensure_user(first_user)
        bot.ensure_user(second_user)
        bot.save_expenses(first_user, [
            ("Автобус", "", 4000),
            ("Транспорт", "", 12500),
            ("Транспорт", "такси туда", 16000),
            ("Транспорт", "такси обратно", 21000),
            ("Такси", "", 5000),
        ], day)
        bot.save_expenses(second_user, [("Транспорт", "", 12500)], day)
        migration = "563057258_remaining_transport_to_taxi_2026_09_30"
        with bot.db() as connection:
            connection.execute("DELETE FROM data_migrations WHERE name = ?", (migration,))

        bot.init_db()
        with bot.db() as connection:
            rows = connection.execute(
                "SELECT category, amount_cents FROM expenses WHERE chat_id = ? ORDER BY id",
                (first_user,),
            ).fetchall()
            count = connection.execute(
                "SELECT affected_rows FROM data_migrations WHERE name = ?", (migration,)
            ).fetchone()["affected_rows"]
            audited = connection.execute(
                "SELECT COUNT(*) AS count FROM data_migration_expenses WHERE migration_name = ?",
                (migration,),
            ).fetchone()["count"]
        self.assertEqual(
            [(row["category"], row["amount_cents"]) for row in rows],
            [("Автобус", 4000), ("Такси", 12500), ("Такси", 16000),
             ("Такси", 21000), ("Такси", 5000)],
        )
        self.assertEqual(count, 3)
        self.assertEqual(audited, 3)
        self.assertEqual(bot.category_totals(second_user, day, day)[0]["category"], "Транспорт")

        bot.save_expenses(first_user, [("Транспорт", "", 3000)], day)
        bot.init_db()
        self.assertEqual(
            bot.category_totals(first_user, day, day)[-1]["category"], "Транспорт"
        )

    def test_custom_reminder_hour_runs_once(self):
        bot.ensure_user(1)
        with bot.db() as connection:
            connection.execute("UPDATE users SET reminder_hours = '19,21,1' WHERE chat_id = 1")

        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 9, 28, 19, tzinfo=ZoneInfo("Asia/Tomsk")).astimezone(tz)

        with patch.object(bot, "datetime", FrozenDateTime), patch.object(
            bot, "send", return_value={"message_id": 1}
        ) as sender:
            bot.run_schedule()
            bot.run_schedule()
        self.assertEqual(sender.call_count, 1)
        self.assertIn("Запиши сегодняшние траты", sender.call_args.args[1])

    def test_save_and_total(self):
        bot.ensure_user(1)
        day = date(2026, 9, 24)
        bot.save_expenses(1, [("еда", "обед", 50000), ("транспорт", "", 25000)], day)
        self.assertEqual(bot.spent(1, day, day), 75000)

    def test_night_expense_is_saved_on_previous_day(self):
        bot.ensure_user(1)
        old_user_ids = bot.ALLOWED_USER_IDS
        bot.ALLOWED_USER_IDS = frozenset({1})

        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 10, 1, 2, 30, tzinfo=ZoneInfo("Asia/Tomsk")).astimezone(tz)

        try:
            with patch.object(bot, "datetime", FrozenDateTime), patch.object(bot, "send"):
                bot.handle_message(
                    {"from": {"id": 1}, "chat": {"id": 1}, "text": "Еда ночной перекус 500"}
                )
        finally:
            bot.ALLOWED_USER_IDS = old_user_ids
        self.assertEqual(bot.spent(1, date(2026, 9, 30), date(2026, 9, 30)), 50000)
        self.assertEqual(bot.spent(1, date(2026, 10, 1), date(2026, 10, 1)), 0)

    def test_salary_is_per_user_and_report_shows_monthly_profit(self):
        for chat_id in (1, 2):
            bot.ensure_user(chat_id)
        with bot.db() as connection:
            connection.execute("UPDATE users SET salary_cents = ? WHERE chat_id = 1", (10000000,))
        today = bot.user_today(bot.get_user(1))
        bot.save_expenses(1, [("Еда", "", 2500000)], today)
        expected_profit = (
            "Прибыль за месяц (зарплата − траты): 75 000 ₽"
            if bot.user_calendar_today(bot.get_user(1)) >= bot.MONTHLY_REPORT_START
            else "Прибыль за месяц (зарплата − траты на сегодня): 75 000 ₽"
        )
        self.assertIn(expected_profit, bot.report_text(bot.get_user(1)))
        period = bot.period_report_text(1, today.replace(day=1), today)
        self.assertIn("Прибыль за месяц (зарплата − траты): 75 000 ₽", period)
        self.assertNotIn("Зарплата за месяц", bot.report_text(bot.get_user(2)))

    def test_report_switches_to_calendar_month_on_october_first(self):
        bot.ensure_user(1)
        bot.save_expenses(1, [("Еда", "сентябрь", 100000)], date(2026, 9, 30))
        bot.save_expenses(1, [("Транспорт", "октябрь", 20000)], date(2026, 10, 1))

        class FrozenDateTime(datetime):
            current = datetime(2026, 9, 30, 23, 59, tzinfo=ZoneInfo("Asia/Tomsk"))

            @classmethod
            def now(cls, tz=None):
                return cls.current.astimezone(tz)

        with patch.object(bot, "datetime", FrozenDateTime):
            september = self.send_command(1, "/report")
            FrozenDateTime.current = datetime(2026, 10, 1, 2, 30, tzinfo=ZoneInfo("Asia/Tomsk"))
            october = self.send_command(1, "/report")
        self.assertIn("Траты за текущий месяц", september)
        self.assertIn("Календарный месяц: 10.2026", october)
        self.assertIn("Всего: 200 ₽", october)
        self.assertNotIn("1 000 ₽", october)

    def test_month_report_includes_full_calendar_month_and_keeps_date_range(self):
        bot.ensure_user(1)
        bot.save_monthly_expense(1, "Зал", "абонемент", 310000, date(2026, 10, 1))
        user = bot.get_user(1)
        month = bot.monthly_report_text(user, date(2026, 10, 1))
        self.assertIn("Всего: 3 100 ₽", month)
        self.assertIn("В среднем в день: 100 ₽", month)
        self.assertIn("31.10", month)
        short = bot.period_report_text(1, date(2026, 10, 1), date(2026, 10, 1))
        self.assertIn("Отчёт за 01.10.2026 — 01.10.2026", short)
        self.assertIn("Всего: 100 ₽", short)

    def test_current_month_average_uses_recorded_days_only(self):
        bot.ensure_user(1)
        bot.save_expenses(1, [("Еда", "", 50000)], date(2026, 10, 1))

        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 10, 2, 12, 0, tzinfo=ZoneInfo("Asia/Tomsk")).astimezone(tz)

        with patch.object(bot, "datetime", FrozenDateTime):
            report = bot.report_text(bot.get_user(1))
        self.assertIn("Всего: 500 ₽", report)
        self.assertIn("В среднем в день: 500 ₽", report)
        self.assertIn("В среднем в неделю: 3 500 ₽", report)
        self.assertIn("В среднем в месяц (прогноз на 30 дней): 15 000 ₽", report)

    def test_current_month_average_ignores_future_monthly_allocations(self):
        bot.ensure_user(1)
        bot.save_monthly_expense(1, "Зал", "", 310000, date(2026, 10, 1))

        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 10, 2, 12, 0, tzinfo=ZoneInfo("Asia/Tomsk")).astimezone(tz)

        with patch.object(bot, "datetime", FrozenDateTime):
            report = bot.report_text(bot.get_user(1))
        self.assertIn("Всего: 3 100 ₽", report)
        self.assertIn("В среднем в день: 100 ₽", report)
        self.assertIn("В среднем в месяц (прогноз на 30 дней): 3 000 ₽", report)

    def test_current_month_report_shows_zero_spend_days_after_day_starts(self):
        bot.ensure_user(1)
        bot.save_expenses(1, [("Еда", "", 50000)], date(2026, 10, 1))

        class FrozenDateTime(datetime):
            current = datetime(2026, 10, 5, 3, 0, tzinfo=ZoneInfo("Asia/Tomsk"))

            @classmethod
            def now(cls, tz=None):
                return cls.current.astimezone(tz)

        with patch.object(bot, "datetime", FrozenDateTime):
            before_cutoff = bot.report_text(bot.get_user(1))
            FrozenDateTime.current = datetime(2026, 10, 5, 12, 0, tzinfo=ZoneInfo("Asia/Tomsk"))
            after_cutoff = bot.report_text(bot.get_user(1))
        self.assertIn("• 04.10 — 0 ₽", before_cutoff)
        self.assertNotIn("• 05.10 — 0 ₽", before_cutoff)
        self.assertIn("• 05.10 — 0 ₽", after_cutoff)
        self.assertNotIn("• 06.10 — 0 ₽", after_cutoff)
        self.assertIn("В среднем в день: 500 ₽", after_cutoff)

    def test_past_calendar_month_report_shows_zero_spend_days(self):
        bot.ensure_user(1)
        bot.save_expenses(1, [("Еда", "", 50000)], date(2026, 9, 1))
        report = bot.monthly_report_text(bot.get_user(1), date(2026, 9, 1))
        self.assertIn("• 02.09 — 0 ₽", report)
        self.assertIn("• 30.09 — 0 ₽", report)

    def test_month_navigation_and_future_month_validation(self):
        bot.ensure_user(1)
        today = date(2026, 10, 3)
        self.assertEqual(bot.parse_report_month("09.2026", today), date(2026, 9, 1))
        with self.assertRaises(ValueError):
            bot.parse_report_month("11.2026", today)
        keyboard = bot.report_keyboard(date(2026, 10, 1), date(2026, 10, 1))
        self.assertIn("report_month:2026-09", str(keyboard))
        self.assertNotIn("report_month:2026-11", str(keyboard))

    def test_report_month_command_and_callback_show_selected_month(self):
        bot.ensure_user(1)
        bot.save_expenses(1, [("Еда", "", 50000)], date(2026, 9, 15))

        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 10, 3, 12, tzinfo=ZoneInfo("Asia/Tomsk")).astimezone(tz)

        old_ids = bot.ALLOWED_USER_IDS
        bot.ALLOWED_USER_IDS = frozenset({1})
        try:
            with patch.object(bot, "datetime", FrozenDateTime), patch.object(bot, "send") as sender, patch.object(bot, "api"):
                bot.handle_message({"from": {"id": 1}, "chat": {"id": 1}, "text": "/report 09.2026"})
                self.assertIn("Календарный месяц: 09.2026", sender.call_args.args[1])
                bot.handle_callback({
                    "id": "callback", "from": {"id": 1}, "data": "report_month:2026-09",
                    "message": {"message_id": 10, "chat": {"id": 1}},
                })
                self.assertIn("Всего: 500 ₽", sender.call_args.args[1])
                bot.handle_callback({
                    "id": "callback2", "from": {"id": 1}, "data": "report:month",
                    "message": {"message_id": 11, "chat": {"id": 1}},
                })
                self.assertIn("Календарный месяц: 10.2026", sender.call_args.args[1])
        finally:
            bot.ALLOWED_USER_IDS = old_ids

    def test_missing_budget_reminder_is_weekly(self):
        bot.ensure_user(1)
        user = bot.get_user(1)
        monday = date(2026, 9, 28)
        with patch.object(bot, "send") as send_mock:
            bot.send_morning_update(user, monday)
            bot.send_morning_update(user, monday)
            bot.send_morning_update(user, monday + bot.timedelta(days=1))
        self.assertEqual(send_mock.call_count, 1)
        self.assertIn("бюджет пока не задан", send_mock.call_args.args[1])

    def test_budget_still_gets_daily_morning_summary(self):
        bot.ensure_user(1)
        with bot.db() as connection:
            connection.execute("UPDATE users SET budget_cents = ? WHERE chat_id = 1", (1000000,))
        user = bot.get_user(1)
        monday = date(2026, 9, 28)
        with patch.object(bot, "send") as send_mock:
            bot.send_morning_update(user, monday)
            bot.send_morning_update(user, monday + bot.timedelta(days=1))
        self.assertEqual(send_mock.call_count, 2)
        self.assertIn("На сегодня:", send_mock.call_args.args[1])

    def test_ten_am_morning_budget_is_separate_from_profit_and_status(self):
        bot.ensure_user(1)
        with bot.db() as connection:
            connection.execute(
                "UPDATE users SET budget_cents = ?, salary_cents = ? WHERE chat_id = 1",
                (2000000, 3100000),
            )

        class FrozenDateTime(datetime):
            current = datetime(2026, 10, 6, 9, 0, tzinfo=ZoneInfo("Asia/Tomsk"))

            @classmethod
            def now(cls, tz=None):
                return cls.current.astimezone(tz)

        with patch.object(bot, "datetime", FrozenDateTime), patch.object(
            bot, "send", return_value={"message_id": 1}
        ) as sender:
            bot.run_schedule()
            sender.assert_not_called()
            FrozenDateTime.current = datetime(2026, 10, 6, 10, 0, tzinfo=ZoneInfo("Asia/Tomsk"))
            bot.run_schedule()
            bot.run_schedule()
        messages = [call.args[1] for call in sender.call_args_list]
        self.assertEqual(len(messages), 2)
        self.assertTrue(messages[0].startswith("Доброе утро!"))
        self.assertIn("На неделю осталось:", messages[0])
        self.assertIn("Итог за 05.10.2026", messages[1])

    def test_existing_default_morning_hour_moves_to_ten_once(self):
        bot.ensure_user(1)
        bot.ensure_user(2)
        with bot.db() as connection:
            connection.execute("UPDATE users SET morning_hour = 9 WHERE chat_id = 1")
            connection.execute("UPDATE users SET morning_hour = 11 WHERE chat_id = 2")
            connection.execute(
                "DELETE FROM data_migrations WHERE name = 'morning_budget_at_10_2026_10_06'"
            )
        bot.init_db()
        self.assertEqual(bot.get_user(1)["morning_hour"], 10)
        self.assertEqual(bot.get_user(2)["morning_hour"], 11)
        with bot.db() as connection:
            connection.execute("UPDATE users SET morning_hour = 9 WHERE chat_id = 1")
        bot.init_db()
        self.assertEqual(bot.get_user(1)["morning_hour"], 9)

    def test_week_budget_uses_same_daily_rate_as_month_remaining(self):
        bot.ensure_user(1)
        with bot.db() as connection:
            connection.execute("UPDATE users SET budget_cents = ? WHERE chat_id = 1", (2000000,))
        bot.save_expenses(1, [("Еда", "", 120200)], date(2026, 10, 5))
        user = bot.get_user(1)

        self.assertEqual(bot.budget_snapshot(user, date(2026, 10, 6)), (72300, 433800, 1879800))
        message = bot.morning_text(user, date(2026, 10, 6))
        self.assertIn("На сегодня: 723 ₽", message)
        self.assertIn("На неделю осталось: 4 338 ₽", message)
        self.assertIn("До конца месяца: 18 798 ₽", message)
        self.assertEqual(bot.budget_snapshot(user, date(2026, 10, 9))[1], 3 * bot.budget_snapshot(user, date(2026, 10, 9))[0])

    def test_week_budget_stops_at_month_end_and_recalculates_daily(self):
        bot.ensure_user(1)
        with bot.db() as connection:
            connection.execute("UPDATE users SET budget_cents = ? WHERE chat_id = 1", (310000,))
        user = bot.get_user(1)
        self.assertEqual(bot.budget_snapshot(user, date(2026, 10, 30)), (155000, 310000, 310000))
        self.assertEqual(bot.budget_snapshot(user, date(2026, 10, 31)), (310000, 310000, 310000))

    def test_daily_rounding_reaches_exact_month_budget_on_last_day(self):
        bot.ensure_user(1)
        with bot.db() as connection:
            connection.execute("UPDATE users SET budget_cents = ? WHERE chat_id = 1", (1001,))
        user = bot.get_user(1)
        first_day = bot.budget_snapshot(user, date(2026, 10, 29))[0]
        bot.save_expenses(1, [("Еда", "", first_day)], date(2026, 10, 29))
        second_day = bot.budget_snapshot(user, date(2026, 10, 30))[0]
        bot.save_expenses(1, [("Еда", "", second_day)], date(2026, 10, 30))
        last_day = bot.budget_snapshot(user, date(2026, 10, 31))[0]
        self.assertEqual(first_day + second_day + last_day, 1001)

    def test_status_command_shows_same_budget_numbers_as_morning(self):
        bot.ensure_user(1)
        with bot.db() as connection:
            connection.execute("UPDATE users SET budget_cents = ? WHERE chat_id = 1", (2000000,))
        bot.save_expenses(1, [("Еда", "", 120200)], date(2026, 10, 5))

        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 10, 6, 12, 0, tzinfo=ZoneInfo("Asia/Tomsk")).astimezone(tz)

        with patch.object(bot, "datetime", FrozenDateTime):
            status = self.send_command(1, "/status")
            morning = bot.morning_text(bot.get_user(1), date(2026, 10, 6))
        self.assertIn("Потрачено в этом месяце: 1 202 ₽", status)
        self.assertIn("Бюджет на месяц: 20 000 ₽", status)
        for line in ("На сегодня: 723 ₽", "На неделю осталось: 4 338 ₽", "До конца месяца: 18 798 ₽"):
            self.assertIn(line, status)
            self.assertIn(line, morning)

    def test_status_without_budget_still_shows_month_expenses(self):
        bot.ensure_user(1)
        bot.save_expenses(1, [("Еда", "", 50000)], date(2026, 10, 5))

        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 10, 6, 12, 0, tzinfo=ZoneInfo("Asia/Tomsk")).astimezone(tz)

        with patch.object(bot, "datetime", FrozenDateTime):
            status = self.send_command(1, "/status")
        self.assertIn("Потрачено в этом месяце: 500 ₽", status)
        self.assertIn("Бюджет на месяц не задан", status)
        self.assertNotIn("На сегодня:", status)

    def test_existing_users_table_gets_salary_column(self):
        with bot.db() as connection:
            connection.execute("DROP TABLE users")
            connection.execute(
                "CREATE TABLE users (chat_id INTEGER PRIMARY KEY, budget_cents INTEGER NOT NULL DEFAULT 0, timezone TEXT NOT NULL, created_at TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO users(chat_id, budget_cents, timezone, created_at) VALUES (1, 123000, 'Asia/Tomsk', '2026-09-01')"
            )
        bot.init_db()
        user = bot.get_user(1)
        self.assertEqual(user["budget_cents"], 123000)
        self.assertEqual(user["salary_cents"], 0)

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

    def test_specific_expense_can_be_replaced_and_deleted(self):
        day = date(2026, 9, 24)
        bot.save_expenses(1, [("Еда", "старый", 50000)], day)
        expense_id = bot.recent_expenses(1)[0]["id"]
        replacement = ("Транспорт", "такси", 70000, day, False)

        bot.replace_expense(1, expense_id, replacement, day)
        row = bot.recent_expenses(1)[0]
        self.assertEqual((row["category"], row["comment"], row["total"]), ("Транспорт", "такси", 70000))
        deleted = bot.delete_expense(1, row["id"])
        self.assertEqual(deleted["total"], 70000)
        self.assertEqual(bot.recent_expenses(1), [])

    def test_period_report_and_comparison(self):
        bot.save_expenses(1, [("Еда", "", 12000)], date(2026, 9, 22))
        bot.save_expenses(1, [("Транспорт", "", 8000)], date(2026, 9, 23))
        report = bot.period_report_text(1, date(2026, 9, 22), date(2026, 9, 23))
        self.assertIn("Всего: 200 ₽", report)
        self.assertIn("Еда: 120 ₽", report)
        self.assertIn("Сравнение недель", bot.comparison_text(1, date(2026, 9, 23)))

    def test_category_limit_warning(self):
        day = date(2026, 9, 24)
        bot.set_category_limit(1, "еда", 100000)
        bot.save_expenses(1, [("Еда", "", 85000)], day)
        warning = bot.category_limit_warning(1, "ЕДА", day)
        self.assertIn("85%", warning)

    def test_recurring_payment_is_generated_once_per_month(self):
        bot.ensure_user(1)
        bot.add_recurring(1, 5, "Интернет", "домашний", 90000)
        user = bot.get_user(1)
        with patch.object(bot, "send") as send_mock:
            bot.process_recurring(user, date(2026, 9, 26))
            bot.process_recurring(user, date(2026, 9, 26))
        self.assertEqual(bot.spent(1, date(2026, 9, 5), date(2026, 9, 5)), 90000)
        self.assertEqual(send_mock.call_count, 1)

    def test_favorites_search_and_export(self):
        day = date(2026, 9, 24)
        favorite_id = bot.add_favorite(1, "Еда", "Обед с Колей", 60000)
        self.assertGreater(favorite_id, 0)
        bot.save_expenses(1, [("Еда", "Обед с Колей", 60000)], day)
        self.assertEqual(len(bot.search_expenses(1, "колей")), 1)
        exported = bot.export_csv(1).decode("utf-8-sig")
        self.assertIn("comment", exported)
        self.assertIn("Обед с Колей", exported)

    def test_history_edit_and_delete_callbacks_are_scoped(self):
        old_user_ids = bot.ALLOWED_USER_IDS
        bot.ALLOWED_USER_IDS = frozenset({1})
        day = date(2026, 9, 24)
        bot.save_expenses(1, [("Еда", "обед", 50000)], day)
        expense_id = bot.recent_expenses(1)[0]["id"]
        base = {
            "id": "callback",
            "from": {"id": 1},
            "message": {"message_id": 10, "chat": {"id": 1}},
        }
        try:
            with patch.object(bot, "api"), patch.object(bot, "send"):
                bot.handle_callback({**base, "data": f"edit:{expense_id}"})
            self.assertEqual(bot.get_pending_action(1)["expense_id"], expense_id)
            bot.clear_pending_action(1)
            with patch.object(bot, "api"), patch.object(bot, "send"):
                bot.handle_callback({**base, "data": f"delete_confirm:{expense_id}"})
            self.assertEqual(bot.recent_expenses(1), [])
        finally:
            bot.ALLOWED_USER_IDS = old_user_ids

    def test_favorite_callback_adds_expense(self):
        old_user_ids = bot.ALLOWED_USER_IDS
        bot.ALLOWED_USER_IDS = frozenset({1})
        bot.ensure_user(1)
        favorite_id = bot.add_favorite(1, "Кофе", "капучино", 25000)
        callback = {
            "id": "callback",
            "from": {"id": 1},
            "data": f"favorite:{favorite_id}",
            "message": {"message_id": 10, "chat": {"id": 1}},
        }
        try:
            with patch.object(bot, "api"), patch.object(bot, "send"):
                bot.handle_callback(callback)
            today = bot.user_today(bot.get_user(1))
            self.assertEqual(bot.spent(1, today, today), 25000)
        finally:
            bot.ALLOWED_USER_IDS = old_user_ids

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

    def test_reminders_fire_at_20_22_and_midnight_once(self):
        bot.ensure_user(1)

        class FrozenDateTime(datetime):
            current = None

            @classmethod
            def now(cls, tz=None):
                return cls.current.astimezone(tz)

        tz = ZoneInfo("Asia/Tomsk")
        times = [(28, 20), (28, 22), (29, 0)]
        with patch.object(bot, "datetime", FrozenDateTime), patch.object(
            bot, "send", return_value={"message_id": 1}
        ) as send_mock:
            for day, hour in times:
                FrozenDateTime.current = datetime(2026, 9, day, hour, tzinfo=tz)
                bot.run_schedule()
                bot.run_schedule()
        self.assertEqual(send_mock.call_count, 3)
        self.assertEqual(
            [call.args[1] for call in send_mock.call_args_list],
            [
                "Запиши сегодняшние траты. Например: еда 500, транспорт 250",
                "Напоминаю про траты 👀 Скинь всё одним сообщением через запятую.",
                "Последний догон за вчера: укажи дату, например «23.09 еда 500», или нажми «Вчера без трат».",
            ],
        )
        self.assertIn("Вчера без трат", str(send_mock.call_args.args[2]))

    def test_ten_am_profit_uses_previous_month_day_count(self):
        bot.ensure_user(1)
        with bot.db() as connection:
            connection.execute("UPDATE users SET salary_cents = ? WHERE chat_id = 1", (3100000,))
        bot.save_expenses(1, [("Еда", "", 120000)], date(2026, 9, 30))

        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 10, 1, 10, 0, tzinfo=ZoneInfo("Asia/Tomsk")).astimezone(tz)

        with patch.object(bot, "datetime", FrozenDateTime), patch.object(
            bot, "send", return_value={"message_id": 1}
        ) as send_mock:
            bot.run_schedule()
            bot.run_schedule()
        self.assertEqual(send_mock.call_count, 1)
        message = send_mock.call_args.args[1]
        self.assertIn("Итог за 30.09.2026", message)
        self.assertIn("Зарплата за день: 1 033.33 ₽", message)
        self.assertIn("Траты: 1 200 ₽", message)
        self.assertIn("Прибыль за день: -166.67 ₽", message)

    def test_ten_am_profit_skips_user_without_salary(self):
        bot.ensure_user(1)
        with patch.object(bot, "send") as send_mock:
            bot.send_daily_profit(bot.get_user(1), date(2026, 9, 30))
        send_mock.assert_not_called()

    def test_failed_reminder_is_retried(self):
        day = date(2026, 9, 28)
        with patch.object(bot, "send", side_effect=[None, {"message_id": 1}]) as send_mock:
            bot.reminder(1, day, "reminder22", "Запиши траты")
            self.assertFalse(bot.was_sent(1, day, "reminder22"))
            bot.reminder(1, day, "reminder22", "Запиши траты")
        self.assertTrue(bot.was_sent(1, day, "reminder22"))
        self.assertEqual(send_mock.call_count, 2)


if __name__ == "__main__":
    unittest.main()
