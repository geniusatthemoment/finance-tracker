"""Bond portfolios, Alfa XLSX imports and public MOEX ISS data.

No broker credentials, AI, tax calculations or changes to the expense ledger.
Amounts are Decimal RUB internally; bond prices are percent of remaining face value.
"""

import base64
import gzip
import hashlib
import io
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


ISS = "https://iss.moex.com/iss"
MAX_FILE = 10_000_000
ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")
COMMANDS = {"/portfolio", "/portfolio_day", "/profit", "/pnl", "/forecast", "/maturity", "/bond", "/payments", "/invest", "/invest_import"}
HELP = (
    "Твои облигации\n\n"
    "/pnl — сколько заработал или потерял\n"
    "/portfolio — что у тебя куплено\n"
    "/portfolio_day — утренняя сводка портфеля сейчас\n"
    "/forecast 31.12.2026 — сколько получишь к дате\n"
    "/maturity — сколько заработаешь до погашения\n"
    "/payments — когда ближайшие выплаты\n"
    "/bond Брусника — подробнее об облигации\n\n"
    "Можно просто написать «сколько заработал», «мои облигации» или «до 31.12.2026».\n"
    "Новый отчёт: пришли XLSX с подписью /invest_import. Налоги не считаем."
)
MENU = {"inline_keyboard": [
    [{"text": "Сколько заработал", "callback_data": "investcmd:pnl"}],
    [{"text": "Мои облигации", "callback_data": "investcmd:portfolio"}],
    [{"text": "Изменение за день", "callback_data": "investcmd:portfolio_day"}],
    [{"text": "До погашения", "callback_data": "investcmd:maturity"},
     {"text": "Ближайшие выплаты", "callback_data": "investcmd:payments"}],
    [{"text": "До какой даты?", "callback_data": "investcmd:forecast"}],
]}


def command_text(text):
    """Plain Russian shortcuts, without intercepting valid expense entries."""
    normalized = text.strip().casefold()
    aliases = {"облигации": "/invest", "мои облигации": "/portfolio", "сколько заработал": "/pnl",
               "прибыль по облигациям": "/pnl", "до погашения": "/maturity", "ближайшие выплаты": "/payments",
               "сводка портфеля": "/portfolio_day", "изменение портфеля": "/portfolio_day"}
    if normalized in aliases:
        return aliases[normalized]
    match = re.fullmatch(r"(?:до|прогноз)\s+(\d{1,2}\.\d{1,2}\.\d{4})", normalized)
    if match:
        return "/forecast " + match[1]
    parts = text.split(maxsplit=1)
    if parts and parts[0].lower() in COMMANDS:
        return parts[0].lower() + (" " + parts[1] if len(parts) > 1 else "")
    return None


