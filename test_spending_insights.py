import io
import json
import os
import sqlite3
import tempfile
import unittest
import urllib.error
from datetime import date, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import bot
import spending_insights as insights


class InsightTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_path = bot.DB_PATH
        bot.DB_PATH = os.path.join(self.temp.name, 'test.sqlite3')
        bot.init_db()
        for chat_id in (563057258, 656675199):
            bot.ensure_user(chat_id)
        bot.INSIGHT_PENDING.clear()
        bot.INSIGHT_RETRY_AFTER.clear()
        self.day = date(2026, 10, 29)

    def tearDown(self):
        bot.DB_PATH = self.old_path
        bot.INSIGHT_PENDING.clear()
        bot.INSIGHT_RETRY_AFTER.clear()
        self.temp.cleanup()

    def add(self, user=563057258, amount=50000, day=None, comment='PRIVATE COMMENT'):
        bot.save_expenses(user, [('Еда', 'Кафе', comment, amount)], day or self.day)

    def test_manual_and_completed_windows(self):
        self.assertEqual(insights.window('day', self.day), (self.day, self.day, date(2026,10,28), date(2026,10,28)))
        self.assertEqual(insights.window('week', date(2026,11,2), True),
                         (date(2026,10,26), date(2026,11,1), date(2026,10,19), date(2026,10,25)))
        self.assertEqual(insights.window('month', date(2026,11,1), True),
                         (date(2026,10,1), date(2026,10,31), date(2026,9,1), date(2026,9,30)))
        self.assertEqual(insights.window('month', date(2026,3,31)),
                         (date(2026,3,1), date(2026,3,31), date(2026,2,1), date(2026,2,28)))
        self.assertEqual(insights.window('month', date(2028,3,1), True)[0:2], (date(2028,2,1), date(2028,2,29)))

    def test_cache_is_per_user_and_invalidates_when_expenses_change(self):
        self.add()
        self.add(656675199, 90000)
        dates = insights.window('day', self.day)
        with patch.object(insights, 'choose_fact', side_effect=lambda facts, *args: facts[0]) as choose:
            one = insights.fact(bot.db, 563057258, 'day', dates)
            self.assertIn('500 ₽', one)
            self.assertEqual(one, insights.fact(bot.db, 563057258, 'day', dates))
            self.assertEqual(choose.call_count, 1)
            self.assertIn('900 ₽', insights.fact(bot.db, 656675199, 'day', dates))
            self.add(amount=10000)
            self.assertIn('600 ₽', insights.fact(bot.db, 563057258, 'day', dates))
            self.assertEqual(choose.call_count, 3)

    def test_empty_period_does_not_call_api_or_claim_actual_zero(self):
        with patch.object(insights, 'choose_fact') as choose:
            text = insights.fact(bot.db, 563057258, 'day', insights.window('day', self.day))
        choose.assert_not_called()
        self.assertIn('Записанных трат', text)

    def test_provider_only_receives_facts_not_comments_or_user_ids(self):
        self.add(comment='НЕ ОТПРАВЛЯТЬ ЭТО')
        captured = []
        def respond(request, **kwargs):
            captured.append(json.loads(request.data))
            return io.BytesIO(json.dumps({'choices':[{'message':{'content':'{"fact_index":0}'}}]}).encode())
        with patch.dict(os.environ, {'RIVER_API_KEY':'test-key'}), patch.object(insights.urllib.request, 'urlopen', side_effect=respond):
            text = insights.fact(bot.db, 563057258, 'day', insights.window('day', self.day))
        body = json.dumps(captured, ensure_ascii=False)
        self.assertNotIn('НЕ ОТПРАВЛЯТЬ ЭТО', body)
        self.assertNotIn('563057258', body)
        self.assertNotIn('656675199', body)
        self.assertIn('500 ₽', text)
        self.assertEqual(captured[0]['model'], insights.MODEL)

    def test_invalid_or_hallucinated_selection_rejected(self):
        for content in ('{"fact_index":-1}', '{"fact_index":99}', '{"fact_index":true}', 'I invented a fact'):
            with self.subTest(content=content), patch.dict(os.environ, {'RIVER_API_KEY':'test-key'}), patch.object(insights.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps({'choices':[{'message':{'content':content}}]}).encode())):
                with self.assertRaises(insights.InsightError):
                    insights.choose_fact(['Only verified fact'], 'day', insights.window('day', self.day), {'count':1}, {'count':0})

    def test_refunds_and_zero_baseline_do_not_divide_by_zero(self):
        rows = [('2026-10-29','Еда','Кафе',50000), ('2026-10-29','Продукты','Магазин',-30000)]
        current = insights.aggregate(rows, self.day, self.day)
        previous = insights.aggregate([('2026-10-28','Еда','Кафе',0)], date(2026,10,28), date(2026,10,28))
        facts = insights.candidates(current, previous)
        self.assertIn('200 ₽', facts[0])
        self.assertTrue(any('Возвраты' in value for value in facts))
        self.assertTrue(any('100%' in value for value in facts))
        self.assertFalse(any('250%' in value for value in facts))

    def test_monthly_comparison_uses_daily_average(self):
        current = insights.aggregate([('2026-10-01','Еда','',310000)], date(2026,10,1), date(2026,10,31))
        previous = insights.aggregate([('2026-09-01','Еда','',300000)], date(2026,9,1), date(2026,9,30))
        self.assertFalse(any('выше' in fact or 'выросли' in fact for fact in insights.candidates(current, previous)))

    def test_aggregate_reads_do_not_mutate_ledger(self):
        self.add()
        with bot.db() as connection:
            before = [tuple(row) for row in connection.execute('SELECT * FROM expenses')]
        with patch.object(insights, 'choose_fact', side_effect=lambda facts, *args: facts[0]):
            insights.fact(bot.db, 563057258, 'month', insights.window('month', self.day))
        bot.init_db()
        with bot.db() as connection:
            self.assertEqual(before, [tuple(row) for row in connection.execute('SELECT * FROM expenses')])

    def test_summary_routing_and_legacy_portfolio_alias(self):
        message = {'from':{'id':563057258}, 'chat':{'id':563057258,'type':'private'}, 'text':'/summary week'}
        with patch.object(bot, 'ALLOWED_USER_IDS', frozenset({563057258})), patch.object(bot, 'queue_expense_insights') as queue, patch.object(bot, 'handle_investment_command') as portfolio:
            bot.handle_message(message)
            self.assertEqual(queue.call_args.args[2], ['week'])
            message['text'] = 'сводка'
            bot.handle_message(message)
            self.assertEqual(queue.call_args.args[2], ['day','week','month'])
            message['text'] = 'сводка портфеля'
            bot.handle_message(message)
            portfolio.assert_called_once()

    def test_summary_unauthorized_and_group_are_silent(self):
        with patch.object(bot, 'ALLOWED_USER_IDS', frozenset({563057258})), patch.object(bot, 'queue_expense_insights') as queue:
            bot.handle_message({'from':{'id':99}, 'chat':{'id':99}, 'text':'/summary'})
            bot.handle_message({'from':{'id':563057258}, 'chat':{'id':-123,'type':'group'}, 'text':'/summary'})
        queue.assert_not_called()

    def test_schedule_dates_timezones_and_no_duplicate_after_success(self):
        user = bot.get_user(563057258)
        with patch.object(bot, 'queue_expense_insights') as queue:
            bot.schedule_expense_insights(user, datetime(2026,11,2,10,0,tzinfo=ZoneInfo('Asia/Tomsk')))
            self.assertEqual(queue.call_args.args[2], ['day','week'])
            queue.reset_mock()
            bot.schedule_expense_insights(user, datetime(2026,11,1,10,0,tzinfo=ZoneInfo('Asia/Tomsk')))
            self.assertEqual(queue.call_args.args[2], ['day','month'])
            queue.reset_mock()
            bot.schedule_expense_insights(user, datetime(2026,10,29,9,59))
            bot.schedule_expense_insights(user, datetime(2026,10,29,10,5))
            queue.assert_not_called()
        with patch.object(insights, 'fact', return_value='Verified fact'), patch.object(bot, 'send', return_value={'message_id':1}):
            bot.expense_insights_worker(563057258, self.day, ['day'], True)
        with patch.object(bot, 'queue_expense_insights') as queue:
            bot.schedule_expense_insights(user, datetime(2026,10,29,10,1))
        queue.assert_not_called()

    def test_failed_api_never_marks_delivery_and_does_not_modify_expenses(self):
        self.add()
        with patch.object(insights, 'fact', side_effect=insights.InsightError('River HTTP 503')), patch.object(bot, 'send') as send:
            bot.expense_insights_worker(563057258, self.day, ['day'], True)
        send.assert_not_called()
        self.assertFalse(bot.was_sent(563057258, self.day, 'expense_ai_day'))
        self.assertEqual(bot.spent(563057258,self.day,self.day), 50000)
        self.assertNotIn(563057258, bot.INSIGHT_PENDING)
        self.assertIn(563057258, bot.INSIGHT_RETRY_AFTER)

    def test_failed_telegram_send_does_not_mark_scheduled_fact(self):
        with patch.object(insights, 'fact', return_value='Fact'), patch.object(bot, 'send', return_value=None):
            bot.expense_insights_worker(563057258, self.day, ['day'], True)
        self.assertFalse(bot.was_sent(563057258, self.day, 'expense_ai_day'))

    def test_pending_user_does_not_queue_twice(self):
        bot.INSIGHT_PENDING.add(563057258)
        with patch.dict(os.environ, {'RIVER_API_KEY':'test-key'}), patch.object(bot, 'ALLOWED_USER_IDS', frozenset({563057258})), patch.object(bot.INSIGHT_WORKERS, 'submit') as submit, patch.object(bot, 'send'):
            bot.queue_expense_insights(bot.get_user(563057258),self.day,['day'])
        submit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
