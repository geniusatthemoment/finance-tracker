import base64
import gzip
import io
import json
import sqlite3
import sys
import tempfile
import time
import unittest
import urllib.error
import zipfile
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import bot
import investments as inv


ISIN = "RU000A10C8F3"


def trade(qty="2", amount="1900", nkd="20", fee="6", identity="1", when="2026-09-01"):
    return {"id": identity, "isin": ISIN, "date": when, "settled": when,
            "quantity": qty, "amount": amount, "nkd": nkd, "fee": fee}


def report(start="2026-09-01", end="2026-09-30", opening="0", qty="2", trades=None, cash=None):
    return {"account": "TEST", "start": start, "end": end,
            "bonds": {ISIN: {"name": "Тест 01", "opening": opening, "quantity": qty, "reported_value": "2000"}},
            "trades": [trade()] if trades is None else trades, "cash": cash or [], "warnings": []}


def market():
    return {"security": {"REGNUMBER": "TEST-REG", "MATDATE": "2026-12-31", "FACEUNIT": "SUR",
                         "FACEVALUE": 1000, "ACCRUEDINT": 10, "SETTLEDATE": "2026-10-01"},
            "price": 98, "quote_at": "2026-10-01", "stale": False, "schedule_stale": False,
            "description": {}, "offers": [], "schedule_at": time.time(),
            "coupons": [{"startdate": "2026-09-01", "coupondate": "2026-10-01", "value": 30, "faceunit": "RUB"},
                        {"startdate": "2026-10-01", "coupondate": "2026-12-31", "value": 90, "faceunit": "RUB"}],
            "amortizations": [{"amortdate": "2026-12-31", "value": 1000, "faceunit": "RUB"}]}


def payment(kind="coupon", amount="60", identity="cash1", when="2026-09-29"):
    return {"id": identity, "kind": kind, "amount": amount, "date": when, "regnumber": "TEST-REG", "comment": "Тест 01"}


class InvestmentsTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = self.directory.name + "/test.sqlite"
        self.old_db = bot.DB_PATH
        bot.DB_PATH = self.path
        bot.init_db()

    def tearDown(self):
        bot.DB_PATH = self.old_db
        self.directory.cleanup()

    def db(self):
        return sqlite3.connect(self.path)

    def save(self, value=None, chat=1, digest="first"):
        with self.db() as c:
            return inv.save_report(c, chat, digest, value or report())

    def state(self, chat=1):
        with self.db() as c:
            return inv.portfolio(c, chat)

    def test_repeated_and_overlapping_reports_deduplicate(self):
        self.assertTrue(self.save())
        self.assertFalse(self.save())
        self.save(report(), digest="overlap")
        state = self.state()
        self.assertEqual(len(state["bonds"][ISIN]["trades"]), 1)
        self.assertTrue(state["bonds"][ISIN]["complete"])

    def test_user_isolation(self):
        self.save()
        self.assertIsNone(self.state(2))
        self.save(report(qty="10", trades=[trade(qty="10")]), chat=2)
        self.assertEqual(self.state()["bonds"][ISIN]["quantity"], "2")

    def test_newer_snapshot_and_older_full_history(self):
        self.save(report(start="2026-09-10", opening="2", trades=[]))
        self.assertFalse(self.state()["bonds"][ISIN]["complete"])
        self.save(report(end="2026-09-09"), digest="older")
        self.assertTrue(self.state()["bonds"][ISIN]["complete"])
        self.assertEqual(self.state()["asof"], date(2026, 9, 30))

    def test_gaps_disable_profit(self):
        self.save(report(end="2026-09-05"))
        self.save(report(start="2026-09-10", opening="2", trades=[]), digest="gap")
        self.assertFalse(self.state()["bonds"][ISIN]["complete"])

    def test_conflicting_trade_rolls_back(self):
        self.save()
        with self.assertRaises(ValueError):
            self.save(report(trades=[trade(amount="999")]), digest="conflict")
        with self.db() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM investment_reports").fetchone()[0], 1)

    def test_other_account_rejected(self):
        self.save()
        different = report()
        different["account"] = "OTHER"
        with self.assertRaises(ValueError):
            self.save(different, digest="other")

    def test_nkd_not_counted_twice_and_fees_included(self):
        self.save(report(cash=[payment()]))
        state = self.state()
        r = inv.results(state["bonds"][ISIN], market(), [payment()])
        self.assertEqual(r["cost"], Decimal(1906))
        self.assertEqual(r["value"], Decimal(1980))
        self.assertEqual(r["unrealized"], Decimal(74))
        self.assertEqual(r["total"], Decimal(134))

    def test_fifo_sell_cost_and_sale_fee(self):
        trades = [trade(), trade(qty="-1", amount="1000", nkd="10", fee="3", identity="2", when="2026-09-20")]
        self.save(report(qty="1", trades=trades))
        r = inv.results(self.state()["bonds"][ISIN], market(), [])
        self.assertEqual(r["cost"], Decimal(953))
        self.assertEqual(r["realized"], Decimal(44))
        self.assertEqual(r["total"], Decimal(81))

    def test_incomplete_opening_history_disables_profit_not_receipts(self):
        self.save(report(opening="1", qty="3"))
        r = inv.results(self.state()["bonds"][ISIN], market(), [payment()])
        self.assertIsNone(r["total"])
        self.assertIsNone(r["cost"])
        self.assertEqual(r["coupons"], Decimal(60))
        self.assertEqual(r["value"], Decimal(2970))

    def test_price_coupons_accrual_and_fees_reconcile(self):
        for trades, qty, cash in [([trade()], '2', [payment()]), ([trade(), trade(qty='-1', amount='1000', nkd='10', fee='3', identity='2')], '1', [payment()])]:
            with self.db() as c:
                c.execute('DELETE FROM investment_reports')
                inv.save_report(c, 1, 'x', report(qty=qty, trades=trades, cash=cash))
            v = inv.results(self.state()['bonds'][ISIN], market(), cash)
            self.assertEqual(v['total'], v['price_result'] + v['coupons'] + v['accrued_result'] - v['fees'])

    def test_full_redemption_reconciles_without_current_quote(self):
        self.save(report(qty='0', end='2026-12-31'))
        m = market(); m['price'] = None
        cash = [payment(kind='principal', amount='2000', when='2026-12-31'), payment()]
        v = inv.results(self.state()['bonds'][ISIN], m, cash)
        self.assertEqual(v['total'], Decimal('154'))
        self.assertEqual(v['price_result'], Decimal('120'))
        self.assertEqual(v['accrued_result'], Decimal('-20'))
        self.assertEqual(v['total'], v['price_result'] + v['coupons'] + v['accrued_result'] - v['fees'])
        cash[0]['amount'] = '1000'
        self.assertIsNone(inv.results(self.state()['bonds'][ISIN], m, cash)['total'])

    def test_reimport_repaired_parser_keeps_existing_trades(self):
        self.save()
        updated = report()
        updated['bonds']['RU000A106HB4'] = {'name': 'ВУШ БО', 'opening': '0', 'quantity': '0', 'reported_value': '0'}
        with self.db() as c:
            self.assertTrue(inv.save_report(c, 1, 'first', updated))
            self.assertEqual(c.execute('SELECT COUNT(*) FROM investment_reports').fetchone()[0], 1)
            self.assertFalse(inv.save_report(c, 1, 'first', updated))

    def test_missing_price_not_zero(self):
        self.save()
        m = market()
        m["price"] = None
        r = inv.results(self.state()["bonds"][ISIN], m, [])
        self.assertIsNone(r["value"])
        self.assertIsNone(r["total"])

    def test_forecast_distinguishes_coupon_and_return_of_capital(self):
        f = inv.forecast(report()["bonds"][ISIN], market(), date(2026, 9, 30), date(2026, 12, 31))
        self.assertEqual(f["coupons"], Decimal(240))
        self.assertEqual(f["principal"], Decimal(2000))
        self.assertEqual(f["earned"], Decimal(182))

    def test_forecast_date_inclusive_start_exclusive(self):
        b = report()["bonds"][ISIN]
        f = inv.forecast(b, market(), date(2026, 10, 1), date(2026, 12, 31))
        self.assertEqual(f["coupons"], Decimal(180))
        f = inv.forecast(b, market(), date(2026, 9, 30), date(2026, 10, 1))
        self.assertEqual(f["coupons"], Decimal(60))
        self.assertEqual(f["earned"], Decimal(2))

    def test_unknown_coupon_not_zero_or_extrapolated(self):
        m = market()
        m["coupons"][1]["value"] = None
        f = inv.forecast(report()["bonds"][ISIN], m, date(2026, 9, 30), date(2026, 12, 31))
        self.assertEqual(f["coupons"], Decimal(60))
        self.assertIsNone(f["earned"])
        self.assertTrue(f["unknown"])

    def test_offers_and_stale_schedules_are_conditional(self):
        m = market()
        m["security"]["OFFERDATE"] = "2026-11-01"
        m["schedule_stale"] = True
        f = inv.forecast(report()["bonds"][ISIN], m, date(2026, 9, 30), date(2026, 12, 31))
        self.assertIsNone(f["earned"])
        self.assertEqual(len(f["unknown"]), 2)

    def test_amortization_uses_each_scheduled_amount(self):
        m = market()
        m["amortizations"] = [
            {"amortdate": "2026-11-01", "value": 200, "faceunit": "RUB"},
            {"amortdate": "2026-12-31", "value": 800, "faceunit": "RUB"}]
        f = inv.forecast(report()["bonds"][ISIN], m, date(2026, 9, 30), date(2026, 11, 1))
        self.assertEqual(f["principal"], Decimal(400))

    def test_matching_receipts_by_regnumber(self):
        self.save(report(cash=[payment(), payment(identity="unknown", amount="5")]))
        state = self.state()
        state["cash"]["unknown"]["regnumber"] = "NOPE"
        state["cash"]["unknown"]["comment"] = "Неизвестный выпуск"
        matched, unmatched = inv.cash_by_bond(state, {ISIN: market()})
        self.assertEqual(len(matched[ISIN]), 1)
        self.assertEqual(len(unmatched), 1)

    def test_cache_ttl_and_stale_fallback(self):
        fetch = unittest.mock.Mock(return_value={"test": 1})
        api = inv.Market(self.db, fetch)
        self.assertFalse(api.cached("x", "/x", 100)[2])
        api.cached("x", "/x", 100)
        self.assertEqual(fetch.call_count, 1)
        fetch.side_effect = urllib.error.URLError("offline")
        value, _, stale = api.cached("x", "/x", -1)
        self.assertEqual(value, {"test": 1})
        self.assertTrue(stale)
        self.assertEqual(api.cached("missing", "/x", 100), ({}, None, True))

    def test_primary_board_and_previous_price_fallback(self):
        api = inv.Market(self.db)
        payloads = [
            {"securities": {"columns": ["BOARDID", "PREVPRICE"], "data": [["TQCB", 95], ["OTHER", 10]]},
             "marketdata": {"columns": ["BOARDID", "LAST"], "data": [["TQCB", None], ["OTHER", 20]]}},
            {"boards": {"columns": ["boardid", "is_primary"], "data": [["TQCB", 1]]}}, {}]
        with patch.object(api, "cached", side_effect=[(p, 1, False) for p in payloads]):
            self.assertEqual(api.security(ISIN)["price"], 95)
        with self.assertRaises(ValueError):
            api.security("../../anything")

    def test_profit_commands_and_date_errors(self):
        self.save(report(cash=[payment()]))
        api = SimpleNamespace(all=lambda _: {ISIN: market()})
        with self.db() as c:
            text, _ = inv.report_text(c, 1, "/profit", "", date(2026, 10, 1), api)
            self.assertIn("134.00 ₽", text)
            text, _ = inv.report_text(c, 1, "/maturity", "", date(2026, 10, 1), api)
            self.assertIn("394.00 ₽", text)
            for arg in ("", "bad", "31.02.2026", "01.09.2026"):
                with self.assertRaises(ValueError):
                    inv.report_text(c, 1, "/forecast", arg, date(2026, 10, 1), api)

    def test_pnl_compact_total_matches_profit_and_fees_not_subtracted_twice(self):
        self.save(report(cash=[payment()]))
        api = SimpleNamespace(all=lambda _: {ISIN: market()})
        with self.db() as c:
            text, keyboard = inv.report_text(c, 1, "/pnl", "", date(2026, 10, 1), api)
        self.assertTrue(text.startswith("Твой результат: 134.00 ₽"))
        self.assertIn("Получено купонов: 60.00 ₽", text)
        self.assertIn("Изменение цены бумаг: 80.00 ₽", text)
        self.assertIn("Потеряно на комиссиях: 6.00 ₽", text)
        self.assertNotIn("ISIN:", text)
        self.assertEqual(keyboard['inline_keyboard'][0][0]['callback_data'], 'investdetail:profit:')
        self.assertLess(len(text), 1000)

    def test_pnl_negative_result(self):
        self.save()
        m = market()
        m["price"] = 80
        with self.db() as c:
            text, _ = inv.report_text(c, 1, "/pnl", "", date(2026, 10, 1), SimpleNamespace(all=lambda _: {ISIN: m}))
        self.assertTrue(text.startswith("Твой результат: -286.00 ₽"))

    def test_pnl_incomplete_history_never_presented_as_whole_portfolio(self):
        r = report()
        other = "RU000A10BS76"
        r["bonds"][other] = dict(r["bonds"][ISIN], name="Нет истории", opening="2")
        self.save(r)
        with self.db() as c:
            text, _ = inv.report_text(c, 1, "/pnl", "", date(2026, 10, 1), SimpleNamespace(all=lambda _: {ISIN: market(), other: market()}))
        self.assertTrue(text.startswith("Твой результат: пока не могу посчитать всё"))
        self.assertIn("По 1 из 2 облигаций: 74.00 ₽", text)
        self.assertIn("Не хватает истории или цены: Нет истории", text)

    def test_pnl_unknown_cash_and_missing_prices(self):
        p = payment()
        p["regnumber"], p["comment"] = "UNKNOWN", "Unknown"
        self.save(report(cash=[p]))
        m = market()
        with self.db() as c:
            text, _ = inv.report_text(c, 1, "/pnl", "", date(2026, 10, 1), SimpleNamespace(all=lambda _: {ISIN: m}))
            self.assertIn("пока не могу посчитать всё", text)
            self.assertIn("Не сопоставлено выплат: 1", text)
            m["price"] = None
            m["stale"] = True
            text, _ = inv.report_text(c, 1, "/pnl", "", date(2026, 10, 1), SimpleNamespace(all=lambda _: {ISIN: m}))
        self.assertIn("По 0 из 1 облигаций: нет данных", text)
        self.assertIn("часть данных сохранённая", text)

    def test_daily_price_and_nkd_changes_reconcile(self):
        self.save(report(cash=[payment()]))
        state = self.state(); m = market()
        first = inv.daily_snapshot(state, {ISIN: m}, '2026-10-01T10:00:00+07:00')
        m['price'] = 97; m['security']['ACCRUEDINT'] = 12
        second = inv.daily_snapshot(state, {ISIN: m}, '2026-10-02T10:00:00+07:00')
        text = inv.daily_change_text(second, first)
        self.assertIn('01.10 10:00: -16.00 ₽', text)
        self.assertIn('без НКД: -20.00 ₽', text)
        self.assertIn('Изменение НКД: +4.00 ₽', text)
        self.assertEqual(second['ledger'], first['ledger'])

    def test_daily_history_change_resets_comparison(self):
        self.save()
        first = inv.daily_snapshot(self.state(), {ISIN: market()}, '2026-10-01T10:00:00+07:00')
        self.save(report(end='2026-10-01', cash=[payment()]), digest='new')
        second = inv.daily_snapshot(self.state(), {ISIN: market()}, '2026-10-02T10:00:00+07:00')
        text = inv.daily_change_text(second, first)
        self.assertIn('История операций обновлена', text)
        self.assertNotIn('Изменение оценки с', text)
        self.assertIn('за всю загруженную историю', text)

    def test_daily_first_and_unconfirmed_payments_are_explicit(self):
        self.save()
        first = inv.daily_snapshot(self.state(), {ISIN: market()}, '2026-10-01T10:00:00+07:00')
        text = inv.daily_change_text(first, None)
        self.assertIn('первую оценку', text)
        self.assertIn('не подтверждены отчётом', text)
        self.assertNotIn('Изменение оценки с', text)

    def test_daily_missing_or_stale_quotes_not_zero(self):
        self.save(); m = market()
        m['stale'] = True
        with self.assertRaises(ValueError):
            inv.daily_snapshot(self.state(), {ISIN: m}, '2026-10-01T10:00:00+07:00')
        m['stale'] = False; m['price'] = None
        with self.assertRaises(ValueError):
            inv.daily_snapshot(self.state(), {ISIN: m}, '2026-10-01T10:00:00+07:00')

    def test_daily_worker_persists_baseline_and_sends_only_when_requested(self):
        self.save()
        with patch.object(inv.Market, 'all', return_value={ISIN: market()}), patch.object(bot, 'send', return_value={'message_id': 1}) as send:
            bot.investment_daily_worker(1, date(2026,10,1), '2026-10-01T15:00:00+07:00', False)
            send.assert_not_called()
            bot.investment_daily_worker(1, date(2026,10,2), '2026-10-02T10:00:00+07:00')
            self.assertTrue(bot.was_sent(1, date(2026,10,2), 'investment_daily'))
            self.assertIn('01.10 15:00', send.call_args.args[1])
        with self.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM investment_daily_snapshots WHERE chat_id=1').fetchone()[0], 2)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM investment_daily_snapshots WHERE chat_id=2').fetchone()[0], 0)

    def test_daily_scheduler_uses_local_ten_and_deduplicates(self):
        self.save(); user={'chat_id':1}
        with self.db() as c:
            c.execute('INSERT INTO investment_daily_snapshots VALUES (?,?,?)', (1,'2026-09-30','{}'))
        with patch.object(bot, 'ALLOWED_USER_IDS', {1}), patch.object(bot.INVESTMENT_WORKERS, 'submit') as submit:
            try:
                bot.schedule_investment_daily(user, datetime.fromisoformat('2026-10-01T09:59:00+07:00'))
                submit.assert_not_called()
                bot.schedule_investment_daily(user, datetime.fromisoformat('2026-10-01T10:00:00+07:00'))
                bot.schedule_investment_daily(user, datetime.fromisoformat('2026-10-01T10:01:00+07:00'))
                self.assertEqual(submit.call_count, 1)
                self.assertTrue(submit.call_args.args[-1])
                bot.INVESTMENT_DAILY_PENDING.discard(1)
                bot.mark_sent(1,date(2026,10,1),'investment_daily')
                bot.schedule_investment_daily(user, datetime.fromisoformat('2026-10-01T10:02:00+07:00'))
                self.assertEqual(submit.call_count, 1)
            finally:
                bot.INVESTMENT_DAILY_PENDING.discard(1)

    def test_pnl_route_registered(self):
        message = {"chat": {"id": 1}, "from": {"id": 1}, "text": "/pnl"}
        with patch.object(bot, "ALLOWED_USER_IDS", {1}), patch.object(bot, "handle_investment_command") as handle:
            bot.handle_message(message)
            handle.assert_called_once()

    def test_plain_commands_do_not_intercept_expenses(self):
        self.assertEqual(inv.command_text('СКОЛЬКО ЗАРАБОТАЛ'), '/pnl')
        self.assertEqual(inv.command_text('до 31.12.2026'), '/forecast 31.12.2026')
        self.assertEqual(inv.command_text('/PNL'), '/pnl')
        self.assertIsNone(inv.command_text('облигации 500'))
        self.assertIsNone(inv.command_text('еда кафе 500'))

    def test_compact_reports_keep_details_available(self):
        self.save(report(cash=[payment()]))
        api = SimpleNamespace(all=lambda _: {ISIN: market()})
        with self.db() as c:
            for command, argument in [('/portfolio', ''), ('/forecast', '31.12.2026'), ('/maturity', ''), ('/payments', '')]:
                text, keyboard = inv.report_text(c, 1, command, argument, date(2026, 10, 1), api)
                self.assertIsNotNone(keyboard)
                self.assertNotIn('ISIN:', text)
                self.assertLess(len(text), 1500)
            text, _ = inv.report_text(c, 1, '/profit', '', date(2026, 10, 1), api, detailed=True)
            self.assertIn('ISIN:', text)

    def test_calculation_runs_in_background_and_rejects_duplicate(self):
        with patch.object(bot, 'send') as send, patch.object(bot.INVESTMENT_WORKERS, 'submit') as submit:
            try:
                bot.handle_investment_command(1, '/pnl', {'timezone': 'Asia/Tomsk'})
                bot.handle_investment_command(1, '/pnl', {'timezone': 'Asia/Tomsk'})
                self.assertEqual(submit.call_count, 1)
                self.assertEqual(send.call_args_list[0].args[1], 'Считаю…')
                self.assertIn('Уже считаю', send.call_args_list[1].args[1])
            finally:
                bot.INVESTMENT_PENDING.discard(1)

    def test_worker_failure_responds_and_releases_user(self):
        bot.INVESTMENT_PENDING.add(1)
        with patch.object(inv, 'report_text', side_effect=RuntimeError('private')), patch.object(bot, 'send') as send:
            bot.investment_worker(1, '/pnl', '', date(2026, 10, 11), False)
            self.assertIn('Не получилось', send.call_args.args[1])
        self.assertNotIn(1, bot.INVESTMENT_PENDING)

    def test_detail_callback_and_date_prompt(self):
        c = {'id': 'q', 'from': {'id': 1}, 'message': {'chat': {'id': 1, 'type': 'private'}}, 'data': 'investdetail:profit:'}
        with patch.object(bot, 'ALLOWED_USER_IDS', {1}), patch.object(bot, 'api'), patch.object(bot, 'handle_investment_command') as handle, patch.object(bot, 'send') as send:
            bot.handle_callback(c)
            self.assertTrue(handle.call_args.kwargs['detailed'])
            c['data'] = 'investcmd:forecast'
            bot.handle_callback(c)
            self.assertIn('до 31.12.2026', send.call_args.args[1])

    def test_bond_ambiguity_buttons(self):
        r = report()
        r["bonds"]["RU000A10BS76"] = dict(r["bonds"][ISIN], name="Тест 02", opening="0", quantity="0")
        self.save(r)
        with self.db() as c:
            text, keyboard = inv.report_text(c, 1, "/bond", "Тест", date(2026, 10, 1), None)
        self.assertEqual(len(keyboard["inline_keyboard"]), 2)
        self.assertEqual(keyboard["inline_keyboard"][0][0]["callback_data"], "investbond:" + ISIN)

    def test_bootstrap_idempotent_and_allowlisted(self):
        payload = {"chat_id": 1, "sha256": "seed", "report": report()}
        encoded = base64.b64encode(gzip.compress(json.dumps(payload).encode())).decode()
        with self.db() as c:
            with self.assertRaises(ValueError):
                inv.bootstrap(c, encoded, {2})
            inv.bootstrap(c, encoded, {1})
            inv.bootstrap(c, encoded, {1})
            self.assertEqual(c.execute("SELECT COUNT(*) FROM investment_reports").fetchone()[0], 1)

    def test_no_changes_to_expenses(self):
        bot.ensure_user(1)
        bot.save_expenses(1, [("Еда", "Кафе", "", 10000)], date(2026, 9, 30))
        before = bot.spent(1, date(2026, 9, 1), date(2026, 9, 30))
        self.save()
        bot.init_db()
        self.assertEqual(bot.spent(1, date(2026, 9, 1), date(2026, 9, 30)), before)
        self.assertIsNotNone(self.state())

    def test_command_dispatch_auth_and_group_privacy(self):
        message = {"chat": {"id": 1, "type": "private"}, "from": {"id": 1}, "text": "/profit"}
        with patch.object(bot, "ALLOWED_USER_IDS", {1}), patch.object(bot, "handle_investment_command") as handle:
            bot.handle_message(message)
            handle.assert_called_once()
            handle.reset_mock()
            message["from"]["id"] = 2
            bot.handle_message(message)
            handle.assert_not_called()
            message["from"]["id"] = 1
            message["chat"]["type"] = "group"
            bot.handle_message(message)
            handle.assert_not_called()

    def test_csv_import_dispatch_preserved(self):
        message = {"chat": {"id": 1}, "from": {"id": 1}, "caption": "/import", "document": {"file_name": "expenses.csv"}}
        with patch.object(bot, "ALLOWED_USER_IDS", {1}), patch.object(bot, "handle_import_document") as handle:
            bot.handle_message(message)
            handle.assert_called_once_with(1, message["document"])

    def test_telegram_chunking(self):
        with patch.object(bot, "send") as send:
            bot.send_investment_text(1, "a" * 9000, {"inline_keyboard": []})
        self.assertEqual(send.call_count, 3)
        self.assertTrue(all(len(c.args[1]) <= 3500 for c in send.call_args_list))
        self.assertEqual("".join(c.args[1] for c in send.call_args_list), "a" * 9000)


