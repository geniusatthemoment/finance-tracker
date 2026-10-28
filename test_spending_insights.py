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

    @staticmethod
    def fake_analysis(data, metrics):
        return 'На траты ушло ' + metrics[data['current']['total']['ref']] + '.'

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
        with patch.object(insights, 'generate_fact', side_effect=self.fake_analysis) as choose:
            one = insights.fact(bot.db, 563057258, 'day', dates)
            self.assertIn('500 ₽', one)
            self.assertEqual(one, insights.fact(bot.db, 563057258, 'day', dates))
            self.assertEqual(choose.call_count, 1)
            self.assertIn('900 ₽', insights.fact(bot.db, 656675199, 'day', dates))
            self.add(amount=10000)
            self.assertIn('600 ₽', insights.fact(bot.db, 563057258, 'day', dates))
            self.assertEqual(choose.call_count, 3)

    def test_empty_period_does_not_call_api_or_claim_actual_zero(self):
        with patch.object(insights, 'generate_fact') as choose:
            text = insights.fact(bot.db, 563057258, 'day', insights.window('day', self.day))
        choose.assert_not_called()
        self.assertIn('Записанных трат', text)

    def test_provider_receives_aggregates_not_templates_comments_or_user_ids(self):
        self.add(comment='НЕ ОТПРАВЛЯТЬ ЭТО')
        captured = []
        def respond(request, **kwargs):
            captured.append(json.loads(request.data))
            return io.BytesIO(json.dumps({'choices':[{'message':{'content':'{"text":"На траты ушло {{m0}}."}'}}]}).encode())
        with patch.dict(os.environ, {'RIVER_API_KEY':'test-key'}), patch.object(insights.urllib.request, 'urlopen', side_effect=respond):
            text = insights.fact(bot.db, 563057258, 'day', insights.window('day', self.day))
        body = json.dumps(captured, ensure_ascii=False)
        self.assertNotIn('НЕ ОТПРАВЛЯТЬ ЭТО', body)
        self.assertNotIn('563057258', body)
        self.assertNotIn('656675199', body)
        self.assertIn('500 ₽', text)
        self.assertEqual(captured[0]['model'], insights.MODEL)
        data = json.loads(captured[0]['messages'][1]['content'])
        self.assertIn('categories', data)
        self.assertIn('daily', data)
        self.assertNotIn('facts', data)
        self.assertNotIn('fact_index', body)
        self.assertNotIn('Сравнение:', text)

    def test_invalid_or_hallucinated_numbers_rejected(self):
        texts = ['Ушло 999 ₽ и {{m0}}.', 'Ушло девять тысяч и {{m0}}.',
                 'Расходы выросли вдвое: {{m0}}.', 'Треть расходов: {{m0}}.',
                 'Ушло {{m999}}.', 'Ушло {{m0 + 3}}.', 'Без данных всё хорошо.',
                 'Ушло {{m0}} и ９９９ рублей.', 'Потрачено семнадцать рублей: {{m0}}.',
                 'Потрачено полторы тысячи: {{m0}}.']
        for text in texts:
            with self.subTest(text=text), self.assertRaises(ValueError):
                insights.render_fact(json.dumps({'text':text}), {'m0':'500 ₽'}, [])
        self.assertEqual(insights.render_fact('{"text":"На еду ушло {{m0}} — заметная часть расходов."}', {'m0':'500 ₽'}, []),
                         'На еду ушло 500 ₽ — заметная часть расходов.')

    def test_numbers_in_real_place_names_are_allowed(self):
        self.assertEqual(insights.render_fact('{"text":"В Кафе 24 ушло {{m0}}."}', {'m0':'500 ₽'}, ['Кафе 24']),
                         'В Кафе 24 ушло 500 ₽.')

    def test_freely_written_numbers_are_checked_with_units(self):
        metrics={'m0':'3 500 ₽','m1':'33%','m2':'05.10','m3':'3'}
        text='5 октября на цветы ушло 3500 ₽ — 33% расходов.'
        self.assertEqual(insights.render_fact(json.dumps({'text':text}),metrics,[]),text)
        for text in ('Ушло 33 ₽.', 'На еду ушло 3500%.', '6 октября ушло 3500 ₽.',
                     'Потрачено 3 999 ₽.', 'Ушло 35000 ₽.'):
            with self.subTest(text=text),self.assertRaises(ValueError):
                insights.render_fact(json.dumps({'text':text}),metrics,[])

    def test_generic_one_week_phrase_is_not_a_numeric_claim(self):
        text='Даже внутри одной недели траты различаются: {{m0}}.'
        self.assertIn('500 ₽',insights.render_fact(json.dumps({'text':text}),{'m0':'500 ₽'},[]))

    def test_bad_generated_answer_is_retried_once(self):
        self.add()
        answers = ['{"text":"Ушло 999 рублей."}', '{"text":"На траты ушло {{m0}}."}']
        def respond(*args, **kwargs):
            return io.BytesIO(json.dumps({'choices':[{'message':{'content':answers.pop(0)}}]}).encode())
        with patch.dict(os.environ, {'RIVER_API_KEY':'test-key'}), patch.object(insights.urllib.request, 'urlopen', side_effect=respond) as request:
            text = insights.fact(bot.db,563057258,'day',insights.window('day',self.day))
        self.assertIn('500 ₽',text)
        self.assertEqual(request.call_count,2)

    def test_second_invalid_answer_is_not_cached(self):
        self.add()
        def respond(*args, **kwargs):
            return io.BytesIO(b'{"choices":[{"message":{"content":"{\\"text\\":\\"999 rubles\\"}"}}]}')
        with patch.dict(os.environ, {'RIVER_API_KEY':'test-key'}), patch.object(insights.urllib.request, 'urlopen', side_effect=respond) as request:
            with self.assertRaises(insights.InsightError):
                insights.fact(bot.db,563057258,'day',insights.window('day',self.day))
        self.assertEqual(request.call_count,2)
        with bot.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM expense_insight_cache').fetchone()[0],0)

    def test_old_template_cache_is_ignored(self):
        self.add()
        with bot.db() as c:
            rows=c.execute('SELECT spent_on,category,place,amount_cents FROM expenses ORDER BY spent_on,id').fetchall()
            old_fingerprint=insights.hashlib.sha256(json.dumps([list(r) for r in rows],ensure_ascii=False).encode()).hexdigest()
            c.execute('INSERT INTO expense_insight_cache VALUES (?,?,?,?,?,?)',
                      (563057258,'day',self.day.isoformat(),self.day.isoformat(),old_fingerprint,'OLD TEMPLATE'))
        with patch.object(insights,'generate_fact',side_effect=self.fake_analysis) as generate:
            text=insights.fact(bot.db,563057258,'day',insights.window('day',self.day))
        generate.assert_called_once()
        self.assertNotIn('OLD TEMPLATE',text)

    def test_refunds_and_zero_baseline_do_not_divide_by_zero(self):
        rows = [('2026-10-29','Еда','Кафе',50000), ('2026-10-29','Продукты','Магазин',-30000)]
        current = insights.aggregate(rows, self.day, self.day)
        previous = insights.aggregate([('2026-10-28','Еда','Кафе',0)], date(2026,10,28), date(2026,10,28))
        data, metrics = insights.analysis_data(current,previous,insights.window('day',self.day),'day')
        self.assertEqual(data['current']['total']['value'],'200 ₽')
        self.assertEqual(data['refunds']['value'],'300 ₽')
        self.assertNotIn('change_percent',data['total_change'])
        self.assertTrue(all('share_of_current_total' not in item for item in data['categories']))

    def test_monthly_comparison_uses_daily_average(self):
        current = insights.aggregate([('2026-10-01','Еда','',310000)], date(2026,10,1), date(2026,10,31))
        previous = insights.aggregate([('2026-09-01','Еда','',300000)], date(2026,9,1), date(2026,9,30))
        data,_=insights.analysis_data(current,previous,insights.window('month',date(2026,11,1),True),'month',True)
        self.assertEqual(data['daily_average_change']['direction'],'equal')
        self.assertEqual(data['categories'][0]['daily_average_change']['direction'],'equal')

    def test_aggregate_reads_do_not_mutate_ledger(self):
        self.add()
        with bot.db() as connection:
            before = [tuple(row) for row in connection.execute('SELECT * FROM expenses')]
        with patch.object(insights, 'generate_fact', side_effect=self.fake_analysis):
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