def init_db(connection):
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS investment_reports (
            chat_id INTEGER NOT NULL, account TEXT NOT NULL, sha256 TEXT NOT NULL,
            start_day TEXT NOT NULL, end_day TEXT NOT NULL, data TEXT NOT NULL,
            PRIMARY KEY(chat_id, sha256)
        );
        CREATE TABLE IF NOT EXISTS investment_market_cache (
            cache_key TEXT PRIMARY KEY, fetched_at REAL NOT NULL, data TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS investment_daily_snapshots (
            chat_id INTEGER NOT NULL, day TEXT NOT NULL, data TEXT NOT NULL,
            PRIMARY KEY(chat_id, day)
        );
        CREATE TABLE IF NOT EXISTS investment_daily_history (
            chat_id INTEGER NOT NULL, day TEXT NOT NULL, data TEXT NOT NULL,
            PRIMARY KEY(chat_id, day)
        );
    """)


def dec(value):
    try:
        result = Decimal(str(value).replace(" ", "").replace(",", "."))
    except (InvalidOperation, ValueError) as error:
        raise ValueError("Некорректное число в брокерском отчёте.") from error
    if not result.is_finite():
        raise ValueError("Некорректное число в брокерском отчёте.")
    return result


def rub(value):
    if value is None:
        return "нет данных"
    return f"{dec(value).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,.2f}".replace(",", " ") + " ₽"


def clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def day(value):
    if isinstance(value, (date, datetime)):
        return value.date() if isinstance(value, datetime) else value
    text = clean(value).split(" ")[0]
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    raise ValueError("Дата должна быть в формате 31.12.2026.")


def table(data, name):
    block = data.get(name, {})
    return [dict(zip(block.get("columns", []), row)) for row in block.get("data", [])]


def parse_alfa(content):
    """Read only. Keep normalized financial rows, never the client's name."""
    if len(content) > MAX_FILE:
        raise ValueError("XLSX слишком большой (максимум 10 МБ).")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            if sum(info.file_size for info in archive.infolist()) > 60_000_000:
                raise ValueError("Распакованный XLSX слишком большой.")
        import openpyxl
        workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except (zipfile.BadZipFile, KeyError, OSError) as error:
        raise ValueError("Не удалось прочитать XLSX.") from error
    try:
        sheets = {s.title.strip(): list(s.iter_rows(values_only=True)) for s in workbook}
        for name, sheet_rows in sheets.items():
            width = max((len(r) for r in sheet_rows), default=0)
            sheets[name] = [r + (None,) * (width - len(r)) for r in sheet_rows]
    finally:
        workbook.close()
    required = {"Динамика позиций", "Завершенные сделки", "Движение ДС"}
    if not required.issubset(sheets):
        raise ValueError("Нужен брокерский XLSX Альфа-Инвестиций с динамикой позиций, сделками и движением ДС.")
    rows = sheets["Динамика позиций"]
    period = None
    account = None
    for row in rows[:12]:
        for value in row:
            text = clean(value)
            match = re.fullmatch(r"(\d{2}\.\d{2}\.\d{4})\s*-\s*(\d{2}\.\d{2}\.\d{4})", text)
            if match:
                period = (day(match[1]), day(match[2]))
            match = re.search(r"№\s*(\d+)\s+от", text)
            if match:
                account = match[1]
    if not period or not account or period[0] > period[1]:
        raise ValueError("В XLSX не найден корректный период или номер счёта.")
    header = next((i for i, r in enumerate(rows) if "Инструмент" in [clean(v) for v in r]), None)
    if header is None:
        raise ValueError("Не найдена таблица позиций.")
    labels = [clean(v) for v in rows[header]]
    name_col = labels.index("Актив ¹") if "Актив ¹" in labels else next((i for i, v in enumerate(labels) if v.startswith("Актив")), -1)
    qty_cols = [i for i, v in enumerate(labels) if v.startswith("кол-во")]
    val_cols = [i for i, v in enumerate(labels) if v.startswith("стоимость")]
    if name_col < 0 or len(qty_cols) < 2 or len(val_cols) < 2:
        raise ValueError("Неизвестный формат колонок позиций.")
    bonds = {}
    in_bonds = False
    for i in range(header + 1, len(rows)):
        row = rows[i]
        instrument = clean(row[labels.index("Инструмент")])
        if instrument:
            in_bonds = instrument == "Облигации"
        if not in_bonds or not isinstance(row[qty_cols[0]], (int, float)):
            continue
        isin = clean(rows[i + 1][name_col]) if i + 1 < len(rows) else ""
        if not ISIN_RE.fullmatch(isin):
            raise ValueError("У облигации не найден ISIN.")
        bonds[isin] = {
            "name": clean(row[name_col]), "opening": str(dec(row[qty_cols[0]])),
            "quantity": str(dec(row[qty_cols[1]])), "reported_value": str(dec(row[val_cols[1]])),
        }
        if dec(bonds[isin]["opening"]) < 0 or dec(bonds[isin]["quantity"]) < 0:
            raise ValueError("Короткие позиции по облигациям пока не поддерживаются.")
    trades = []
    trade_rows = sheets["Завершенные сделки"]
    h = next((r for r in trade_rows if any(clean(v) == "№ сделки" for v in r)), None)
    if h is None:
        raise ValueError("Не найден заголовок сделок.")
    def col(prefix):
        return next((i for i, v in enumerate(h) if clean(v).startswith(prefix)), None)
    columns = {k: col(v) for k, v in {
        "id": "№ сделки", "date": "Дата заключен", "settled": "Дата расчетов",
        "isin": "ISIN", "qty": "Количество актива", "amount": "Сумма сделки",
        "nkd": "В т.ч. НКД", "fee": "Комиссия банка", "currency": "Валюта расчетов",
        "comment": "Коммент.", "name": "Актив",
    }.items()}
    if any(columns[k] is None for k in ("id", "date", "settled", "isin", "qty", "amount", "nkd", "fee", "currency")):
        raise ValueError("Неизвестный формат колонок сделок.")
    for row in trade_rows:
        isin = clean(row[columns["isin"]])
        comment = clean(row[columns["comment"]]) if columns["comment"] is not None else ""
        if "репо" in comment.lower():
            continue
        if isin not in bonds:
            name = clean(row[columns['name']]) if columns['name'] is not None else ''
            if not ISIN_RE.fullmatch(isin) or not re.search(r'\bБО\b|облигац|\bОФЗ\b', name, re.IGNORECASE):
                continue
            bonds[isin] = {'name': name, 'opening': '0', 'quantity': '0', 'reported_value': '0', 'trade_only': True}
        if clean(row[columns["currency"]]) not in ("RUB", "SUR"):
            raise ValueError("Пока поддерживаются только рублёвые облигации.")
        trades.append({
            "id": clean(row[columns["id"]]).split()[0], "isin": isin,
            "date": day(row[columns["date"]]).isoformat(),
            "settled": day(row[columns["settled"]]).isoformat(),
            "quantity": str(dec(row[columns["qty"]])),
            "amount": str(abs(dec(row[columns["amount"]]))),
            "nkd": str(dec(row[columns["nkd"]] or 0)),
            "fee": str(dec(row[columns["fee"]] or 0)),
        })
    if not bonds:
        raise ValueError("В отчёте не найдено облигаций.")
    cash = []
    cash_rows = sheets["Движение ДС"]
    h = next((r for r in cash_rows if "Наименование операции" in [clean(v) for v in r]), None)
    if h is None:
        raise ValueError("Не найдена таблица движения денег.")
    labels = [clean(v) for v in h]
    comment_col = labels.index("Комментарий")
    # Alfa has a day in the first Date column and a timestamp in the next populated one.
    operation_col = labels.index("Наименование операции")
    rub_col = next((i for r in cash_rows for i, v in enumerate(r) if clean(v) == "RUB" and i > comment_col), None)
    if rub_col is None:
        raise ValueError("В отчёте нет рублёвого движения денег.")
    occurrences = Counter()
    for row in cash_rows:
        comment = clean(row[comment_col])
        low = comment.lower()
        kind = "coupon" if "купон" in low else "principal" if ("амортизац" in low or "погашение облигац" in low or "погашение номинал" in low) else None
        if not kind:
            continue
        timestamps = [v for v in row[:operation_col] if isinstance(v, datetime) or re.search(r"\d{2}:\d{2}", clean(v))]
        timestamp = timestamps[-1] if timestamps else next((v for v in row[:operation_col] if v), None)
        cash_day = day(timestamp)
        amount = dec(row[rub_col])
        if amount <= 0:
            raise ValueError("Отрицательная/нулевая выплата: нужна ручная проверка отчёта.")
        reg = re.search(r"\b(?:4B|4-|[1-4]\d)[A-Z0-9-]+", comment, re.IGNORECASE)
        regcode = reg[0].upper() if reg else ""
        key = f"{clean(timestamp)}|{kind}|{regcode or comment}|{amount}"
        occurrences[key] += 1
        cash.append({"id": key + f"|{occurrences[key]}", "date": cash_day.isoformat(),
                     "kind": kind, "regnumber": regcode, "comment": comment, "amount": str(amount)})
    warnings = []
    # Do not silently invent acquisition costs for non-trading transfers/corporate actions.
    for row in sheets.get("Неторговые операции", [])[15:]:
        # Redemption cancellation is reconciled against cash and MOEX in results.
        if 'аннулирование выпуска' in ' '.join(clean(v) for v in row).casefold():
            continue
        if any(isin in " ".join(clean(v) for v in row) or bond["name"] in " ".join(clean(v) for v in row)
               for isin, bond in bonds.items()):
            warnings.append("Есть неторговые операции с облигациями: себестоимость требует проверки.")
            break
    return {"account": account, "start": period[0].isoformat(), "end": period[1].isoformat(),
            "bonds": bonds, "trades": trades, "cash": cash, "warnings": warnings}


def import_report(connection, chat_id, content):
    digest = hashlib.sha256(content).hexdigest()
    report = parse_alfa(content)
    return report if save_report(connection, chat_id, digest, report) else None


def save_report(connection, chat_id, digest, report):
    same = connection.execute("SELECT data FROM investment_reports WHERE chat_id=? AND sha256=?", (chat_id, digest)).fetchone()
    if same and json.loads(same[0]) == report:
        return False
    existing = connection.execute("SELECT data FROM investment_reports WHERE chat_id=?", (chat_id,)).fetchall()
    if any(json.loads(r[0])["account"] != report["account"] for r in existing):
        raise ValueError("Уже импортирован другой брокерский счёт. Пока один счёт на пользователя.")
    # Overlaps are safe only when the same transaction has identical financial fields.
    old_trades = {t["id"]: t for r in existing for t in json.loads(r[0])["trades"]}
    for trade in report["trades"]:
        if trade["id"] in old_trades and trade != old_trades[trade["id"]]:
            raise ValueError("Сделка отличается от ранее импортированной. Отчёт не сохранён.")
    if same:
        previous = json.loads(same[0])
        if not {t['id'] for t in previous['trades']}.issubset({t['id'] for t in report['trades']}) or previous['cash'] != report['cash']:
            raise ValueError('Обновлённый импорт теряет операции. Отчёт не сохранён.')
    connection.execute("INSERT INTO investment_reports VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(chat_id,sha256) DO UPDATE SET data=excluded.data",
                       (chat_id, report["account"], digest, report["start"], report["end"], json.dumps(report, ensure_ascii=False)))
    return True


def bootstrap(connection, encoded, allowed_users):
    """Optional private, one-time deployment import; never store reports in Git."""
    if not encoded:
        return False
    payload = json.loads(gzip.decompress(base64.b64decode(encoded)))
    chat_id = int(payload["chat_id"])
    if chat_id not in allowed_users:
        raise ValueError("Bootstrap user is not on the allowlist")
    imported = save_report(connection, chat_id, payload["sha256"], payload["report"])
    for cached in payload.get("market_cache", []):
        connection.execute("""INSERT INTO investment_market_cache VALUES (?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET fetched_at=excluded.fetched_at, data=excluded.data
            WHERE excluded.fetched_at > investment_market_cache.fetched_at""", cached)
    return imported


def portfolio(connection, chat_id):
    reports = [json.loads(r[0]) for r in connection.execute(
        "SELECT data FROM investment_reports WHERE chat_id=? ORDER BY start_day, end_day", (chat_id,))]
    if not reports:
        return None
    latest = max(reports, key=lambda r: (r["end"], -date.fromisoformat(r["start"]).toordinal()))
    result = {"asof": date.fromisoformat(latest["end"]), "start": reports[0]["start"], "bonds": {}, "cash": {}, "warnings": []}
    frontier = reports[0]["end"]
    for report in reports:
        if date.fromisoformat(report["start"]) > date.fromisoformat(frontier) + timedelta(days=1):
            result["warnings"].append("Между отчётами есть пропуск: полная прибыль недоступна.")
        frontier = max(frontier, report["end"])
        result["warnings"].extend(report["warnings"])
        result["cash"].update({c["id"]: c for c in report["cash"] if c["date"] <= latest["end"]})
        for isin, record in report["bonds"].items():
            if isin not in result["bonds"]:
                result["bonds"][isin] = dict(record, trades={})
            result["bonds"][isin]["trades"].update({t["id"]: t for t in report["trades"] if t["isin"] == isin and t["settled"] <= latest["end"]})
    for isin, bond in result["bonds"].items():
        bond["quantity"] = latest["bonds"].get(isin, {}).get("quantity", "0")
        bond["reported_value"] = latest["bonds"].get(isin, {}).get("reported_value", "0")
        expected = dec(bond["opening"]) + sum((dec(t["quantity"]) for t in bond["trades"].values()), Decimal(0))
        bond['expected_quantity'] = expected
        bond['history_complete'] = dec(bond['opening']) == 0 and expected >= 0 and not result['warnings']
        bond['asof'] = result['asof']
        bond["complete"] = dec(bond["opening"]) == 0 and expected == dec(bond["quantity"]) and not result["warnings"]
    result["warnings"] = list(dict.fromkeys(result["warnings"]))
    return result


def fetch_json(path):
    request = urllib.request.Request(ISS + path, headers={"User-Agent": "finance-tracker/1.0"})
    with urllib.request.urlopen(request, timeout=8) as response:
        return json.loads(response.read(5_000_000))


class Market:
    """Shared public cache, never shared user portfolios. Failures retain labelled cache."""
    def __init__(self, db_factory, fetch=fetch_json):
        self.db_factory = db_factory
        self.fetch = fetch

    def cached(self, key, path, ttl):
        with self.db_factory() as conn:
            row = conn.execute("SELECT fetched_at,data FROM investment_market_cache WHERE cache_key=?", (key,)).fetchone()
        if row and time.time() - row[0] < ttl:
            return json.loads(row[1]), row[0], False
        try:
            data = self.fetch(path)
            if not isinstance(data, dict) or not data:
                raise ValueError("Пустой ответ MOEX")
            fetched = time.time()
            with self.db_factory() as conn:
                conn.execute("INSERT OR REPLACE INTO investment_market_cache VALUES (?, ?, ?)", (key, fetched, json.dumps(data)))
            return data, fetched, False
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            if row:
                return json.loads(row[1]), row[0], True
            return {}, None, True

    def security(self, isin):
        if not ISIN_RE.fullmatch(isin):
            raise ValueError("Некорректный ISIN")
        quoted = urllib.parse.quote(isin, safe="")
        data, fetched, stale = self.cached("quote:" + isin,
            f"/engines/stock/markets/bonds/securities/{quoted}.json?iss.meta=off", 900)
        # Primary board from description, not an arbitrary negotiated-trades quote.
        description, _, desc_stale = self.cached("description:" + isin,
            f"/securities/{quoted}.json?iss.meta=off", 86400)
        boards = table(description, "boards")
        primary = next((r.get("boardid") for r in boards if r.get("is_primary") == 1), None)
        securities = table(data, "securities")
        security = next((s for s in securities if s.get("BOARDID") == primary), None)
        if security is None:
            security = next((s for s in securities if str(s.get("BOARDID", "")).startswith("TQ")), None)
        security = security or {}
        md = next((r for r in table(data, "marketdata") if r.get("BOARDID") == security.get("BOARDID")), {})
        last = md.get("LAST")
        price = last if last is not None else security.get("PREVPRICE")
        stamp = md.get("SYSTIME") if last is not None else security.get("PREVDATE")
        schedule, sfetched, sstale = self.cached("schedule:" + isin,
            f"/securities/{quoted}/bondization.json?iss.meta=off&limit=unlimited", 86400)
        desc = {r["name"]: r["value"] for r in table(description, "description")}
        return {"security": security, "price": price, "quote_at": stamp, "fetched": fetched,
                "stale": stale or desc_stale, "schedule_stale": sstale, "schedule_at": sfetched,
                "coupons": table(schedule, "coupons"), "amortizations": table(schedule, "amortizations"),
                "offers": table(schedule, "offers"), "description": desc}

    def history(self, isin, start, end):
        if not ISIN_RE.fullmatch(isin) or start > end:
            raise ValueError('Некорректный запрос истории MOEX')
        rows = []
        for offset in range(0, 10000, 100):
            path = f'/history/engines/stock/markets/bonds/securities/{isin}.json?from={start}&till={end}&start={offset}&iss.meta=off'
            data, _, _ = self.cached(f'history:{isin}:{start}:{end}:{offset}', path, 86400)
            if not data:
                raise ValueError('MOEX не вернул архив котировок')
            page = table(data, 'history')
            rows.extend(page)
            cursor = table(data, 'history.cursor')
            if cursor:
                if cursor[0]['INDEX'] + cursor[0]['PAGESIZE'] >= cursor[0]['TOTAL']:
                    return rows
            elif len(page) < 100:
                return rows
            if not page:
                raise ValueError('Архив MOEX загружен не полностью')
        raise ValueError('Архив MOEX слишком большой')

    def all(self, isins):
        with ThreadPoolExecutor(max_workers=4) as pool:
            return dict(zip(isins, pool.map(self.security, isins)))


def cash_by_bond(state, markets):
    by_reg = {str(m["security"].get("REGNUMBER") or m["description"].get("REGNUMBER") or "").upper(): isin
              for isin, m in markets.items()}
    payments = defaultdict(list)
    unmatched = []
    for cash in state["cash"].values():
        isin = by_reg.get(cash["regnumber"]) if cash["regnumber"] else None
        if not isin:
            matches = [i for i, b in state["bonds"].items() if b["name"].casefold() in cash["comment"].casefold()]
            isin = matches[0] if len(matches) == 1 else None
        if isin:
            payments[isin].append(cash)
        else:
            unmatched.append(cash)
    return payments, unmatched


def results(bond, market, payments):
    """FIFO cost allocation. Missing opening costs disable only profit, not receipts."""
    lots = []
    realized = Decimal(0)
    net_trades = Decimal(0)
    fees = Decimal(0)
    complete = bond["complete"]
    for t in sorted(bond["trades"].values(), key=lambda t: (t["settled"], t["date"], t["id"])):
        qty, amount, fee = dec(t["quantity"]), dec(t["amount"]), dec(t["fee"])
        fees += fee
        if qty > 0:
            lots.append([qty, (amount + fee) / qty])
            net_trades -= amount + fee  # amount already includes NKD
        elif qty < 0:
            net_trades += amount - fee
            remaining, cost = -qty, Decimal(0)
            while lots and remaining > 0:
                taken = min(remaining, lots[0][0])
                cost += taken * lots[0][1]
                remaining -= taken
                lots[0][0] -= taken
                if lots[0][0] == 0:
                    lots.pop(0)
            if remaining:
                complete = False
            realized += amount - fee - cost
    coupons = sum((dec(c["amount"]) for c in payments if c["kind"] == "coupon"), Decimal(0))
    principal = sum((dec(c["amount"]) for c in payments if c["kind"] == "principal"), Decimal(0))
    # Fully redeemed positions disappear from Alfa's ending positions. Verify the
    # missing units against the exchange's final redemption, not a guessed sale.
    if not complete and bond.get('history_complete') and dec(bond['quantity']) == 0:
        maturity = market['security'].get('MATDATE') or market['description'].get('MATDATE')
        if maturity and maturity != '0000-00-00' and day(maturity) <= bond['asof']:
            final = [r for r in market['amortizations'] if r.get('amortdate') and day(r['amortdate']) == day(maturity)]
            paid = sum((dec(p['amount']) for p in payments if p['kind'] == 'principal' and day(p['date']) >= day(maturity)), Decimal(0))
            face = sum((payment_value(r) or Decimal(0) for r in final), Decimal(0))
            complete = bool(face > 0 and paid == bond['expected_quantity'] * face)
    # Corporate-action cost allocation is not guessed. Full profit via cash+valuation
    # remains unavailable until the quantity/history reconciliation succeeds.
    allocation_complete = complete and not principal
    qty = dec(bond["quantity"])
    sec = market["security"]
    value = None
    if qty == 0:
        value = Decimal(0)
    elif market["price"] is not None and sec.get("FACEVALUE") is not None and sec.get("ACCRUEDINT") is not None and sec.get("FACEUNIT") in ("RUB", "SUR"):
        value = qty * (dec(market["price"]) / 100 * dec(sec["FACEVALUE"]) + dec(sec["ACCRUEDINT"]))
    cost = sum((q * unit for q, unit in lots), Decimal(0)) if allocation_complete else None
    total = net_trades + coupons + principal + value if complete and value is not None else None
    clean_trades = sum(((Decimal(-1) if dec(t['quantity']) > 0 else Decimal(1)) * (dec(t['amount']) - dec(t['nkd'])) for t in bond['trades'].values()), Decimal(0))
    accrued_trades = sum(((Decimal(-1) if dec(t['quantity']) > 0 else Decimal(1)) * dec(t['nkd']) for t in bond['trades'].values()), Decimal(0))
    current_accrued = qty * dec(sec.get('ACCRUEDINT') or 0)
    price_result = clean_trades + principal + value - current_accrued if total is not None else None
    accrued_result = accrued_trades + current_accrued if total is not None else None
    return {"value": value, "cost": cost, "coupons": coupons, "principal": principal, "fees": fees,
            "realized": realized if allocation_complete else None,
            "unrealized": value - cost if allocation_complete and value is not None else None,
            "total": total, "complete": complete, "net_trades": net_trades,
            'price_result': price_result, 'accrued_result': accrued_result}


def payment_value(row):
    if row.get("faceunit") not in ("RUB", "SUR"):
        return None
    value = row.get("value_rub")
    if value is None:
        value = row.get("value")
    return dec(value) if value is not None else None


def accrual(coupons, at):
    for row in coupons:
        if not row.get("startdate") or not row.get("coupondate"):
            continue
        start, end = day(row["startdate"]), day(row["coupondate"])
        if start <= at < end:
            value = payment_value(row)
            return value * Decimal((at - start).days) / Decimal((end - start).days) if value is not None and end > start else None
    return None


def forecast(bond, market, asof, target):
    qty = dec(bond["quantity"])
    entries, unknown = [], []
    unknown_payments = []
    for kind, rows, datekey in (("coupon", market["coupons"], "coupondate"), ("principal", market["amortizations"], "amortdate")):
        for row in rows:
            if not row.get(datekey):
                continue
            when = day(row[datekey])
            if asof < when <= target:
                value = payment_value(row)
                if value is None:
                    unknown_payments.append((when, kind))
                else:
                    entries.append({"day": when, "kind": kind, "amount": value * qty})
    coupons = sum((e["amount"] for e in entries if e["kind"] == "coupon"), Decimal(0))
    principal = sum((e["amount"] for e in entries if e["kind"] == "principal"), Decimal(0))
    maturity_value = market["security"].get("MATDATE") or market["description"].get("MATDATE")
    maturity = day(maturity_value) if maturity_value and maturity_value != "0000-00-00" else None
    if unknown_payments:
        first = min(p[0] for p in unknown_payments)
        unknown.append(f"Размер {len(unknown_payments)} выплат не определён, первая {first:%d.%m.%Y}")
    if qty and not market["coupons"]:
        unknown.append("Нет календаря купонов")
    if qty and (not maturity or not market["amortizations"]):
        unknown.append("Нет полного графика возврата номинала")
    elif qty and maturity and max(day(r["amortdate"]) for r in market["amortizations"] if r.get("amortdate")) < maturity:
        unknown.append("График возврата номинала не покрывает погашение")
    if qty and maturity and market["coupons"] and max(day(r["coupondate"]) for r in market["coupons"] if r.get("coupondate")) < min(target, maturity):
        unknown.append("Календарь купонов не покрывает весь период")
    if market["schedule_stale"]:
        unknown.append("Календарь не обновлён: используется сохранённая версия")
    if any(str(market["description"].get(k, "0")) == "1" for k in ("HASDEFAULT", "HASTECHNICALDEFAULT")):
        unknown.append("MOEX отмечает дефолт/технический дефолт: выплаты под риском")
    offer_values = [market["security"].get(k) for k in ("OFFERDATE", "BUYBACKDATE", "CALLOPTIONDATE", "PUTOPTIONDATE")]
    offer_values += [r.get("offerdate") for r in market["offers"]]
    if any(v and v != "0000-00-00" and asof < day(v) <= target for v in offer_values):
        unknown.append("В периоде есть оферта/call/put: прогноз после неё условный")
    end_accrual = Decimal(0) if maturity and target >= maturity else accrual(market["coupons"], target)
    start_accrual = accrual(market["coupons"], asof)
    earned = coupons + qty * (end_accrual - start_accrual) if start_accrual is not None and end_accrual is not None and not unknown else None
    return {"entries": entries, "coupons": coupons, "principal": principal, "unknown": unknown,
            "earned": earned, "maturity": maturity}


def ledger_fingerprint(state):
    ledger = {'bonds': state['bonds'], 'cash': state['cash'], 'asof': state['asof']}
    return hashlib.sha256(json.dumps(ledger, sort_keys=True, default=str).encode()).hexdigest()


def ledger_totals(state, through):
    trades = [t for b in state['bonds'].values() for t in b['trades'].values() if day(t['settled']) <= through]
    payouts = [p for p in state['cash'].values() if day(p['date']) <= through]
    def signed_trade(t, field):
        return (Decimal(-1) if dec(t['quantity']) > 0 else Decimal(1)) * dec(t[field])
    return {
        'net_clean_trades': str(sum((signed_trade(t, 'amount') - signed_trade(t, 'nkd') for t in trades), Decimal(0))),
        'net_nkd_trades': str(sum((signed_trade(t, 'nkd') for t in trades), Decimal(0))),
        'fees': str(sum((dec(t['fee']) for t in trades), Decimal(0))),
        'coupons': str(sum((dec(p['amount']) for p in payouts if p['kind'] == 'coupon'), Decimal(0))),
        'principal': str(sum((dec(p['amount']) for p in payouts if p['kind'] == 'principal'), Decimal(0))),
    }


def reconstruct_month(state, markets, histories, month_start, today):
    """End-of-session estimates, never fabricated intraday observations."""
    last = min(today - timedelta(days=1), state['asof'], (month_start + timedelta(days=32)).replace(day=1) - timedelta(days=1))
    payments, unmatched = cash_by_bond(state, markets)
    fingerprint = ledger_fingerprint(state)
    rows = []
    on = month_start
    while on <= last:
        errors, positions = [], {}
        if on < day(state['start']):
            errors.append('Дата раньше начала загруженной истории')
        price, nkd = Decimal(0), Decimal(0)
        for isin, bond in state['bonds'].items():
            if not bond.get('history_complete'):
                errors.append(isin + ': неполная история количества')
                continue
            qty = dec(bond['opening']) + sum((dec(t['quantity']) for t in bond['trades'].values() if day(t['settled']) <= on), Decimal(0))
            m = markets[isin]
            if bond['expected_quantity'] != dec(bond['quantity']) and not results(bond, m, payments[isin])['complete']:
                errors.append(isin + ': остаток не сверяется с отчётом')
                continue
            maturity = m['security'].get('MATDATE') or m['description'].get('MATDATE')
            if maturity and maturity != '0000-00-00' and day(maturity) <= on and qty:
                if dec(bond['quantity']) == 0 and results(bond, m, payments[isin])['complete']:
                    qty = Decimal(0)
                else:
                    errors.append(isin + ': погашение не сверено')
                    continue
            if qty < 0:
                errors.append(isin + ': отрицательный остаток')
                continue
            if qty == 0:
                continue
            board = m['security'].get('BOARDID')
            candidates = [r for r in histories.get(isin, []) if r.get('TRADEDATE') and day(r['TRADEDATE']) <= on
                          and ((r.get('BOARDID') == board) if board else str(r.get('BOARDID', '')).startswith('TQ'))
                          and r.get('CLOSE') is not None and r.get('ACCINT') is not None and r.get('FACEVALUE') is not None
                          and r.get('FACEUNIT') in ('RUB', 'SUR')]
            if not candidates:
                errors.append(isin + ': нет архивной цены/НКД/номинала')
                continue
            quote = max(candidates, key=lambda r: (r['TRADEDATE'], r.get('TRADINGSESSION') == 3, r.get('NUMTRADES') or 0))
            clean = qty * dec(quote['CLOSE']) / 100 * dec(quote['FACEVALUE'])
            accrued = qty * dec(quote['ACCINT'])
            price += clean; nkd += accrued
            positions[isin] = {'quantity': str(qty), 'price': str(clean), 'nkd': str(accrued),
                               'quote_day': quote['TRADEDATE'], 'board': quote['BOARDID'], 'facevalue': str(quote['FACEVALUE'])}
        if unmatched:
            errors.append('Есть несопоставленные выплаты')
        data = {'source': 'moex_history_close', 'day': on.isoformat(), 'ledger': fingerprint, 'asof': str(state['asof']),
                'observed_at': on.isoformat() + 'T23:59:59+03:00', 'positions': positions,
                'complete': not errors, 'errors': errors, 'value': str(price + nkd) if not errors else None,
                'price': str(price) if not errors else None, 'nkd': str(nkd) if not errors else None,
                'pending': [], 'quote_at': sorted({p['quote_day'] for p in positions.values()}),
                'total': None, **ledger_totals(state, on)}
        data['carried_quotes'] = any(p['quote_day'] != on.isoformat() for p in positions.values())
        rows.append(data)
        on += timedelta(days=1)
    return rows


def previous_snapshot(connection, chat_id, today, current, include_today=False):
    # A reconstructed close is preferable to a morning observation for "today".
    archived = connection.execute('SELECT data FROM investment_daily_history WHERE chat_id=? AND day=?',
                                  (chat_id, (today - timedelta(days=1)).isoformat())).fetchone()
    if archived:
        data = json.loads(archived[0])
        if data.get('complete') and data['ledger'] == current['ledger']:
            return data
    row = connection.execute('''SELECT data FROM investment_daily_snapshots WHERE chat_id=? AND day<=?
        ORDER BY CASE WHEN day<? THEN 0 ELSE 1 END, day DESC LIMIT 1''',
        (chat_id, (today if include_today else today-timedelta(days=1)).isoformat(), today.isoformat())).fetchone()
    return json.loads(row[0]) if row else None


def daily_snapshot(state, markets, observed_at):
    """Persist comparable valuations; imported ledger changes reset the baseline."""
    cash, unmatched = cash_by_bond(state, markets)
    calculated = [results(b, markets[i], cash[i]) for i, b in state['bonds'].items()]
    if any(r['value'] is None for r in calculated):
        raise ValueError('Не удалось получить цены всех бумаг. Изменение портфеля не рассчитано.')
    if any(m['stale'] for i, m in markets.items() if dec(state['bonds'][i]['quantity']) > 0):
        raise ValueError('MOEX не обновил цены. Чтобы не показать ложное изменение, сегодня сравнение пропущено.')
    value = sum((r['value'] for r in calculated), Decimal(0))
    nkd = sum((dec(b['quantity']) * dec(markets[i]['security'].get('ACCRUEDINT') or 0) for i, b in state['bonds'].items()), Decimal(0))
    fingerprint = ledger_fingerprint(state)
    total = sum((r['total'] for r in calculated), Decimal(0)) if not unmatched and all(r['total'] is not None for r in calculated) else None
    pending = []
    observed_day = day(observed_at[:10])
    for isin, bond in state['bonds'].items():
        if dec(bond['quantity']) <= 0:
            continue
        m = markets[isin]
        for rows, key in ((m['coupons'], 'coupondate'), (m['amortizations'], 'amortdate')):
            for event in rows:
                if event.get(key) and state['asof'] < day(event[key]) <= observed_day:
                    pending.append((event[key], bond['name']))
    return {'observed_at': observed_at, 'ledger': fingerprint, 'asof': str(state['asof']),
            'value': str(value), 'price': str(value - nkd), 'nkd': str(nkd),
            'total': str(total) if total is not None else None,
            'coupons': str(sum((r['coupons'] for r in calculated), Decimal(0))),
            'fees': str(sum((r['fees'] for r in calculated), Decimal(0))),
            'principal': str(sum((r['principal'] for r in calculated), Decimal(0))),
            'pending': sorted(set(pending)),
            'quote_at': sorted({m['quote_at'] for i, m in markets.items() if m['quote_at'] and dec(state['bonds'][i]['quantity']) > 0}),
            **ledger_totals(state, state['asof'])}


def daily_change_text(current, previous):
    def money(value):
        return rub(value).replace('.00 ₽', ' ₽')

    def signed(value):
        return ('+' if dec(value) > 0 else '') + money(value)

    at = datetime.fromisoformat(current['observed_at'])
    months = ('января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря')
    comparable = previous is not None and previous['ledger'] == current['ledger']
    lines = [f'{at.day} {months[at.month - 1]} · портфель',
             f"Было {money(previous['value']) if comparable else '—'} → стало {money(current['value'])}"]
    def change(key):
        return dec(current[key]) - dec(previous[key]) if comparable and key in previous and key in current else Decimal(0)
    price_change = change('price') + change('net_clean_trades') + change('principal')
    nkd_change = change('nkd') + change('net_nkd_trades')
    coupons_change, fees_change = change('coupons'), change('fees')
    if comparable:
        lines.append(f"Цена: {signed(price_change)} · НКД: {signed(nkd_change)}")
    else:
        lines.append('Цена: — · НКД: — (нет сравнения)')
    if comparable and day(current['asof']) >= at.date():
        lines.append(f'Купоны: {signed(coupons_change)} · комиссии: {signed(-fees_change)}')
    else:
        lines.append(f"Купоны/комиссии: нет новых данных после {day(current['asof']):%d.%m}")
    pending = [e for e in current['pending'] if not previous or e[0] > previous['observed_at'][:10]]
    if not comparable:
        lines.append('Итого: — · обновлён отчёт, новая точка отсчёта' if previous else 'Итого: — · первая оценка, сравнение со следующей сводки')
    elif pending:
        lines.append('Итого: — · выплата по графику не подтверждена; обнови XLSX')
    else:
        start = datetime.fromisoformat(previous['observed_at'])
        period = f"с закрытия {start:%d.%m}" if previous.get('source') == 'moex_history_close' else ('за день' if (at.date() - start.date()).days == 1 else f'с {start:%d.%m %H:%M}')
        qualifier = '' if day(current['asof']) >= at.date() else ' (только цена и НКД)'
        lines.append(f"Итого {period}: {signed(price_change + nkd_change + coupons_change - fees_change)}{qualifier}")
    return '\n'.join(lines)


def pnl_text(state, markets, cash, unmatched):
    """Same cash-flow PNL as /profit, with a compact and explicit coverage scope."""
    calculated, missing = [], []
    for isin, bond in state["bonds"].items():
        result = results(bond, markets[isin], cash[isin])
        if result["total"] is None:
            missing.append(bond["name"])
        else:
            calculated.append(result)
    complete = not missing and not unmatched
    total = sum((r["total"] for r in calculated), Decimal(0)) if calculated else None
    lines = [f"Твой результат: {rub(total) if complete else 'пока не могу посчитать всё'}"]
    if not complete:
        lines.append(f"По {len(calculated)} из {len(state['bonds'])} облигаций: {rub(total)}")
    if calculated:
        coupons = sum((r["coupons"] for r in calculated), Decimal(0))
        realized = sum((r["realized"] for r in calculated), Decimal(0)) if all(r["realized"] is not None for r in calculated) else None
        unrealized = sum((r["unrealized"] for r in calculated), Decimal(0)) if all(r["unrealized"] is not None for r in calculated) else None
        fees = sum((r["fees"] for r in calculated), Decimal(0))
        if not complete:
            lines.append("Из чего состоит эта сумма:")
        price = sum((r['price_result'] for r in calculated), Decimal(0))
        accrued = sum((r['accrued_result'] for r in calculated), Decimal(0))
        lines.extend([f"Изменение цены бумаг: {rub(price)}", f"Получено купонов: {rub(coupons)}",
                      f"Накопленные проценты (с учётом покупки и продажи): {rub(accrued)}",
                      f"Потеряно на комиссиях: {rub(fees)}"])
        lines.append('Итог = изменение цены + купоны + накопленные проценты − комиссии.')
    if missing:
        lines.append("Не хватает истории или цены: " + ", ".join(missing))
    if unmatched:
        lines.append(f"Не сопоставлено выплат: {len(unmatched)}. Полный PNL недоступен.")
    quote_dates = sorted({m["quote_at"] for isin, m in markets.items() if m["quote_at"] and dec(state["bonds"][isin]["quantity"]) > 0})
    if quote_dates:
        lines.append("Котировки: " + (quote_dates[0] if len(quote_dates) == 1 else f"{quote_dates[0]} — {quote_dates[-1]}"))
    if any(m["stale"] for m in markets.values()):
        lines.append("MOEX не обновился: часть данных сохранённая.")
    lines.append(f"Сделки и купоны по отчёту на {state['asof']:%d.%m.%Y}. Без налогов.")
    lines.append("Накопленные проценты учтены. Цена продажи может отличаться.")
    return "\n".join(lines)


def simple_report(state, markets, cash, unmatched, command, target, today):
    """One main answer, with per-bond detail kept behind a button."""
    if command == "/portfolio":
        values = [results(b, markets[i], cash[i])["value"] for i, b in state["bonds"].items()]
        total = sum(values, Decimal(0)) if all(v is not None for v in values) else None
        lines = [f"Твои облигации сейчас: {rub(total)}", ""]
        lines.extend(f"• {b['name']} — {dec(b['quantity']):g} шт., {rub(v)}" for b, v in zip(state["bonds"].values(), values))
        keyboard = {"inline_keyboard": [[{"text": b["name"], "callback_data": "investbond:" + i}] for i, b in state["bonds"].items()]}
    else:
        forecasts, gains, entries, issues = [], [], [], []
        for isin, bond in state["bonds"].items():
            if dec(bond["quantity"]) == 0:
                continue
            m = markets[isin]
            maturity = m["security"].get("MATDATE") or m["description"].get("MATDATE")
            end = day(maturity) if command == "/maturity" and maturity and maturity != "0000-00-00" else target
            f = forecast(bond, m, state["asof"], end)
            forecasts.append(f)
            r = results(bond, m, cash[isin])
            gains.append(r["net_trades"] + r["coupons"] + r["principal"] + f["coupons"] + f["principal"] if r["complete"] and not f["unknown"] and f["maturity"] else None)
            if f["unknown"]:
                issues.append(bond["name"])
            entries.extend((e["day"], bond["name"], e["kind"], e["amount"]) for e in f["entries"] if e["day"] > today)
        coupons = sum((f["coupons"] for f in forecasts), Decimal(0))
        principal = sum((f["principal"] for f in forecasts), Decimal(0))
        if command == "/payments":
            lines = ["Ближайшие выплаты (90 дней)", ""]
            lines.extend(f"• {when:%d.%m}: {rub(value)} — {name}{' (возврат вложенных денег)' if kind == 'principal' else ''}" for when, name, kind, value in sorted(entries))
            if not entries:
                lines.append("Известных выплат пока нет.")
        elif command == "/forecast":
            earned = sum((f["earned"] for f in forecasts), Decimal(0)) if all(f["earned"] is not None for f in forecasts) else None
            lines = [f"До {target:%d.%m.%Y} получишь по известному графику: {rub(coupons + principal)}", "",
                     f"Купоны: {rub(coupons)}", f"Возврат вложенных денег: {rub(principal)}",
                     f"Купонный доход за этот период: {rub(earned)}",
                     "Будущую цену облигаций не угадываем."]
        else:
            complete = all(v is not None for v in gains) and not unmatched
            known = [v for v in gains if v is not None]
            total = sum(known, Decimal(0)) if known else None
            lines = [f"До погашения заработаешь: {rub(total) if complete else 'полная сумма пока неизвестна'}", "",
                     f"Ещё получишь купонов по известному графику: {rub(coupons)}",
                     f"Вернётся вложенных денег: {rub(principal)}"]
            if not complete:
                lines.append(f"Прибыль по {len(known)} из {len(gains)} выпусков с полными данными: {rub(total)}")
        if issues:
            lines.append("\nНе все будущие выплаты известны или есть оферта: " + ", ".join(issues))
        if command in ("/forecast", "/maturity"):
            lines.append(f"Считаем выплаты после отчёта от {state['asof']:%d.%m.%Y}; прошедшие даты ещё не подтверждены отчётом.")
        keyboard = {"inline_keyboard": [[{"text": "Подробнее по каждой облигации", "callback_data": f"investdetail:{command[1:]}:{target.isoformat()}"}]]} if command != "/payments" else MENU
    if any(m["stale"] or m["schedule_stale"] for m in markets.values()):
        lines.append("MOEX не обновился: показываю сохранённые данные.")
    if unmatched:
        lines.append("Не все прошлые выплаты сопоставлены с облигациями. Подробнее: /profit.")
    lines.append(f"Отчёт на {state['asof']:%d.%m.%Y}. Без налогов; выплаты по графику не гарантированы.")
    return "\n".join(lines), keyboard


def report_text(connection, chat_id, command, argument, today, market, detailed=False, observed_at=None):
    state = portfolio(connection, chat_id)
    if not state:
        return HELP, None
    if command == "/forecast":
        target = day(argument)
        if target <= state["asof"]:
            raise ValueError("Дата прогноза должна быть позже конца последнего отчёта.")
    else:
        target = today + timedelta(days=90)
    bonds = state["bonds"]
    if command == "/bond":
        matches = [i for i, b in bonds.items() if argument.casefold() in b["name"].casefold() or argument.upper() == i]
        if not argument or not matches:
            return "Напиши /bond название или ISIN. Выпуски: /portfolio", None
        if len(matches) > 1:
            return "Выбери выпуск:", {"inline_keyboard": [[{"text": bonds[i]["name"], "callback_data": "investbond:" + i}] for i in matches]}
        bonds = {matches[0]: bonds[matches[0]]}
    markets = market.all(list(state["bonds"]))
    if command == '/portfolio_day':
        current = daily_snapshot(state, markets, observed_at or today.isoformat() + 'T10:00:00')
        previous = previous_snapshot(connection, chat_id, today, current, include_today=True)
        if previous is None:
            connection.execute('INSERT OR IGNORE INTO investment_daily_snapshots VALUES (?,?,?)',
                               (chat_id, today.isoformat(), json.dumps(current, ensure_ascii=False)))
        return daily_change_text(current, previous), MENU
    cash, unmatched = cash_by_bond(state, markets)
    if command == "/pnl" or (command == "/profit" and not detailed):
        return pnl_text(state, markets, cash, unmatched), {"inline_keyboard": [[{"text": "Подробнее по каждой облигации", "callback_data": "investdetail:profit:"}]]}
    if not detailed and command in ("/portfolio", "/forecast", "/maturity", "/payments"):
        return simple_report(state, markets, cash, unmatched, command, target, today)
    lines = [f"Облигации · отчёт по {state['asof']:%d.%m.%Y}"]
    if state["asof"] < today:
        lines.append("Сделки и поступления после этой даты не подтверждены. Обнови XLSX после новых операций.")
    summary, forecasts, maturity_results, calendar_entries = [], [], [], []
    warnings = list(state["warnings"])
    for isin, bond in bonds.items():
        m = markets[isin]
        r = results(bond, m, cash[isin])
        summary.append(r)
        quantity = dec(bond["quantity"])
        if command in ("/portfolio", "/profit", "/bond"):
            lines.append(f"\n{bond['name']} · {quantity:g} шт.\nISIN: {isin}")
            if command in ("/portfolio", "/bond"):
                lines.append(f"Стоимость покупки оставшихся: {rub(r['cost'])}\nТекущая стоимость с НКД: {rub(r['value'])}")
            if command in ("/profit", "/bond"):
                lines.append(f"Изменение цены: {rub(r['price_result'])}\nКупоны: {rub(r['coupons'])}\nНакопленные проценты: {rub(r['accrued_result'])}\nКомиссии: {rub(r['fees'])}")
                lines.append(f"Купоны получены: {rub(r['coupons'])}\nРеализованный результат сделок: {rub(r['realized'])}\nИзменение стоимости с НКД: {rub(r['unrealized'])}\nОбщий результат: {rub(r['total'])}")
            lines.append(f"Котировка: {m['quote_at'] or 'недоступна'}" + (" (сохранённая, обновить не удалось)" if m["stale"] else ""))
            if m["security"].get("SETTLEDATE"):
                lines.append(f"НКД по данным MOEX на расчёты {m['security']['SETTLEDATE']}")
            if not r["complete"]:
                lines.append("Неполная история/корпоративные операции: прибыль и себестоимость не рассчитаны.")
        if command == "/bond":
            lines.append(f"Комиссии сделок: {rub(r['fees'])}")
            for t in sorted(bond["trades"].values(), key=lambda t: t["date"])[-12:]:
                lines.append(f"• {day(t['date']):%d.%m.%Y}: {'покупка' if dec(t['quantity']) > 0 else 'продажа'} {abs(dec(t['quantity'])):g} шт., {rub(t['amount'])} (НКД {rub(t['nkd'])}), комиссия {rub(t['fee'])}")
        if command in ("/forecast", "/maturity", "/payments", "/bond") and quantity:
            maturity_value = m["security"].get("MATDATE") or m["description"].get("MATDATE")
            bond_target = day(maturity_value) if command == "/maturity" and maturity_value and maturity_value != "0000-00-00" else target
            f = forecast(bond, m, state["asof"], bond_target)
            forecasts.append(f)
            if command in ("/forecast", "/maturity"):
                lines.append(f"\n{bond['name']} · до {bond_target:%d.%m.%Y}")
                label = "Известные выплаты" if f["unknown"] else "Ожидаемые выплаты"
                lines.append(f"{label}: купоны {rub(f['coupons'])}, возврат номинала {rub(f['principal'])}\nВсего на счёт: {rub(f['coupons'] + f['principal'])}\nКупонный доход за период с учётом НКД: {rub(f['earned'])}")
                if command == "/maturity":
                    total = r["net_trades"] + r["coupons"] + r["principal"] + f["coupons"] + f["principal"] if r["complete"] and not f["unknown"] and f["maturity"] else None
                    maturity_results.append(total)
                    lines.append(f"Итоговая прибыль с покупки до погашения: {rub(total)}")
                    if not r["complete"]:
                        warnings.append(f"{bond['name']}: неполная история покупки, итоговая прибыль недоступна")
            if command in ("/payments", "/bond"):
                for e in f["entries"]:
                    if e["day"] > today or command == "/bond":
                        calendar_entries.append((e["day"], f"• {e['day']:%d.%m.%Y} · {bond['name']} · {'купон' if e['kind']=='coupon' else 'возврат номинала'}: {rub(e['amount'])}"))
            warnings.extend(f"{bond['name']}: {w}" for w in f["unknown"])
    if command in ("/portfolio", "/profit"):
        key = "value" if command == "/portfolio" else "total"
        known_values = [r[key] for r in summary if r[key] is not None]
        known = sum(known_values, Decimal(0)) if known_values else None
        label = "Всего" if all(r[key] is not None for r in summary) and not unmatched else "По выпускам с полными данными (не весь портфель)"
        lines.append(f"\n{label}: {rub(known)}")
        if command == "/profit":
            lines.append(f"Всего получено купонов в отчётах: {rub(sum((dec(c['amount']) for c in state['cash'].values() if c['kind'] == 'coupon'), Decimal(0)))}")
    if calendar_entries:
        lines.append("\nКалендарь выплат:")
        lines.extend(text for _, text in sorted(calendar_entries))
    elif command == "/payments":
        lines.append("В ближайшие 90 дней известных выплат не найдено.")
    if command in ("/forecast", "/maturity"):
        future_coupons = sum((f["coupons"] for f in forecasts), Decimal(0))
        future_principal = sum((f["principal"] for f in forecasts), Decimal(0))
        lines.append(f"\nВсего известных выплат: {rub(future_coupons + future_principal)}\nКупоны: {rub(future_coupons)}\nВозврат вложенных денег: {rub(future_principal)}")
        if command == "/forecast":
            earned = sum((f["earned"] for f in forecasts), Decimal(0)) if all(f["earned"] is not None for f in forecasts) else None
            lines.append(f"Всего купонного дохода за период с учётом НКД: {rub(earned)}")
        if command == "/maturity":
            label = "Итоговая прибыль всего" if all(v is not None for v in maturity_results) and not unmatched else "Итоговая прибыль только выпусков с полными данными"
            known_results = [v for v in maturity_results if v is not None]
            lines.append(f"{label}: {rub(sum(known_results, Decimal(0)) if known_results else None)}")
        lines.append(f"\nВыплаты после отчёта {state['asof']:%d.%m.%Y}. Прошедшие даты — ожидаемые, не подтверждённые поступления.")
    if unmatched:
        warnings.append(f"Не сопоставлено выплат: {len(unmatched)} на {rub(sum((dec(c['amount']) for c in unmatched), Decimal(0)))}. Итоговая прибыль может быть неполной.")
    if warnings:
        lines.append("\nПредупреждения:\n" + "\n".join(dict.fromkeys(warnings)))
    lines.append("\nБез налогов. Купоны и погашения — по графику, не гарантия выплаты. Без новых сделок и реинвестирования. Будущая цена продажи не прогнозируется. Источник: MOEX ISS.")
    return "\n".join(lines), None