class ParserTest(unittest.TestCase):
    def test_alfa_columns_merged_rows_and_repos(self):
        def row(**values):
            result = [None] * 34
            for key, value in values.items():
                result[int(key[1:])] = value
            return tuple(result)
        positions = [row(c9="01.09.2026 - 30.09.2026", c20="№ 123 от 01.01.2026")]
        positions += [row(c2="Инструмент", c6="Актив ¹", c12="кол-во, шт.", c14="стоимость², руб.", c15="кол-во, шт.", c17="стоимость², руб.")]
        positions += [row(c2="Облигации", c6="Тест 01", c12=0, c14=0, c15=2, c17=2000), row(c6=ISIN), ()]
        header = row(c4="№ сделки", c7="Дата\nзаключен.", c10="Дата\nрасчетов", c12="ISIN/рег.код", c14="Актив", c16="Количество актива⁷", c19="Сумма сделки", c21="В т.ч. НКД", c23="Валюта расчетов", c24="Комиссия\nбанка", c28="Коммент.")
        purchase = row(c4="1\nother", c7="01.09.2026 12:00:00", c10="02.09.2026 00:00:00", c12=ISIN, c16=2, c19=1900, c21=20, c23="RUB", c24=6)
        repo = row(c4="repo", c7="01.09.2026", c10="02.09.2026", c12=ISIN, c16=-2, c19=1900, c23="RUB", c28="репо ч.1")
        cash = [row(c2="Дата", c9="Наименование операции", c10="Комментарий"), row(c14="RUB")]
        cash += [row(c2=datetime(2026, 9, 29), c6=datetime(2026, 9, 29, 14), c10="погашение купона 4B02-04-00492-R-002P", c14=60)]
        closed = row(c4='closed', c7='01.09.2026', c10='02.09.2026', c12='RU000A106HB4', c14='ВУШ БО 001P-02', c16=4, c19=4000, c21=10, c23='RUB', c24=4)
        stock = row(c4='stock', c12='RU0009029540', c14='Сбербанк')
        sheets = {"Динамика позиций": positions, "Завершенные сделки": [header, purchase, repo, closed, stock], " Движение ДС": cash,
                  'Неторговые операции': [()] * 15 + [row(c6='ВУШ БО 001P-02 RU000A106HB4', c10='Аннулирование выпуска WHOOSH001Р02.') ]}
        class Workbook:
            def __iter__(self):
                return iter([SimpleNamespace(title=k, iter_rows=lambda values_only, rows=v: iter(rows)) for k, v in sheets.items()])
            def close(self):
                pass
        content = io.BytesIO()
        with zipfile.ZipFile(content, "w"):
            pass
        with patch.dict(sys.modules, {"openpyxl": SimpleNamespace(load_workbook=lambda *a, **k: Workbook())}):
            parsed = inv.parse_alfa(content.getvalue())
        self.assertEqual(len(parsed["trades"]), 2)
        self.assertEqual(parsed['bonds']['RU000A106HB4']['quantity'], '0')
        self.assertNotIn('RU0009029540', parsed['bonds'])
        self.assertFalse(parsed['warnings'])
        self.assertEqual(parsed["trades"][0]["nkd"], "20")
        self.assertEqual(parsed["cash"][0]["regnumber"], "4B02-04-00492-R-002P")
        self.assertEqual(parsed["bonds"][ISIN]["quantity"], "2")
        self.assertNotIn("Клиент", json.dumps(parsed))

    def test_invalid_file_and_nonfinite_numbers(self):
        with self.assertRaises(ValueError):
            inv.parse_alfa(b"not xlsx")
        for value in ("NaN", "Infinity", None):
            with self.assertRaises(ValueError):
                inv.dec(value)


if __name__ == "__main__":
    unittest.main()
