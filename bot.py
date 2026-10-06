#!/usr/bin/env python3
"""A deliberately small Telegram expense tracker with no AI or dependencies."""

import calendar
import base64
import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from zoneinfo import ZoneInfo


TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")


def parse_allowed_user_ids(value):
    """Parse a comma/space separated Telegram user allowlist."""
    if not value.strip():
        return frozenset()
    parts = [part for part in re.split(r"[\s,;]+", value.strip()) if part]
    try:
        user_ids = frozenset(int(part) for part in parts)
    except ValueError as error:
        raise ValueError("Telegram user IDs must be integers") from error
    if any(user_id <= 0 for user_id in user_ids):
        raise ValueError("Telegram user IDs must be positive")
    return user_ids


ALLOWED_USER_IDS = parse_allowed_user_ids(
    os.environ.get("ALLOWED_TELEGRAM_USER_IDS")
    or os.environ.get("ALLOWED_TELEGRAM_USER_ID", "")
)
DB_PATH = os.environ.get("DATABASE_PATH", "expenses.db")
DEFAULT_TIMEZONE = os.environ.get("BOT_TIMEZONE", "Asia/Tomsk")
MORNING_HOUR = int(os.environ.get("MORNING_HOUR", "10"))
MONTHLY_REPORT_START = date(2026, 10, 1)
API_URL = "https://api.telegram.org/bot{}/{}"

EXPENSE_RE = re.compile(
    r"^(\S+)(?:\s+(.+?))?\s+(\d+(?:\.\d{1,2})?)\s*(?:₽|р|руб(?:\.|лей|ля)?)?$",
    re.IGNORECASE,
)
DATE_TEXT = r"\d{1,2}\.\d{1,2}(?:\.\d{2,4})?"
DATE_PREFIX_RE = re.compile(rf"^({DATE_TEXT})\s*(?::|-)?\s+(.+)$")
DATE_SUFFIX_RE = re.compile(rf"^(.+?)\s+({DATE_TEXT})$")
MONTHLY_RE = re.compile(r"\s+ЕЖЕМЕСЯЧНО\s*$", re.IGNORECASE)


def db():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def expense_correction_fingerprint(row):
    fields = [
        row["id"], row["spent_on"], row["category"], row["place"], row["comment"],
        row["amount_cents"], row["batch_id"] or "", row["created_at"],
    ]
    encoded = json.dumps(fields, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def apply_expense_corrections(encoded_patch, backup_path):
    """Apply a verified one-off place/comment patch without importing duplicate expenses."""
    patch = json.loads(base64.b64decode(encoded_patch, validate=True))
    if patch.get("chat_id") != 563057258 or not isinstance(patch.get("rows"), list):
        raise ValueError("Invalid expense correction target")
    rows = patch["rows"]
    if len(rows) != 19 or len({item["id"] for item in rows}) != len(rows):
        raise ValueError("Unexpected expense correction rows")
    migration = "expense_places_2026_10_06_" + hashlib.sha256(encoded_patch.encode()).hexdigest()[:12]
    with db() as connection:
        if connection.execute(
            "SELECT 1 FROM data_migrations WHERE name = ?", (migration,)
        ).fetchone():
            return 0
        for item in rows:
            if (
                type(item.get("id")) is not int
                or not isinstance(item.get("before_sha256"), str)
                or not isinstance(item.get("place"), str)
                or not isinstance(item.get("comment"), str)
            ):
                raise ValueError("Invalid expense correction row")
            current = connection.execute(
                """SELECT id, spent_on, category, place, comment, amount_cents, batch_id, created_at
                   FROM expenses WHERE chat_id = ? AND id = ?""",
                (patch["chat_id"], item["id"]),
            ).fetchone()
            if current is None or expense_correction_fingerprint(current) != item["before_sha256"]:
                raise ValueError(f"Expense {item['id']} differs from the CSV export; no changes made")
        if not os.path.exists(backup_path):
            with sqlite3.connect(backup_path) as backup:
                connection.backup(backup)
        connection.execute("BEGIN IMMEDIATE")
        for item in rows:
            current = connection.execute(
                """SELECT id, spent_on, category, place, comment, amount_cents, batch_id, created_at
                   FROM expenses WHERE chat_id = ? AND id = ?""",
                (patch["chat_id"], item["id"]),
            ).fetchone()
            if current is None or expense_correction_fingerprint(current) != item["before_sha256"]:
                raise ValueError(f"Expense {item['id']} changed during backup; no changes made")
        for item in rows:
            connection.execute(
                "UPDATE expenses SET place = ?, comment = ? WHERE chat_id = ? AND id = ?",
                (normalize_place(item["place"]), item["comment"], patch["chat_id"], item["id"]),
            )
        connection.execute(
            "INSERT INTO data_migrations(name, applied_at, affected_rows) VALUES (?, ?, ?)",
            (migration, datetime.utcnow().isoformat(), len(rows)),
        )
    return len(rows)


def init_db():
    with db() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER PRIMARY KEY,
                budget_cents INTEGER NOT NULL DEFAULT 0,
                salary_cents INTEGER NOT NULL DEFAULT 0,
                timezone TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                category TEXT NOT NULL,
                place TEXT NOT NULL DEFAULT '',
                comment TEXT NOT NULL DEFAULT '',
                amount_cents INTEGER NOT NULL,
                spent_on TEXT NOT NULL,
                created_at TEXT NOT NULL,
                batch_id TEXT
            );

            CREATE INDEX IF NOT EXISTS expenses_chat_date
                ON expenses(chat_id, spent_on);

            CREATE TABLE IF NOT EXISTS day_status (
                chat_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                acknowledged INTEGER NOT NULL DEFAULT 0,
                midnight_pending INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (chat_id, day)
            );

            CREATE TABLE IF NOT EXISTS sent_messages (
                chat_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                message_type TEXT NOT NULL,
                PRIMARY KEY (chat_id, day, message_type)
            );

            CREATE TABLE IF NOT EXISTS category_limits (
                chat_id INTEGER NOT NULL,
                category TEXT NOT NULL,
                limit_cents INTEGER NOT NULL,
                PRIMARY KEY (chat_id, category)
            );

            CREATE TABLE IF NOT EXISTS recurring_expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                category TEXT NOT NULL,
                place TEXT NOT NULL DEFAULT '',
                comment TEXT NOT NULL DEFAULT '',
                amount_cents INTEGER NOT NULL,
                day_of_month INTEGER NOT NULL,
                last_generated_month TEXT,
                active INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS favorites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                category TEXT NOT NULL,
                place TEXT NOT NULL DEFAULT '',
                comment TEXT NOT NULL DEFAULT '',
                amount_cents INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS pending_actions (
                chat_id INTEGER PRIMARY KEY,
                action TEXT NOT NULL,
                expense_id INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS income (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                description TEXT NOT NULL,
                amount_cents INTEGER NOT NULL,
                received_on TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS salary_history (
                chat_id INTEGER NOT NULL,
                effective_from TEXT NOT NULL,
                salary_cents INTEGER NOT NULL,
                PRIMARY KEY (chat_id, effective_from)
            );

            CREATE TABLE IF NOT EXISTS planned_expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                description TEXT NOT NULL,
                amount_cents INTEGER NOT NULL,
                planned_on TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS imported_files (
                chat_id INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                PRIMARY KEY (chat_id, sha256)
            );

            CREATE TABLE IF NOT EXISTS data_migrations (
                name TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL,
                affected_rows INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS data_migration_expenses (
                migration_name TEXT NOT NULL,
                expense_id INTEGER NOT NULL,
                old_category TEXT NOT NULL,
                PRIMARY KEY (migration_name, expense_id)
            );
            """
        )
        user_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(users)").fetchall()
        }
        if "salary_cents" not in user_columns:
            connection.execute(
                "ALTER TABLE users ADD COLUMN salary_cents INTEGER NOT NULL DEFAULT 0"
            )
        for name, declaration in (
            ("savings_goal_cents", "INTEGER NOT NULL DEFAULT 0"),
            ("day_cutoff_hour", "INTEGER NOT NULL DEFAULT 4"),
            ("reminder_hours", "TEXT NOT NULL DEFAULT '20,22,0'"),
            ("morning_hour", f"INTEGER NOT NULL DEFAULT {MORNING_HOUR}"),
            ("profit_hour", "INTEGER NOT NULL DEFAULT 10"),
        ):
            if name not in user_columns:
                connection.execute(f"ALTER TABLE users ADD COLUMN {name} {declaration}")
        migration = "morning_budget_at_10_2026_10_06"
        if not connection.execute(
            "SELECT 1 FROM data_migrations WHERE name = ?", (migration,)
        ).fetchone():
            updated = connection.execute(
                "UPDATE users SET morning_hour = 10 WHERE morning_hour = 9"
            ).rowcount
            connection.execute(
                "INSERT INTO data_migrations(name, applied_at, affected_rows) VALUES (?, ?, ?)",
                (migration, datetime.utcnow().isoformat(), updated),
            )
        connection.execute(
            """INSERT OR IGNORE INTO salary_history(chat_id, effective_from, salary_cents)
               SELECT chat_id, '0001-01-01', salary_cents FROM users
               WHERE salary_cents > 0 AND NOT EXISTS
               (SELECT 1 FROM salary_history WHERE salary_history.chat_id = users.chat_id)"""
        )
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(expenses)").fetchall()
        }
        if "batch_id" not in columns:
            connection.execute("ALTER TABLE expenses ADD COLUMN batch_id TEXT")
        if "comment" not in columns:
            connection.execute(
                "ALTER TABLE expenses ADD COLUMN comment TEXT NOT NULL DEFAULT ''"
            )
        if "place" not in columns:
            connection.execute(
                "ALTER TABLE expenses ADD COLUMN place TEXT NOT NULL DEFAULT ''"
            )
        for table in ("recurring_expenses", "favorites"):
            table_columns = {
                row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
            }
            if "place" not in table_columns:
                connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN place TEXT NOT NULL DEFAULT ''"
                )
        rows = connection.execute("SELECT id, category, place FROM expenses").fetchall()
        connection.executemany(
            "UPDATE expenses SET category = ?, place = ? WHERE id = ?",
            [
                (normalize_category(row["category"]), normalize_place(row["place"]), row["id"])
                for row in rows
            ],
        )
        for table in ("recurring_expenses", "favorites"):
            rows = connection.execute(f"SELECT id, place FROM {table}").fetchall()
            connection.executemany(
                f"UPDATE {table} SET place = ? WHERE id = ?",
                [(normalize_place(row["place"]), row["id"]) for row in rows],
            )
        migration = "563057258_transport_small_fares_to_bus_2026_09_30"
        if not connection.execute(
            "SELECT 1 FROM data_migrations WHERE name = ?", (migration,)
        ).fetchone():
            targets = connection.execute(
                """SELECT id, category FROM expenses
                   WHERE chat_id = ? AND category = 'Транспорт'
                   AND amount_cents IN (4000, 4100, 8000)
                   AND batch_id IS NULL""",
                (563057258,),
            ).fetchall()
            connection.executemany(
                """INSERT INTO data_migration_expenses(migration_name, expense_id, old_category)
                   VALUES (?, ?, ?)""",
                [(migration, row["id"], row["category"]) for row in targets],
            )
            connection.executemany(
                "UPDATE expenses SET category = 'Автобус' WHERE id = ?",
                [(row["id"],) for row in targets],
            )
            updated = len(targets)
            connection.execute(
                "INSERT INTO data_migrations(name, applied_at, affected_rows) VALUES (?, ?, ?)",
                (migration, datetime.utcnow().isoformat(), updated),
            )
            if updated:
                print(f"One-time bus category migration: {updated} expense(s)", flush=True)

        migration = "563057258_remaining_transport_to_taxi_2026_09_30"
        if not connection.execute(
            "SELECT 1 FROM data_migrations WHERE name = ?", (migration,)
        ).fetchone():
            targets = connection.execute(
                """SELECT id, category FROM expenses
                   WHERE chat_id = ? AND category = 'Транспорт'""",
                (563057258,),
            ).fetchall()
            connection.executemany(
                """INSERT INTO data_migration_expenses(migration_name, expense_id, old_category)
                   VALUES (?, ?, ?)""",
                [(migration, row["id"], row["category"]) for row in targets],
            )
            connection.executemany(
                "UPDATE expenses SET category = 'Такси' WHERE id = ?",
                [(row["id"],) for row in targets],
            )
            updated = len(targets)
            connection.execute(
                "INSERT INTO data_migrations(name, applied_at, affected_rows) VALUES (?, ?, ?)",
                (migration, datetime.utcnow().isoformat(), updated),
            )
            if updated:
                print(f"One-time taxi category migration: {updated} expense(s)", flush=True)


def api(method, **params):
    body = urllib.parse.urlencode(params).encode()
    request = urllib.request.Request(API_URL.format(TOKEN, method), data=body)
    try:
        with urllib.request.urlopen(request, timeout=35) as response:
            result = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError) as error:
        print("Telegram request failed:", error, file=sys.stderr)
        return None
    if not result.get("ok"):
        print("Telegram API error:", result, file=sys.stderr)
        return None
    return result.get("result")


def send(chat_id, text, reply_markup=None):
    params = {"chat_id": chat_id, "text": text}
    if reply_markup:
        params["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    return api("sendMessage", **params)


def send_document(chat_id, filename, content, caption=""):
    boundary = "----finance-tracker-boundary"
    chunks = []

    def field(name, value):
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                str(value).encode("utf-8"),
                b"\r\n",
            ]
        )

    field("chat_id", chat_id)
    if caption:
        field("caption", caption)
    chunks.extend(
        [
            f"--{boundary}\r\n".encode(),
            (
                f'Content-Disposition: form-data; name="document"; filename="{filename}"\r\n'
                "Content-Type: text/csv; charset=utf-8\r\n\r\n"
            ).encode(),
            content,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    request = urllib.request.Request(
        API_URL.format(TOKEN, "sendDocument"),
        data=b"".join(chunks),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=35) as response:
            result = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError) as error:
        print("Telegram document upload failed:", error, file=sys.stderr)
        return None
    if not result.get("ok"):
        print("Telegram API error:", result, file=sys.stderr)
        return None
    return result.get("result")


def money(cents):
    value = Decimal(cents) / 100
    if value == value.to_integral():
        rendered = f"{int(value):,}".replace(",", " ")
    else:
        rendered = f"{value:,.2f}".replace(",", " ")
    return rendered + " ₽"


def parse_money(value, allow_zero=False):
    if not re.fullmatch(r"\d+(?:\.\d{1,2})?", value):
        raise ValueError("Сумма должна быть числом, копейки — через точку.")
    cents = int(Decimal(value) * 100)
    if cents < 0 or (cents == 0 and not allow_zero):
        raise ValueError("Сумма должна быть больше нуля.")
    return cents


def salary_for_day(chat_id, day):
    with db() as connection:
        row = connection.execute(
            """SELECT salary_cents FROM salary_history
               WHERE chat_id = ? AND effective_from <= ?
               ORDER BY effective_from DESC LIMIT 1""",
            (chat_id, day.isoformat()),
        ).fetchone()
        if row:
            return row["salary_cents"]
        has_history = connection.execute(
            "SELECT 1 FROM salary_history WHERE chat_id = ? LIMIT 1", (chat_id,)
        ).fetchone()
        if has_history:
            return 0
        user = connection.execute(
            "SELECT salary_cents FROM users WHERE chat_id = ?", (chat_id,)
        ).fetchone()
    return user["salary_cents"] if user else 0


def salary_share(chat_id, start, end):
    total = 0
    day = start
    while day <= end:
        monthly = salary_for_day(chat_id, day)
        days = calendar.monthrange(day.year, day.month)[1]
        total += int((Decimal(monthly) / days).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        day += timedelta(days=1)
    return total


def salary_month_total(chat_id, month_day):
    days = calendar.monthrange(month_day.year, month_day.month)[1]
    value = sum(
        (Decimal(salary_for_day(chat_id, date(month_day.year, month_day.month, number))) / days
         for number in range(1, days + 1)),
        Decimal(0),
    )
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def income_total(chat_id, start, end):
    with db() as connection:
        row = connection.execute(
            """SELECT COALESCE(SUM(amount_cents), 0) AS total FROM income
               WHERE chat_id = ? AND received_on BETWEEN ? AND ?""",
            (chat_id, start.isoformat(), end.isoformat()),
        ).fetchone()
    return row["total"]


def incomes_text(chat_id):
    with db() as connection:
        rows = connection.execute(
            "SELECT * FROM income WHERE chat_id = ? ORDER BY received_on DESC, id DESC LIMIT 20",
            (chat_id,),
        ).fetchall()
    if not rows:
        return "Других доходов пока нет. Добавь: /income подработка 5000"
    lines = ["Последние доходы:"]
    lines.extend(
        f"• #{row['id']} · {date.fromisoformat(row['received_on']):%d.%m.%Y} · {row['description']} — {money(row['amount_cents'])}"
        for row in rows
    )
    lines.append("Удалить ошибочную запись: /income_delete ID")
    return "\n".join(lines)


def monthly_balance(chat_id, today):
    start = today.replace(day=1)
    salary = salary_month_total(chat_id, today)
    income = income_total(chat_id, start, today)
    expenses = spent(chat_id, start, today)
    return salary + income - expenses


def ensure_user(chat_id):
    with db() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO users(chat_id, timezone, created_at, morning_hour) VALUES (?, ?, ?, ?)",
            (chat_id, DEFAULT_TIMEZONE, datetime.utcnow().isoformat(), MORNING_HOUR),
        )


def is_allowed(update_part):
    sender = update_part.get("from") or {}
    return sender.get("id") in ALLOWED_USER_IDS


def normalize_category(category):
    return " ".join(category.split()).casefold().capitalize()


def normalize_place(place):
    return " ".join(place.split()).casefold().capitalize()


def business_day(local_now, cutoff_hour=4):
    """The expense day changes at 04:00 in the user's local timezone."""
    day = local_now.date()
    return day - timedelta(days=1) if local_now.hour < cutoff_hour else day


def user_today(user):
    return business_day(datetime.now(ZoneInfo(user["timezone"])), user["day_cutoff_hour"])


def user_calendar_today(user):
    return datetime.now(ZoneInfo(user["timezone"])).date()


def get_user(chat_id):
    with db() as connection:
        return connection.execute(
            "SELECT * FROM users WHERE chat_id = ?", (chat_id,)
        ).fetchone()


def parse_expenses(text):
    """Parse `category [place [comment]] amount` with comma-separated items."""
    parts = [part.strip() for part in text.split(",") if part.strip()]
    if not parts:
        raise ValueError("Пустое сообщение")

    parsed = []
    for part in parts:
        match = EXPENSE_RE.match(part)
        if not match:
            raise ValueError(f"Не понял: «{part}»")
        category = normalize_category(match.group(1))
        details = (match.group(2) or "").split(maxsplit=1)
        place = normalize_place(details[0]) if details else ""
        comment = " ".join(details[1].split()) if len(details) > 1 else ""
        if not category or len(category) > 60 or any(char.isdigit() for char in category):
            raise ValueError(f"Некорректная категория: «{category}»")
        if len(place) > 60:
            raise ValueError("Название места слишком длинное")
        if len(comment) > 300:
            raise ValueError("Комментарий слишком длинный")
        try:
            amount = Decimal(match.group(3))
        except InvalidOperation:
            raise ValueError(f"Некорректная сумма: «{match.group(3)}»")
        cents = int(amount * 100)
        if cents <= 0:
            raise ValueError("Сумма должна быть больше нуля")
        parsed.append((category, place, comment, cents))
    return parsed


def parse_date(value, today, latest_date=None):
    latest_date = latest_date or today
    pieces = value.split(".")
    day, month = int(pieces[0]), int(pieces[1])
    year = latest_date.year if len(pieces) == 2 else int(pieces[2])
    if year < 100:
        year += 2000
    try:
        result = date(year, month, day)
    except ValueError:
        raise ValueError(f"Некорректная дата: «{value}»")
    if result > latest_date:
        raise ValueError("Нельзя записать трату на будущую дату")
    return result


def parse_expense_message(text, today, latest_date=None):
    """Parse expenses with an optional global prefix or per-item date suffix."""
    global_date = None
    prefix = DATE_PREFIX_RE.match(text.strip())
    if prefix:
        global_date = parse_date(prefix.group(1), today, latest_date)
        text = prefix.group(2)

    result = []
    parts = [part.strip() for part in text.split(",") if part.strip()]
    if not parts:
        raise ValueError("Пустое сообщение")
    for part in parts:
        spent_on = global_date or today
        monthly = bool(MONTHLY_RE.search(part))
        if monthly:
            part = MONTHLY_RE.sub("", part).strip()
        if global_date is None:
            suffix = DATE_SUFFIX_RE.match(part)
            if suffix:
                part = suffix.group(1).strip()
                spent_on = parse_date(suffix.group(2), today, latest_date)
        parsed = parse_expenses(part)
        category, place, comment, cents = parsed[0]
        result.append((category, place, comment, cents, spent_on, monthly))
    return result


def save_expenses(chat_id, items, spent_on):
    now = datetime.utcnow().isoformat()
    values = []
    for item in items:
        if len(item) == 3:
            category, comment, cents = item
            place = ""
        else:
            category, place, comment, cents = item
        values.append((
            chat_id, normalize_category(category), normalize_place(place),
            comment, cents, spent_on.isoformat(), now,
        ))
    with db() as connection:
        connection.executemany(
            """
            INSERT INTO expenses(
                chat_id, category, place, comment, amount_cents, spent_on, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        connection.execute(
            """
            INSERT INTO day_status(chat_id, day, acknowledged, midnight_pending)
            VALUES (?, ?, 1, 0)
            ON CONFLICT(chat_id, day) DO UPDATE SET acknowledged = 1, midnight_pending = 0
            """,
            (chat_id, spent_on.isoformat()),
        )


def save_monthly_expense(
    chat_id, category, comment, amount_cents, month_date, acknowledge=False, place=""
):
    days_in_month = calendar.monthrange(month_date.year, month_date.month)[1]
    daily_cents, remainder = divmod(amount_cents, days_in_month)
    batch_id = uuid.uuid4().hex
    now = datetime.utcnow().isoformat()
    rows = []
    for day_number in range(1, days_in_month + 1):
        cents = daily_cents + (1 if day_number <= remainder else 0)
        spent_on = date(month_date.year, month_date.month, day_number)
        rows.append(
            (
                chat_id,
                normalize_category(category),
                normalize_place(place),
                comment,
                cents,
                spent_on.isoformat(),
                now,
                batch_id,
            )
        )
    with db() as connection:
        connection.executemany(
            """
            INSERT INTO expenses(
                chat_id, category, place, comment, amount_cents, spent_on, created_at, batch_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        if acknowledge:
            connection.execute(
                """
                INSERT INTO day_status(chat_id, day, acknowledged, midnight_pending)
                VALUES (?, ?, 1, 0)
                ON CONFLICT(chat_id, day)
                DO UPDATE SET acknowledged = 1, midnight_pending = 0
                """,
                (chat_id, month_date.isoformat()),
            )
    return daily_cents, days_in_month


def spent(chat_id, start, end):
    with db() as connection:
        row = connection.execute(
            """
            SELECT COALESCE(SUM(amount_cents), 0) AS total
            FROM expenses WHERE chat_id = ? AND spent_on BETWEEN ? AND ?
            """,
            (chat_id, start.isoformat(), end.isoformat()),
        ).fetchone()
    return row["total"]


def category_totals(chat_id, start, end, limit=5):
    with db() as connection:
        return connection.execute(
            """
            SELECT category, SUM(amount_cents) AS total
            FROM expenses
            WHERE chat_id = ? AND spent_on BETWEEN ? AND ?
            GROUP BY lower(category)
            ORDER BY total DESC LIMIT ?
            """,
            (chat_id, start.isoformat(), end.isoformat(), limit),
        ).fetchall()


def category_report_text(chat_id, category, month_start):
    category = normalize_category(category)
    month_end = month_start.replace(
        day=calendar.monthrange(month_start.year, month_start.month)[1]
    )
    with db() as connection:
        places = connection.execute(
            """
            SELECT place, SUM(amount_cents) AS total,
                   COUNT(DISTINCT CASE WHEN amount_cents > 0 THEN
                       CASE WHEN batch_id IS NULL THEN 'expense:' || id
                            ELSE 'batch:' || batch_id END
                   END) AS purchases
            FROM expenses
            WHERE chat_id = ? AND category = ? AND spent_on BETWEEN ? AND ?
            GROUP BY place ORDER BY total DESC, place
            """,
            (chat_id, category, month_start.isoformat(), month_end.isoformat()),
        ).fetchall()
        recent = connection.execute(
            """
            SELECT place, comment, SUM(amount_cents) AS total,
                   MIN(spent_on) AS first_day
            FROM expenses
            WHERE chat_id = ? AND category = ? AND spent_on BETWEEN ? AND ?
            GROUP BY CASE WHEN batch_id IS NULL THEN 'expense:' || id
                          ELSE 'batch:' || batch_id END
            ORDER BY MAX(spent_on) DESC, MAX(id) DESC LIMIT 5
            """,
            (chat_id, category, month_start.isoformat(), month_end.isoformat()),
        ).fetchall()
    if not places:
        return f"За {month_start:%m.%Y} трат в категории «{category}» нет."
    total = sum(row["total"] for row in places)
    purchases = sum(row["purchases"] for row in places)
    lines = [
        f"Категория «{category}» за {month_start:%m.%Y}",
        f"Всего: {money(total)}",
        f"Покупок: {purchases}",
        f"Средний чек: {money(total // purchases) if purchases else '—'}",
        "",
        "Где потрачено больше всего:",
    ]
    for row in places[:10]:
        place = row["place"] or "Без места"
        share = round(row["total"] * 100 / total) if total else 0
        lines.append(
            f"• {place}: {money(row['total'])} ({share}%, покупок: {row['purchases']})"
        )
    if len(places) > 10:
        lines.append(f"• Остальные места: {money(sum(row['total'] for row in places[10:]))}")
    lines.extend(["", "Последние покупки:"])
    for row in recent:
        day = date.fromisoformat(row["first_day"])
        place = row["place"] or "Без места"
        comment = f" · {row['comment'][:80]}" if row["comment"] else ""
        lines.append(f"• {day:%d.%m} · {place} · {money(row['total'])}{comment}")
    return "\n".join(lines)


def daily_totals(chat_id, start, end):
    with db() as connection:
        return connection.execute(
            """
            SELECT spent_on, SUM(amount_cents) AS total
            FROM expenses
            WHERE chat_id = ? AND spent_on BETWEEN ? AND ?
            GROUP BY spent_on
            ORDER BY spent_on ASC
            """,
            (chat_id, start.isoformat(), end.isoformat()),
        ).fetchall()


def recent_expenses(chat_id, limit=10):
    """Return recent purchases, collapsing a monthly allocation into one item."""
    with db() as connection:
        return connection.execute(
            """
            SELECT
                MAX(id) AS id,
                category,
                place,
                comment,
                SUM(amount_cents) AS total,
                MIN(spent_on) AS start_day,
                MAX(spent_on) AS end_day,
                batch_id
            FROM expenses
            WHERE chat_id = ?
            GROUP BY CASE
                WHEN batch_id IS NULL THEN 'expense:' || id
                ELSE 'batch:' || batch_id
            END
            ORDER BY MAX(created_at) DESC, MAX(id) DESC
            LIMIT ?
            """,
            (chat_id, limit),
        ).fetchall()


def expense_details(chat_id, expense_id):
    with db() as connection:
        row = connection.execute(
            """
            SELECT id, category, place, comment, amount_cents, spent_on, batch_id
            FROM expenses WHERE chat_id = ? AND id = ?
            """,
            (chat_id, expense_id),
        ).fetchone()
        if not row:
            return None
        if not row["batch_id"]:
            return {
                "id": row["id"],
                "category": row["category"],
                "place": row["place"],
                "comment": row["comment"],
                "total": row["amount_cents"],
                "start_day": row["spent_on"],
                "end_day": row["spent_on"],
                "monthly": False,
            }
        summary = connection.execute(
            """
            SELECT SUM(amount_cents) AS total, MIN(spent_on) AS start_day,
                   MAX(spent_on) AS end_day
            FROM expenses WHERE chat_id = ? AND batch_id = ?
            """,
            (chat_id, row["batch_id"]),
        ).fetchone()
        return {
            "id": row["id"],
            "category": row["category"],
            "place": row["place"],
            "comment": row["comment"],
            "total": summary["total"],
            "start_day": summary["start_day"],
            "end_day": summary["end_day"],
            "monthly": True,
        }


def expense_details_text(details):
    start_day = date.fromisoformat(details["start_day"])
    end_day = date.fromisoformat(details["end_day"])
    if details["monthly"]:
        when = f"Период: {start_day:%d.%m.%Y} — {end_day:%d.%m.%Y}"
    else:
        when = f"Дата: {start_day:%d.%m.%Y}"
    comment = details["comment"] or "—"
    return (
        f"Покупка #{details['id']}\n"
        f"{when}\n"
        f"Категория: {details['category']}\n"
        f"Место: {details['place'] or '—'}\n"
        f"Комментарий: {comment}\n"
        f"Сумма: {money(details['total'])}"
    )


def history_message(chat_id):
    rows = recent_expenses(chat_id)
    if not rows:
        return "История пока пустая.", None
    buttons = []
    for row in rows:
        start_day = date.fromisoformat(row["start_day"])
        place = f" · {row['place']}" if row["place"] else ""
        label = f"{start_day:%d.%m} · {row['category']}{place} · {money(row['total'])}"
        buttons.append(
            [{"text": label, "callback_data": f"expense:{row['id']}"}]
        )
    return "Последние покупки. Нажми на покупку, чтобы открыть подробности:", {
        "inline_keyboard": buttons
    }


def expense_actions_keyboard(expense_id):
    return {
        "inline_keyboard": [
            [
                {"text": "✏️ Редактировать", "callback_data": f"edit:{expense_id}"},
                {"text": "🗑 Удалить", "callback_data": f"delete:{expense_id}"},
            ]
        ]
    }


def delete_expense(chat_id, expense_id):
    """Delete one logical purchase, including a complete monthly allocation."""
    with db() as connection:
        row = connection.execute(
            "SELECT id, category, comment, amount_cents, batch_id FROM expenses "
            "WHERE chat_id = ? AND id = ?",
            (chat_id, expense_id),
        ).fetchone()
        if not row:
            return None
        if row["batch_id"]:
            total = connection.execute(
                "SELECT SUM(amount_cents) AS total FROM expenses "
                "WHERE chat_id = ? AND batch_id = ?",
                (chat_id, row["batch_id"]),
            ).fetchone()["total"]
            connection.execute(
                "DELETE FROM expenses WHERE chat_id = ? AND batch_id = ?",
                (chat_id, row["batch_id"]),
            )
        else:
            total = row["amount_cents"]
            connection.execute(
                "DELETE FROM expenses WHERE chat_id = ? AND id = ?",
                (chat_id, expense_id),
            )
        return {"category": row["category"], "total": total}


def set_pending_edit(chat_id, expense_id):
    with db() as connection:
        connection.execute(
            """
            INSERT INTO pending_actions(chat_id, action, expense_id)
            VALUES (?, 'edit', ?)
            ON CONFLICT(chat_id) DO UPDATE SET action = 'edit', expense_id = excluded.expense_id
            """,
            (chat_id, expense_id),
        )


def get_pending_action(chat_id):
    with db() as connection:
        return connection.execute(
            "SELECT action, expense_id FROM pending_actions WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()


def clear_pending_action(chat_id):
    with db() as connection:
        connection.execute("DELETE FROM pending_actions WHERE chat_id = ?", (chat_id,))


def replace_expense(chat_id, expense_id, item, today):
    details = expense_details(chat_id, expense_id)
    if not details:
        return None
    category, place, comment, cents, spent_on, monthly = item
    delete_expense(chat_id, expense_id)
    if monthly:
        save_monthly_expense(
            chat_id,
            category,
            comment,
            cents,
            spent_on,
            acknowledge=spent_on == today,
            place=place,
        )
    else:
        save_expenses(chat_id, [(category, place, comment, cents)], spent_on)
    return category, place, comment, cents, spent_on, monthly


def period_report_text(chat_id, start, end, zero_through=None):
    total = spent(chat_id, start, end)
    rows = daily_totals(chat_id, start, end)
    categories = category_totals(chat_id, start, end, 10)
    observed_days = max((end - start).days + 1, 1)
    lines = [
        f"Отчёт за {start:%d.%m.%Y} — {end:%d.%m.%Y}",
        f"Всего: {money(total)}",
        f"В среднем в день: {money(total // observed_days)}",
        "",
        "По дням:",
    ]
    if zero_through is not None:
        amounts_by_day = {row["spent_on"]: row["total"] for row in rows}
        day = start
        while day <= min(zero_through, end):
            amounts_by_day.setdefault(day.isoformat(), 0)
            day += timedelta(days=1)
        if amounts_by_day:
            lines.extend(
                f"• {date.fromisoformat(day_key):%d.%m} — {money(amounts_by_day[day_key])}"
                for day_key in sorted(amounts_by_day)
            )
        else:
            lines.append("• Нет трат")
    elif rows:
        lines.extend(
            f"• {date.fromisoformat(row['spent_on']):%d.%m} — {money(row['total'])}"
            for row in rows
        )
    else:
        lines.append("• Нет трат")
    lines.extend(["", "Категории:"])
    if categories:
        for row in categories:
            share = round(row["total"] * 100 / total) if total else 0
            lines.append(f"• {row['category']}: {money(row['total'])} ({share}%)")
    else:
        lines.append("• Нет трат")
    user = get_user(chat_id)
    other_income = income_total(chat_id, start, end)
    if (
        user
        and salary_month_total(chat_id, end)
        and start == end.replace(day=1)
        and (end == user_today(user) or end.day == calendar.monthrange(end.year, end.month)[1])
    ):
        lines.extend(
            [
                "",
                f"Зарплата за месяц: {money(salary_month_total(chat_id, end))}",
                f"Прибыль за месяц (зарплата − траты): {money(salary_month_total(chat_id, end) + other_income - total)}",
            ]
        )
    if other_income:
        lines.append(f"Другие доходы: {money(other_income)}")
        if not salary_month_total(chat_id, end):
            lines.append(f"Прибыль (другие доходы − траты): {money(other_income - total)}")
    return "\n".join(lines)


def report_keyboard(month_start=None, current_month=None):
    rows = [
            [
                {"text": "Сегодня", "callback_data": "report:today"},
                {"text": "Неделя", "callback_data": "report:week"},
                {"text": "Месяц", "callback_data": "report:month"},
            ],
            [{"text": "Сравнить недели", "callback_data": "report:compare"}],
    ]
    if month_start is not None:
        previous = (month_start - timedelta(days=1)).replace(day=1)
        navigation = [{"text": "← Предыдущий месяц", "callback_data": f"report_month:{previous:%Y-%m}"}]
        next_month = (month_start + timedelta(days=32)).replace(day=1)
        if current_month is None or next_month <= current_month:
            navigation.append({"text": "Следующий месяц →", "callback_data": f"report_month:{next_month:%Y-%m}"})
        rows.insert(0, navigation)
    return {"inline_keyboard": rows}


def comparison_text(chat_id, today):
    current_start = today - timedelta(days=today.weekday())
    previous_end = current_start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=6)
    current = spent(chat_id, current_start, today)
    previous = spent(chat_id, previous_start, previous_end)
    difference = current - previous
    if previous:
        percent = round(abs(difference) * 100 / previous)
        direction = "больше" if difference > 0 else "меньше"
        comparison = (
            "Столько же, сколько на прошлой неделе."
            if difference == 0
            else f"Это на {percent}% {direction}, чем на прошлой неделе."
        )
    elif current:
        comparison = "На прошлой неделе трат не было."
    else:
        comparison = "Трат нет ни на этой, ни на прошлой неделе."
    current_categories = {row["category"]: row["total"] for row in category_totals(chat_id, current_start, today, 50)}
    previous_categories = {row["category"]: row["total"] for row in category_totals(chat_id, previous_start, previous_end, 50)}
    changes = sorted(
        ((category, amount - previous_categories.get(category, 0)) for category, amount in current_categories.items()),
        key=lambda item: item[1],
        reverse=True,
    )
    lines = [
        "Сравнение недель",
        f"Текущая: {money(current)}",
        f"Прошлая: {money(previous)}",
        comparison,
    ]
    increases = [(category, value) for category, value in changes if value > 0][:3]
    if increases:
        lines.extend(["", "Больше всего выросли:"])
        lines.extend(f"• {category}: +{money(value)}" for category, value in increases)
    return "\n".join(lines)


def month_comparison_text(chat_id, today):
    current_start = today.replace(day=1)
    previous_end = current_start - timedelta(days=1)
    previous_start = previous_end.replace(day=1)
    current = spent(chat_id, current_start, today)
    previous = spent(chat_id, previous_start, previous_end)
    current_categories = {row["category"]: row["total"] for row in category_totals(chat_id, current_start, today, 100)}
    previous_categories = {row["category"]: row["total"] for row in category_totals(chat_id, previous_start, previous_end, 100)}
    changes = sorted(
        ((name, amount - previous_categories.get(name, 0)) for name, amount in current_categories.items()),
        key=lambda item: item[1], reverse=True,
    )
    lines = [
        "Сравнение месяцев (текущий — на сегодня, прошлый — целиком)",
        f"Этот месяц: {money(current)}",
        f"Прошлый: {money(previous)}",
        f"Разница: {money(current - previous)}",
    ]
    rises = [(name, change) for name, change in changes if change > 0][:3]
    if rises:
        lines.extend(["", "Больше всего выросли:"])
        lines.extend(f"• {name}: +{money(change)}" for name, change in rises)
    return "\n".join(lines)


def weekly_digest_text(chat_id, monday):
    end = monday - timedelta(days=1)
    start = end - timedelta(days=6)
    before_end = start - timedelta(days=1)
    before_start = before_end - timedelta(days=6)
    total = spent(chat_id, start, end)
    previous = spent(chat_id, before_start, before_end)
    earnings = salary_share(chat_id, start, end) + income_total(chat_id, start, end)
    top = category_totals(chat_id, start, end, 3)
    lines = [
        f"Итог недели {start:%d.%m}–{end:%d.%m}",
        f"Траты: {money(total)}",
        f"Прибыль (доходы − траты): {money(earnings - total)}",
        f"К прошлой неделе: {money(total - previous)}",
        "Топ категорий: " + (", ".join(f"{row['category']} {money(row['total'])}" for row in top) if top else "нет трат"),
    ]
    return "\n".join(lines)


def anomaly_text(chat_id, category, today):
    week_start = today - timedelta(days=today.weekday())
    prior_end = week_start - timedelta(days=1)
    prior_start = prior_end - timedelta(days=27)
    with db() as connection:
        previous = connection.execute(
            """SELECT COALESCE(SUM(amount_cents), 0) AS total FROM expenses
               WHERE chat_id = ? AND category = ? AND spent_on BETWEEN ? AND ?""",
            (chat_id, category, prior_start.isoformat(), prior_end.isoformat()),
        ).fetchone()["total"]
        current = connection.execute(
            """SELECT COALESCE(SUM(amount_cents), 0) AS total FROM expenses
               WHERE chat_id = ? AND category = ? AND spent_on BETWEEN ? AND ?""",
            (chat_id, category, week_start.isoformat(), today.isoformat()),
        ).fetchone()["total"]
    typical_week = previous // 4
    if typical_week >= 100000 and current > typical_week * 2:
        return f"⚠️ На «{category}» за эту неделю ушло {money(current)} — больше чем вдвое выше обычного ({money(typical_week)} в неделю)."
    return None


def plans_text(chat_id, today):
    with db() as connection:
        rows = connection.execute(
            "SELECT * FROM planned_expenses WHERE chat_id = ? AND planned_on >= ? ORDER BY planned_on, id",
            (chat_id, today.isoformat()),
        ).fetchall()
    if not rows:
        return "Планируемых трат нет. Добавь: /plan 15.10 стоматолог 15000"
    lines = ["Планируемые траты:"]
    for row in rows:
        lines.append(f"• #{row['id']} · {date.fromisoformat(row['planned_on']):%d.%m} · {row['description']} — {money(row['amount_cents'])}")
    lines.append("Удалить: /plan_delete ID")
    return "\n".join(lines)


def free_to_spend_text(user, today):
    start = today.replace(day=1)
    end = today.replace(day=calendar.monthrange(today.year, today.month)[1])
    with db() as connection:
        planned = connection.execute(
            """SELECT COALESCE(SUM(amount_cents), 0) AS total FROM planned_expenses
               WHERE chat_id = ? AND planned_on BETWEEN ? AND ?""",
            (user["chat_id"], today.isoformat(), end.isoformat()),
        ).fetchone()["total"]
    available = monthly_balance(user["chat_id"], today) - user["savings_goal_cents"] - planned
    return (
        f"Свободно до конца месяца: {money(available)}\n"
        f"Цель накоплений: {money(user['savings_goal_cents'])}\n"
        f"Будущие запланированные траты: {money(planned)}"
    )


def set_category_limit(chat_id, category, limit_cents):
    category = normalize_category(category)
    with db() as connection:
        connection.execute(
            """
            INSERT INTO category_limits(chat_id, category, limit_cents)
            VALUES (?, ?, ?)
            ON CONFLICT(chat_id, category)
            DO UPDATE SET limit_cents = excluded.limit_cents
            """,
            (chat_id, category, limit_cents),
        )


def category_limits_text(chat_id):
    with db() as connection:
        rows = connection.execute(
            "SELECT category, limit_cents FROM category_limits "
            "WHERE chat_id = ? ORDER BY category",
            (chat_id,),
        ).fetchall()
    if not rows:
        return "Лимиты не заданы. Например: /limit Еда 20000"
    return "Лимиты на месяц:\n" + "\n".join(
        f"• {row['category']}: {money(row['limit_cents'])}" for row in rows
    )


def category_limit_warning(chat_id, category, spent_on):
    category = normalize_category(category)
    with db() as connection:
        row = connection.execute(
            "SELECT limit_cents FROM category_limits "
            "WHERE chat_id = ? AND category = ?",
            (chat_id, category),
        ).fetchone()
    if not row:
        return None
    month_start = spent_on.replace(day=1)
    month_end = spent_on.replace(
        day=calendar.monthrange(spent_on.year, spent_on.month)[1]
    )
    total = next(
        (
            item["total"]
            for item in category_totals(chat_id, month_start, month_end, 1000)
            if normalize_category(item["category"]) == category
        ),
        0,
    )
    limit_cents = row["limit_cents"]
    if total >= limit_cents:
        return f"⚠️ Лимит «{category}» превышен: {money(total)} из {money(limit_cents)}"
    if total * 100 >= limit_cents * 80:
        return f"⚠️ Использовано {round(total * 100 / limit_cents)}% лимита «{category}»: {money(total)} из {money(limit_cents)}"
    return None


def add_recurring(chat_id, day_of_month, category, comment, amount_cents, place=""):
    with db() as connection:
        cursor = connection.execute(
            """
            INSERT INTO recurring_expenses(
                chat_id, category, place, comment, amount_cents, day_of_month
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (chat_id, normalize_category(category), normalize_place(place), comment, amount_cents, day_of_month),
        )
        return cursor.lastrowid


def recurring_message(chat_id):
    with db() as connection:
        rows = connection.execute(
            """
            SELECT id, category, place, comment, amount_cents, day_of_month
            FROM recurring_expenses
            WHERE chat_id = ? AND active = 1 ORDER BY day_of_month, id
            """,
            (chat_id,),
        ).fetchall()
    if not rows:
        return (
            "Регулярных платежей нет.\n"
            "Добавить: /recurring add 5 Интернет домашний 900"
        ), None
    lines = ["Регулярные платежи:"]
    buttons = []
    for row in rows:
        place = f" · {row['place']}" if row["place"] else ""
        comment = f" — {row['comment']}" if row["comment"] else ""
        lines.append(
            f"• {row['day_of_month']}-го: {row['category']}{place}{comment}, {money(row['amount_cents'])}"
        )
        buttons.append(
            [
                {
                    "text": f"Удалить {row['category']} {money(row['amount_cents'])}",
                    "callback_data": f"recurring_delete:{row['id']}",
                }
            ]
        )
    return "\n".join(lines), {"inline_keyboard": buttons}


def process_recurring(user, today):
    month_key = today.strftime("%Y-%m")
    with db() as connection:
        rows = connection.execute(
            """
            SELECT * FROM recurring_expenses
            WHERE chat_id = ? AND active = 1
              AND (last_generated_month IS NULL OR last_generated_month != ?)
            """,
            (user["chat_id"], month_key),
        ).fetchall()
    for row in rows:
        due_day = min(
            row["day_of_month"], calendar.monthrange(today.year, today.month)[1]
        )
        if today.day < due_day:
            continue
        spent_on = date(today.year, today.month, due_day)
        save_expenses(
            user["chat_id"],
            [(row["category"], row["place"], row["comment"], row["amount_cents"])],
            spent_on,
        )
        with db() as connection:
            connection.execute(
                "UPDATE recurring_expenses SET last_generated_month = ? "
                "WHERE chat_id = ? AND id = ?",
                (month_key, user["chat_id"], row["id"]),
            )
        send(
            user["chat_id"],
            f"Автоматически записал регулярный платёж: "
            f"{row['category']} — {money(row['amount_cents'])}",
        )


def add_favorite(chat_id, category, comment, amount_cents, place=""):
    with db() as connection:
        cursor = connection.execute(
            "INSERT INTO favorites(chat_id, category, place, comment, amount_cents) "
            "VALUES (?, ?, ?, ?, ?)",
            (chat_id, normalize_category(category), normalize_place(place), comment, amount_cents),
        )
        return cursor.lastrowid


def favorites_message(chat_id):
    with db() as connection:
        rows = connection.execute(
            "SELECT * FROM favorites WHERE chat_id = ? ORDER BY id",
            (chat_id,),
        ).fetchall()
    if not rows:
        return (
            "Избранных трат нет.\nДобавить: /favorite add Еда бизнес-ланч 600"
        ), None
    buttons = []
    for row in rows:
        title = row["category"]
        if row["place"]:
            title += f" · {row['place']}"
        if row["comment"]:
            title += f" · {row['comment']}"
        buttons.append(
            [
                {
                    "text": f"➕ {title} · {money(row['amount_cents'])}",
                    "callback_data": f"favorite:{row['id']}",
                },
                {"text": "✕", "callback_data": f"favorite_delete:{row['id']}"},
            ]
        )
    return "Нажми, чтобы записать трату на сегодня:", {"inline_keyboard": buttons}


def search_expenses(chat_id, query, limit=20):
    with db() as connection:
        rows = connection.execute(
            """
            SELECT MAX(id) AS id, category, place, comment, SUM(amount_cents) AS total,
                   MIN(spent_on) AS start_day, MAX(spent_on) AS end_day, batch_id
            FROM expenses
            WHERE chat_id = ?
            GROUP BY CASE WHEN batch_id IS NULL THEN 'expense:' || id
                          ELSE 'batch:' || batch_id END
            ORDER BY MAX(created_at) DESC, MAX(id) DESC
            """,
            (chat_id,),
        ).fetchall()
    needle = query.casefold()
    return [
        row for row in rows
        if needle in row["comment"].casefold() or needle in row["place"].casefold()
    ][:limit]


def search_message(chat_id, query):
    rows = search_expenses(chat_id, query)
    if not rows:
        return f"По месту или комментарию «{query}» ничего не найдено.", None
    buttons = []
    for row in rows:
        day = date.fromisoformat(row["start_day"])
        place = f" · {row['place']}" if row["place"] else ""
        buttons.append(
            [
                {
                    "text": f"{day:%d.%m} · {row['category']}{place} · {money(row['total'])}",
                    "callback_data": f"expense:{row['id']}",
                }
            ]
        )
    return f"Найдено по запросу «{query}»:", {"inline_keyboard": buttons}


def export_csv(chat_id):
    with db() as connection:
        rows = connection.execute(
            """
            SELECT id, spent_on, category, place, comment, amount_cents, batch_id, created_at
            FROM expenses WHERE chat_id = ? ORDER BY spent_on, id
            """,
            (chat_id,),
        ).fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        ["id", "date", "category", "place", "comment", "amount_rub", "monthly_batch", "created_at"]
    )
    for row in rows:
        writer.writerow(
            [
                row["id"],
                row["spent_on"],
                row["category"],
                row["place"],
                row["comment"],
                f"{Decimal(row['amount_cents']) / 100:.2f}",
                row["batch_id"] or "",
                row["created_at"],
            ]
        )
    return b"\xef\xbb\xbf" + output.getvalue().encode("utf-8")


def import_csv_bytes(chat_id, content):
    if len(content) > 2_000_000:
        raise ValueError("CSV слишком большой (максимум 2 МБ).")
    digest = hashlib.sha256(content).hexdigest()
    with db() as connection:
        if connection.execute(
            "SELECT 1 FROM imported_files WHERE chat_id = ? AND sha256 = ?", (chat_id, digest)
        ).fetchone():
            return 0
    try:
        reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
        required = {"date", "category", "comment", "amount_rub"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError("Нужен CSV из /export с колонками date, category, comment, amount_rub.")
        rows = []
        now = datetime.utcnow().isoformat()
        for record in reader:
            if len(rows) >= 20000:
                raise ValueError("В CSV слишком много строк (максимум 20 000).")
            try:
                spent_on = date.fromisoformat(record["date"])
                category = normalize_category(record["category"])
                place = normalize_place(record.get("place") or "")
                comment = (record["comment"] or "").strip()
                amount = Decimal(record["amount_rub"])
                if not amount.is_finite() or amount.as_tuple().exponent < -2:
                    raise ValueError
                cents = int(amount * 100)
            except (ValueError, TypeError, InvalidOperation, OverflowError) as error:
                raise ValueError(f"Ошибка в строке {len(rows) + 2} CSV.") from error
            if not category or len(category) > 60 or len(place) > 60 or len(comment) > 300 or cents == 0:
                raise ValueError(f"Ошибка в строке {len(rows) + 2} CSV.")
            imported_batch = record.get("monthly_batch")
            if imported_batch:
                imported_batch = f"import:{digest[:12]}:{imported_batch}"
            rows.append((chat_id, category, place, comment, cents, spent_on.isoformat(), now, imported_batch))
    except UnicodeDecodeError as error:
        raise ValueError("CSV должен быть в UTF-8.") from error
    with db() as connection:
        connection.executemany(
            """INSERT INTO expenses(chat_id, category, place, comment, amount_cents, spent_on, created_at, batch_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", rows,
        )
        connection.execute(
            "INSERT INTO imported_files(chat_id, sha256) VALUES (?, ?)", (chat_id, digest)
        )
    return len(rows)


def handle_import_document(chat_id, document):
    if document.get("file_size", 0) > 2_000_000:
        send(chat_id, "CSV слишком большой (максимум 2 МБ).")
        return
    if not document.get("file_name", "").lower().endswith(".csv"):
        send(chat_id, "Пришли CSV-файл из /export.")
        return
    file_info = api("getFile", file_id=document["file_id"])
    if not file_info or not file_info.get("file_path"):
        send(chat_id, "Не удалось скачать файл. Попробуй ещё раз.")
        return
    path = urllib.parse.quote(file_info["file_path"], safe="/")
    url = f"https://api.telegram.org/file/bot{TOKEN}/{path}"
    try:
        with urllib.request.urlopen(url, timeout=35) as response:
            content = response.read(2_000_001)
        count = import_csv_bytes(chat_id, content)
    except (urllib.error.URLError, TimeoutError) as error:
        print("Telegram file download failed:", error, file=sys.stderr)
        send(chat_id, "Не удалось скачать файл. Попробуй ещё раз.")
        return
    except ValueError as error:
        send(chat_id, str(error))
        return
    send(chat_id, f"Импортировал {count} записей." if count else "Этот CSV уже импортирован — повторно не добавляю.")


def budget_snapshot(user, today):
    month_start = today.replace(day=1)
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    month_end = today.replace(day=days_in_month)
    week_end = min(today + timedelta(days=6 - today.weekday()), month_end)
    month_spent = spent(user["chat_id"], month_start, today)
    remaining = max(user["budget_cents"] - month_spent, 0)
    days_left = max((month_end - today).days + 1, 1)
    daily = remaining // days_left
    week_left = daily * ((week_end - today).days + 1)
    return daily, week_left, remaining


def budget_snapshot_lines(user, today):
    daily, week_left, month_left = budget_snapshot(user, today)
    return [
        f"На сегодня: {money(daily)}",
        f"На неделю осталось: {money(week_left)}",
        f"До конца месяца: {money(month_left)}",
    ]


def budget_status_text(user, today):
    month_start = today.replace(day=1)
    month_spent = spent(user["chat_id"], month_start, today)
    lines = [
        f"Бюджет на {today:%d.%m.%Y}",
        f"Потрачено в этом месяце: {money(month_spent)}",
    ]
    if not user["budget_cents"]:
        return "\n".join([*lines, "Бюджет на месяц не задан. Установи его: /budget 70000"])
    return "\n".join([
        *lines,
        f"Бюджет на месяц: {money(user['budget_cents'])}",
        *budget_snapshot_lines(user, today),
    ])


def morning_text(user, today):
    lines = ["Доброе утро!", *budget_snapshot_lines(user, today)]
    last_week = today - timedelta(days=6)
    top = category_totals(user["chat_id"], last_week, today, 1)
    if top:
        lines.append(f"Факт: за 7 дней больше всего ушло на «{top[0]['category']}» — {money(top[0]['total'])}.")
    return "\n".join(lines)


def parse_report_month(value, calendar_today):
    match = re.fullmatch(r"(0?[1-9]|1[0-2])\.(\d{4})", value)
    if not match:
        raise ValueError("Формат месяца: /report 10.2026")
    month = date(int(match.group(2)), int(match.group(1)), 1)
    if month > calendar_today.replace(day=1):
        raise ValueError("Будущий месяц пока недоступен.")
    return month


def monthly_report_text(user, month_start):
    """Report one complete calendar month, including monthly allocations."""
    month_end = month_start.replace(
        day=calendar.monthrange(month_start.year, month_start.month)[1]
    )
    chat_id = user["chat_id"]
    total = spent(chat_id, month_start, month_end)
    days_in_month = month_end.day
    is_current_month = month_start == user_calendar_today(user).replace(day=1)
    zero_through = min(user_today(user), month_end) if is_current_month else month_end
    text = period_report_text(chat_id, month_start, month_end, zero_through=zero_through)
    lines = text.splitlines()
    lines[0] = f"Календарный месяц: {month_start:%m.%Y} ({month_start:%d.%m}–{month_end:%d.%m})"
    lines.insert(2, f"В среднем в неделю: {money(total * 7 // days_in_month)}")
    if is_current_month:
        # Future monthly allocations remain in the full-month total, but they
        # must not dilute or inflate the average of days recorded so far.
        observed_end = min(user_today(user), month_end)
        observed_rows = daily_totals(chat_id, month_start, observed_end)
        observed_days = len(observed_rows)
        average_day = sum(row["total"] for row in observed_rows) // observed_days if observed_days else 0
        lines[2] = f"В среднем в неделю: {money(average_day * 7)}"
        lines[3] = f"В среднем в день: {money(average_day)}"
        lines.insert(4, f"В среднем в месяц (прогноз на 30 дней): {money(average_day * 30)}")
    if is_current_month and user["budget_cents"]:
        lines.extend(["", f"Остаток месячного бюджета: {money(user['budget_cents'] - total)}"])
    if is_current_month and user["savings_goal_cents"]:
        profit = salary_month_total(chat_id, month_start) + income_total(chat_id, month_start, month_end) - total
        lines.append(f"После цели накоплений: {money(profit - user['savings_goal_cents'])}")
    return "\n".join(lines)


def report_text(user):
    calendar_today = user_calendar_today(user)
    if calendar_today >= MONTHLY_REPORT_START:
        return monthly_report_text(user, calendar_today.replace(day=1))
    today = user_today(user)
    month_start = today.replace(day=1)
    current_total = spent(user["chat_id"], month_start, today)
    with db() as connection:
        first = connection.execute(
            "SELECT MIN(spent_on) AS first_day FROM expenses WHERE chat_id = ?",
            (user["chat_id"],),
        ).fetchone()["first_day"]
        all_total = connection.execute(
            """
            SELECT COALESCE(SUM(amount_cents), 0) AS total
            FROM expenses WHERE chat_id = ? AND spent_on <= ?
            """,
            (user["chat_id"], today.isoformat()),
        ).fetchone()["total"]
    salary_lines = []
    if salary_month_total(user["chat_id"], today):
        salary_lines = [
            f"Зарплата за месяц: {money(salary_month_total(user['chat_id'], today))}",
            f"Прибыль за месяц (зарплата − траты на сегодня): {money(monthly_balance(user['chat_id'], today))}",
        ]
    extra_income = income_total(user["chat_id"], month_start, today)
    if extra_income:
        salary_lines.append(f"Другие доходы за месяц: {money(extra_income)}")
        if not salary_month_total(user["chat_id"], today):
            salary_lines.append(f"Прибыль (другие доходы − траты): {money(extra_income - current_total)}")
    if not first:
        return "\n".join(
            ["Пока нет ни одной траты. Напиши, например: еда 500, транспорт 250"]
            + salary_lines
        )

    first_day = date.fromisoformat(first)
    observed_days = max((today - first_day).days + 1, 1)
    average_day = all_total // observed_days
    average_week = average_day * 7
    average_month = average_day * 30
    top = category_totals(user["chat_id"], month_start, today)
    days = daily_totals(user["chat_id"], month_start, today)
    lines = [
        f"Траты за текущий месяц: {money(current_total)}",
        f"В среднем в день: {money(average_day)}",
        f"В среднем в неделю: {money(average_week)}",
        f"В среднем в месяц: {money(average_month)}",
        "",
        "По дням:",
    ]
    for row in days:
        day_label = date.fromisoformat(row["spent_on"]).strftime("%d.%m")
        lines.append(f"• {day_label} — {money(row['total'])}")
    lines.extend([
        "",
        "Главные категории месяца:",
    ])
    for row in top:
        share = round(row["total"] * 100 / current_total) if current_total else 0
        lines.append(f"• {row['category']}: {money(row['total'])} ({share}%)")
    if user["budget_cents"]:
        lines.extend(["", f"Остаток бюджета: {money(max(user['budget_cents'] - current_total, 0))}"])
    if salary_lines:
        lines.extend(["", *salary_lines])
    return "\n".join(lines)


def help_text():
    return (
        "Первое слово — категория, второе — место, дальше комментарий, в конце сумма:\n"
        "еда Кафе обед с Колей 850\n"
        "Если место не нужно: еда 500\n\n"
        "Несколько трат разделяй запятыми:\n"
        "еда 500, супермаркеты 2300, транспорт 250\n\n"
        "Чтобы указать дату, поставь ее в начале сообщения:\n"
        "23.09 еда 500, транспорт 250\n\n"
        "Или укажи разные даты после каждой траты:\n"
        "еда 500 23.09, транспорт 250 24.09\n\n"
        "Чтобы распределить трату по дням месяца:\n"
        "зал 2500 ЕЖЕМЕСЯЧНО\n\n"
        "Категория может быть любой. Сумму пиши без пробелов, дробную — через точку.\n\n"
        "Команды:\n"
        "/budget 70000 — бюджет на месяц\n"
        "/status — расходы и остаток бюджета на сегодня\n"
        "/salary 100000 — зарплата за месяц (0 — убрать)\n"
        "/income подработка 5000 — другой доход; /incomes — список\n"
        "/refund 123 800 — возврат покупки #123 (номер в /history)\n"
        "/goal 20000 — цель накоплений; /free — свободные деньги\n"
        "/plan 15.10 стоматолог 15000 — будущая трата; /plans — список\n"
        "/report — с октября отчёт за календарный месяц\n"
        "/report 10.2026 — конкретный месяц\n"
        "/report 01.09 15.09 — отчет за период\n"
        "/category Еда — где больше всего тратишь в категории\n"
        "/category Еда 09.2026 — категория за другой месяц\n"
        "/history — последние покупки и комментарии\n"
        "/limits — лимиты категорий\n"
        "/limit Еда 20000 — задать лимит\n"
        "/recurring — регулярные платежи\n"
        "/favorites — быстрые траты\n"
        "/search текст — поиск по месту и комментариям\n"
        "/compare — сравнить недели\n"
        "/compare_months — сравнить месяцы; /week — прошлая неделя\n"
        "/export — выгрузить CSV\n"
        "/import — загрузить CSV из экспорта\n"
        "/settings — время и часовой пояс\n"
        "/undo — удалить последнюю запись\n"
        "/zero — сегодня без трат\n"
        "/help — эта подсказка"
    )


def mark_zero(chat_id, day):
    with db() as connection:
        connection.execute(
            """
            INSERT INTO day_status(chat_id, day, acknowledged, midnight_pending)
            VALUES (?, ?, 1, 0)
            ON CONFLICT(chat_id, day) DO UPDATE SET acknowledged = 1, midnight_pending = 0
            """,
            (chat_id, day.isoformat()),
        )


def handle_message(message):
    if not is_allowed(message):
        return
    chat_id = message["chat"]["id"]
    text = (message.get("text") or message.get("caption") or "").strip()
    ensure_user(chat_id)
    user = get_user(chat_id)

    if message.get("document"):
        if text == "/import":
            handle_import_document(chat_id, message["document"])
        else:
            send(chat_id, "Чтобы импортировать CSV, пришли файл с подписью /import.")
        return

    if text in ("/start", "/help"):
        send(chat_id, "Готово. Я буду записывать твои траты.\n\n" + help_text())
        return
    if text == "/status":
        send(chat_id, budget_status_text(user, user_today(user)))
        return
    if text == "/category" or text.startswith("/category "):
        pieces = text.split()
        if len(pieces) not in (2, 3):
            send(chat_id, "Напиши /category Еда или /category Еда 09.2026")
            return
        calendar_today = user_calendar_today(user)
        try:
            month = (
                parse_report_month(pieces[2], calendar_today)
                if len(pieces) == 3 else calendar_today.replace(day=1)
            )
        except ValueError as error:
            send(chat_id, str(error))
            return
        send(chat_id, category_report_text(chat_id, pieces[1], month))
        return
    if text.startswith("/budget"):
        pieces = text.split()
        if len(pieces) != 2 or not pieces[1].isdigit() or int(pieces[1]) <= 0:
            send(chat_id, "Напиши бюджет так: /budget 70000")
            return
        cents = int(pieces[1]) * 100
        with db() as connection:
            connection.execute("UPDATE users SET budget_cents = ? WHERE chat_id = ?", (cents, chat_id))
        send(chat_id, f"Месячный бюджет сохранен: {money(cents)}")
        return
    if text.startswith("/salary"):
        pieces = text.split()
        if len(pieces) != 2 or not re.fullmatch(r"\d+(?:\.\d{1,2})?", pieces[1]):
            send(chat_id, "Напиши зарплату за месяц так: /salary 100000 (или /salary 0, чтобы убрать)")
            return
        cents = int(Decimal(pieces[1]) * 100)
        with db() as connection:
            already_had_salary = connection.execute(
                "SELECT 1 FROM salary_history WHERE chat_id = ? LIMIT 1", (chat_id,)
            ).fetchone()
            effective_day = user_today(user)
            if not already_had_salary and cents:
                effective_day = effective_day.replace(day=1)
            connection.execute(
                "UPDATE users SET salary_cents = ? WHERE chat_id = ?", (cents, chat_id)
            )
            connection.execute(
                """INSERT INTO salary_history(chat_id, effective_from, salary_cents)
                   VALUES (?, ?, ?) ON CONFLICT(chat_id, effective_from)
                   DO UPDATE SET salary_cents = excluded.salary_cents""",
                (chat_id, effective_day.isoformat(), cents),
            )
        send(
            chat_id,
            f"Зарплата за месяц сохранена: {money(cents)}" if cents else "Зарплата удалена.",
        )
        return
    if text.startswith("/income "):
        body = text[len("/income "):].strip()
        given_date = DATE_PREFIX_RE.match(body)
        received_on = user_today(user)
        if given_date:
            try:
                received_on = parse_date(given_date.group(1), received_on, datetime.now(ZoneInfo(user["timezone"])).date())
            except ValueError as error:
                send(chat_id, str(error))
                return
            body = given_date.group(2)
        parts = body.rsplit(maxsplit=1)
        if len(parts) != 2 or not parts[0].strip():
            send(chat_id, "Формат: /income подработка 5000 или /income 23.09 подработка 5000")
            return
        try:
            cents = parse_money(parts[1])
        except ValueError as error:
            send(chat_id, str(error))
            return
        with db() as connection:
            connection.execute(
                "INSERT INTO income(chat_id, description, amount_cents, received_on) VALUES (?, ?, ?, ?)",
                (chat_id, parts[0].strip()[:300], cents, received_on.isoformat()),
            )
        send(chat_id, f"Записал доход за {received_on:%d.%m}: {parts[0]} — {money(cents)}")
        return
    if text == "/incomes":
        send(chat_id, incomes_text(chat_id))
        return
    if text.startswith("/income_delete "):
        part = text.split(maxsplit=1)[1]
        if not part.isdigit():
            send(chat_id, "Формат: /income_delete ID")
            return
        with db() as connection:
            deleted = connection.execute(
                "DELETE FROM income WHERE chat_id = ? AND id = ?", (chat_id, int(part))
            ).rowcount
        send(chat_id, "Доход удалён." if deleted else "Доход не найден.")
        return
    if text.startswith("/refund "):
        parts = text[len("/refund "):].rsplit(maxsplit=1)
        if len(parts) != 2 or not parts[0].strip():
            send(chat_id, "Формат: /refund Еда 800")
            return
        try:
            cents = parse_money(parts[1])
        except ValueError as error:
            send(chat_id, str(error))
            return
        reference = parts[0].lstrip("#")
        comment = "Возврат"
        place = ""
        if reference.isdigit():
            details = expense_details(chat_id, int(reference))
            if not details or details["total"] <= 0:
                send(chat_id, "Покупка не найдена. Открой /history и возьми номер покупки.")
                return
            with db() as connection:
                previous_refunds = connection.execute(
                    """SELECT COALESCE(-SUM(amount_cents), 0) AS total FROM expenses
                       WHERE chat_id = ? AND comment = ? AND amount_cents < 0""",
                    (chat_id, f"Возврат #{reference}"),
                ).fetchone()["total"]
            if previous_refunds + cents > details["total"]:
                send(chat_id, "Возврат не может быть больше суммы покупки.")
                return
            category = details["category"]
            place = details["place"]
            comment = f"Возврат #{reference}"
        else:
            category = normalize_category(parts[0])
        if len(category) > 60:
            send(chat_id, "Категория слишком длинная.")
            return
        today = user_today(user)
        save_expenses(chat_id, [(category, place, comment, -cents)], today)
        send(chat_id, f"Записал возврат за {today:%d.%m}: {category} −{money(cents)}")
        return
    if text.startswith("/goal"):
        parts = text.split()
        if len(parts) != 2:
            send(chat_id, "Формат: /goal 20000 (или /goal 0, чтобы убрать)")
            return
        try:
            cents = parse_money(parts[1], allow_zero=True)
        except ValueError as error:
            send(chat_id, str(error))
            return
        with db() as connection:
            connection.execute("UPDATE users SET savings_goal_cents = ? WHERE chat_id = ?", (cents, chat_id))
        send(chat_id, f"Цель накоплений: {money(cents)} в месяц.\n" + free_to_spend_text(get_user(chat_id), user_today(user)))
        return
    if text == "/free":
        send(chat_id, free_to_spend_text(user, user_today(user)))
        return
    if text.startswith("/plan_delete "):
        part = text.split(maxsplit=1)[1]
        if not part.isdigit():
            send(chat_id, "Формат: /plan_delete ID")
            return
        with db() as connection:
            deleted = connection.execute(
                "DELETE FROM planned_expenses WHERE chat_id = ? AND id = ?", (chat_id, int(part))
            ).rowcount
        send(chat_id, "План удалён." if deleted else "План не найден.")
        return
    if text in ("/plans", "/plan"):
        send(chat_id, plans_text(chat_id, user_today(user)))
        return
    if text.startswith("/plan "):
        match = re.fullmatch(rf"/plan\s+({DATE_TEXT})\s+(.+)\s+(\d+(?:\.\d{{1,2}})?)", text)
        if not match:
            send(chat_id, "Формат: /plan 15.10 стоматолог 15000")
            return
        try:
            pieces = match.group(1).split(".")
            year = int(pieces[2]) if len(pieces) == 3 else user_today(user).year
            if year < 100:
                year += 2000
            planned_on = date(year, int(pieces[1]), int(pieces[0]))
            cents = parse_money(match.group(3))
        except ValueError:
            send(chat_id, "Неверная дата или сумма.")
            return
        if planned_on < user_today(user):
            send(chat_id, "Планируемая дата уже прошла.")
            return
        with db() as connection:
            connection.execute(
                "INSERT INTO planned_expenses(chat_id, description, amount_cents, planned_on) VALUES (?, ?, ?, ?)",
                (chat_id, match.group(2).strip()[:300], cents, planned_on.isoformat()),
            )
        send(chat_id, f"Запланировал на {planned_on:%d.%m.%Y}: {match.group(2)} — {money(cents)}")
        return
    if text == "/settings":
        send(chat_id, f"Часовой пояс: {user['timezone']}\nДень заканчивается в {user['day_cutoff_hour']:02d}:00\nНапоминания: {user['reminder_hours']}\nБюджет: {user['morning_hour']:02d}:00; прибыль: {user['profit_hour']:02d}:00\nИзменить: /settings cutoff 4, /settings reminders 20 22 0, /settings morning 10, /settings profit 10, /settings timezone Asia/Tomsk")
        return
    if text.startswith("/settings "):
        parts = text.split()
        key = parts[1] if len(parts) > 1 else ""
        field = {"cutoff": "day_cutoff_hour", "morning": "morning_hour", "profit": "profit_hour"}.get(key)
        if field and len(parts) == 3 and parts[2].isdigit() and 0 <= int(parts[2]) <= 23:
            with db() as connection:
                connection.execute(f"UPDATE users SET {field} = ? WHERE chat_id = ?", (int(parts[2]), chat_id))
            send(chat_id, "Настройку сохранил.")
            return
        if key == "reminders" and len(parts) == 5 and len(set(parts[2:])) == 3 and all(p.isdigit() and 0 <= int(p) <= 23 for p in parts[2:]):
            with db() as connection:
                connection.execute("UPDATE users SET reminder_hours = ? WHERE chat_id = ?", (",".join(parts[2:]), chat_id))
            send(chat_id, "Время трёх напоминаний сохранил.")
            return
        if key == "timezone" and len(parts) == 3:
            try:
                ZoneInfo(parts[2])
            except (KeyError, ValueError):
                send(chat_id, "Неизвестный часовой пояс. Пример: Asia/Tomsk")
                return
            with db() as connection:
                connection.execute("UPDATE users SET timezone = ? WHERE chat_id = ?", (parts[2], chat_id))
            send(chat_id, "Часовой пояс сохранил.")
            return
        send(chat_id, "Смотри форматы команд в /settings.")
        return
    if text.startswith("/report"):
        pieces = text.split()
        calendar_today = user_calendar_today(user)
        calendar_month = calendar_today.replace(day=1)
        if len(pieces) == 1:
            keyboard = (
                report_keyboard(calendar_month, calendar_month)
                if calendar_today >= MONTHLY_REPORT_START else report_keyboard()
            )
            send(chat_id, report_text(user), keyboard)
            return
        if len(pieces) == 2:
            try:
                month = parse_report_month(pieces[1], calendar_today)
            except ValueError as error:
                send(chat_id, str(error))
                return
            send(chat_id, monthly_report_text(user, month), report_keyboard(month, calendar_month))
            return
        if len(pieces) != 3:
            send(chat_id, "Формат: /report 10.2026 или /report 01.09 15.09")
            return
        today = user_today(user)
        try:
            start = parse_date(pieces[1], today, calendar_today)
            end = parse_date(pieces[2], today, calendar_today)
        except ValueError as error:
            send(chat_id, str(error))
            return
        if start > end:
            send(chat_id, "Начальная дата должна быть раньше конечной.")
            return
        send(chat_id, period_report_text(chat_id, start, end), report_keyboard())
        return
    if text == "/compare":
        send(chat_id, comparison_text(chat_id, user_today(user)))
        return
    if text == "/compare_months":
        send(chat_id, month_comparison_text(chat_id, user_today(user)))
        return
    if text == "/week":
        today = user_today(user)
        monday = today - timedelta(days=today.weekday())
        send(chat_id, weekly_digest_text(chat_id, monday))
        return
    if text == "/history":
        history_text, keyboard = history_message(chat_id)
        send(chat_id, history_text, keyboard)
        return
    if text == "/limits":
        send(chat_id, category_limits_text(chat_id))
        return
    if text.startswith("/limit_delete "):
        category = normalize_category(text.split(maxsplit=1)[1])
        with db() as connection:
            cursor = connection.execute(
                "DELETE FROM category_limits WHERE chat_id = ? AND category = ?",
                (chat_id, category),
            )
        send(chat_id, "Лимит удалён." if cursor.rowcount else "Такого лимита нет.")
        return
    if text.startswith("/limit "):
        pieces = text.split()
        if len(pieces) != 3:
            send(chat_id, "Формат: /limit Еда 20000")
            return
        try:
            cents = int(Decimal(pieces[2]) * 100)
        except InvalidOperation:
            cents = 0
        if cents <= 0:
            send(chat_id, "Лимит должен быть больше нуля.")
            return
        set_category_limit(chat_id, pieces[1], cents)
        send(
            chat_id,
            f"Лимит «{normalize_category(pieces[1])}» установлен: {money(cents)} в месяц.",
        )
        return
    if text == "/recurring":
        recurring_text, keyboard = recurring_message(chat_id)
        send(chat_id, recurring_text, keyboard)
        return
    if text.startswith("/recurring add "):
        pieces = text.split(maxsplit=3)
        if len(pieces) != 4 or not pieces[2].isdigit():
            send(chat_id, "Формат: /recurring add 5 Интернет домашний 900")
            return
        day_of_month = int(pieces[2])
        if not 1 <= day_of_month <= 31:
            send(chat_id, "День месяца должен быть от 1 до 31.")
            return
        try:
            parsed = parse_expenses(pieces[3])
        except ValueError as error:
            send(chat_id, str(error))
            return
        if len(parsed) != 1:
            send(chat_id, "Добавляй по одному регулярному платежу.")
            return
        category, place, comment, cents = parsed[0]
        add_recurring(chat_id, day_of_month, category, comment, cents, place=place)
        send(
            chat_id,
            f"Добавил регулярный платёж {day_of_month}-го числа: "
            f"{category} — {money(cents)}.",
        )
        return
    if text in ("/favorites", "/favorite"):
        favorites_text, keyboard = favorites_message(chat_id)
        send(chat_id, favorites_text, keyboard)
        return
    if text.startswith("/favorite add "):
        try:
            parsed = parse_expenses(text[len("/favorite add ") :])
        except ValueError as error:
            send(chat_id, str(error))
            return
        if len(parsed) != 1:
            send(chat_id, "Добавляй по одной избранной трате.")
            return
        category, place, comment, cents = parsed[0]
        add_favorite(chat_id, category, comment, cents, place=place)
        send(chat_id, f"Добавил в избранное: {category} — {money(cents)}.")
        return
    if text.startswith("/search "):
        query = text.split(maxsplit=1)[1].strip()
        if not query:
            send(chat_id, "Формат: /search такси")
            return
        search_text, keyboard = search_message(chat_id, query)
        send(chat_id, search_text, keyboard)
        return
    if text == "/export":
        content = export_csv(chat_id)
        send_document(
            chat_id,
            f"expenses-{date.today().isoformat()}.csv",
            content,
            "Все твои траты в CSV",
        )
        return
    if text == "/import":
        send(chat_id, "Пришли CSV из /export как документ с подписью /import. Один и тот же файл повторно не загрузится.")
        return
    if text == "/cancel":
        clear_pending_action(chat_id)
        send(chat_id, "Редактирование отменено.")
        return
    if text == "/undo":
        with db() as connection:
            last = connection.execute(
                """
                SELECT id, category, amount_cents, batch_id
                FROM expenses WHERE chat_id = ? ORDER BY id DESC LIMIT 1
                """,
                (chat_id,),
            ).fetchone()
        if last:
            deleted = delete_expense(chat_id, last["id"])
            send(chat_id, f"Удалил: {deleted['category']} — {money(deleted['total'])}")
        else:
            send(chat_id, "Удалять нечего.")
        return
    if text == "/zero":
        mark_zero(chat_id, user_today(user))
        send(chat_id, "Отметил: сегодня без трат 👍")
        return
    if text.startswith("/"):
        send(chat_id, help_text())
        return

    today = user_today(user)
    calendar_today = datetime.now(ZoneInfo(user["timezone"])).date()
    pending = get_pending_action(chat_id)
    if pending and pending["action"] == "edit":
        try:
            items = parse_expense_message(text, today, calendar_today)
        except ValueError as error:
            send(chat_id, f"{error}. Отправь исправленную покупку или /cancel.")
            return
        if len(items) != 1:
            send(chat_id, "При редактировании отправь ровно одну покупку или /cancel.")
            return
        replacement = replace_expense(chat_id, pending["expense_id"], items[0], today)
        clear_pending_action(chat_id)
        if not replacement:
            send(chat_id, "Покупка уже не найдена.")
            return
        category, place, comment, cents, spent_on, monthly = replacement
        suffix = " ЕЖЕМЕСЯЧНО" if monthly else ""
        send(
            chat_id,
            f"Обновил: {spent_on:%d.%m.%Y} · {category} · {money(cents)}{suffix}"
            + (f"\nМесто: {place}" if place else "")
            + (f"\nКомментарий: {comment}" if comment else ""),
        )
        return
    try:
        items = parse_expense_message(text, today, calendar_today)
    except ValueError as error:
        send(
            chat_id,
            f"{error}. Формат: категория место комментарий сумма\n"
            "Например: еда Кафе обед с Колей 850, транспорт Такси 250\n"
            "С датой: 23.09 еда Кафе обед 500, транспорт Такси 250",
        )
        return

    by_date = {}
    monthly_items = []
    for category, place, comment, cents, spent_on, monthly in items:
        if monthly:
            monthly_items.append((category, place, comment, cents, spent_on))
        else:
            by_date.setdefault(spent_on, []).append((category, place, comment, cents))
    response = []
    for spent_on in sorted(by_date):
        dated_items = by_date[spent_on]
        save_expenses(chat_id, dated_items, spent_on)
        saved = ", ".join(
            f"{category}{f' · {place}' if place else ''} — {money(cents)}"
            + (f" ({comment})" if comment else "")
            for category, place, comment, cents in dated_items
        )
        day_total = spent(chat_id, spent_on, spent_on)
        label = "сегодня" if spent_on == today else spent_on.strftime("%d.%m.%Y")
        response.append(f"Записал за {label}: {saved}\nВсего за день: {money(day_total)}")
        for category, _place, _comment, _cents in dated_items:
            warning = category_limit_warning(chat_id, category, spent_on)
            if warning and warning not in response:
                response.append(warning)
            if spent_on == today:
                anomaly = anomaly_text(chat_id, category, today)
                if anomaly and anomaly not in response:
                    response.append(anomaly)
    for category, place, comment, cents, spent_on in monthly_items:
        daily_cents, days_in_month = save_monthly_expense(
            chat_id,
            category,
            comment,
            cents,
            spent_on,
            acknowledge=spent_on == today,
            place=place,
        )
        month_label = spent_on.strftime("%m.%Y")
        response.append(
            f"Распределил за {month_label}: {category}{f' · {place}' if place else ''} — {money(cents)}\n"
            f"На {days_in_month} дней: примерно {money(daily_cents)} в день"
        )
        warning = category_limit_warning(chat_id, category, spent_on)
        if warning:
            response.append(warning)
    send(chat_id, "\n\n".join(response))


def handle_callback(callback):
    if not is_allowed(callback):
        return
    chat_id = callback["message"]["chat"]["id"]
    ensure_user(chat_id)
    user = get_user(chat_id)
    data = callback.get("data", "")
    if data.startswith("zero:"):
        day = date.fromisoformat(data.split(":", 1)[1])
        mark_zero(chat_id, day)
        api("answerCallbackQuery", callback_query_id=callback["id"], text="Отметил")
        send(chat_id, "Принято: трат не было 👍")
    elif data.startswith("done:"):
        day = date.fromisoformat(data.split(":", 1)[1])
        mark_zero(chat_id, day)
        api("answerCallbackQuery", callback_query_id=callback["id"], text="Готово")
        api(
            "deleteMessage",
            chat_id=chat_id,
            message_id=callback["message"]["message_id"],
        )
    elif data.startswith("expense:"):
        try:
            expense_id = int(data.split(":", 1)[1])
        except ValueError:
            return
        details = expense_details(chat_id, expense_id)
        if not details:
            api(
                "answerCallbackQuery",
                callback_query_id=callback["id"],
                text="Покупка не найдена",
            )
            return
        api("answerCallbackQuery", callback_query_id=callback["id"])
        send(
            chat_id,
            expense_details_text(details),
            expense_actions_keyboard(details["id"]),
        )
    elif data.startswith("edit:"):
        try:
            expense_id = int(data.split(":", 1)[1])
        except ValueError:
            return
        if not expense_details(chat_id, expense_id):
            api(
                "answerCallbackQuery",
                callback_query_id=callback["id"],
                text="Покупка не найдена",
            )
            return
        set_pending_edit(chat_id, expense_id)
        api("answerCallbackQuery", callback_query_id=callback["id"], text="Жду новую запись")
        send(
            chat_id,
            "Отправь исправленную покупку целиком. Например:\n"
            "Еда Кафе новый комментарий 900 25.09\n\n"
            "Для отмены: /cancel",
        )
    elif data.startswith("delete:"):
        try:
            expense_id = int(data.split(":", 1)[1])
        except ValueError:
            return
        details = expense_details(chat_id, expense_id)
        if not details:
            api(
                "answerCallbackQuery",
                callback_query_id=callback["id"],
                text="Покупка не найдена",
            )
            return
        api("answerCallbackQuery", callback_query_id=callback["id"])
        send(
            chat_id,
            f"Точно удалить «{details['category']}» на {money(details['total'])}?",
            {
                "inline_keyboard": [
                    [
                        {
                            "text": "Да, удалить",
                            "callback_data": f"delete_confirm:{expense_id}",
                        }
                    ]
                ]
            },
        )
    elif data.startswith("delete_confirm:"):
        try:
            expense_id = int(data.split(":", 1)[1])
        except ValueError:
            return
        deleted = delete_expense(chat_id, expense_id)
        api(
            "answerCallbackQuery",
            callback_query_id=callback["id"],
            text="Удалено" if deleted else "Покупка не найдена",
        )
        if deleted:
            send(chat_id, f"Удалил: {deleted['category']} — {money(deleted['total'])}")
    elif data.startswith("report_month:"):
        month_code = data.split(":", 1)[1]
        try:
            month = date.fromisoformat(month_code + "-01")
        except ValueError:
            return
        current_month = user_calendar_today(user).replace(day=1)
        if month > current_month:
            api("answerCallbackQuery", callback_query_id=callback["id"], text="Будущий месяц пока недоступен")
            return
        api("answerCallbackQuery", callback_query_id=callback["id"])
        send(chat_id, monthly_report_text(user, month), report_keyboard(month, current_month))
    elif data.startswith("report:"):
        period = data.split(":", 1)[1]
        today = user_today(user)
        if period == "today":
            start = end = today
            text = period_report_text(chat_id, start, end)
        elif period == "week":
            start = today - timedelta(days=today.weekday())
            text = period_report_text(chat_id, start, today)
        elif period == "month":
            calendar_today = user_calendar_today(user)
            if calendar_today >= MONTHLY_REPORT_START:
                month = calendar_today.replace(day=1)
                text = monthly_report_text(user, month)
            else:
                start = today.replace(day=1)
                text = period_report_text(chat_id, start, today)
        elif period == "compare":
            text = comparison_text(chat_id, today)
        else:
            return
        api("answerCallbackQuery", callback_query_id=callback["id"])
        keyboard = (
            report_keyboard(month, calendar_today.replace(day=1))
            if period == "month" and calendar_today >= MONTHLY_REPORT_START
            else report_keyboard()
        )
        send(chat_id, text, keyboard)
    elif data.startswith("recurring_delete:"):
        try:
            recurring_id = int(data.split(":", 1)[1])
        except ValueError:
            return
        with db() as connection:
            cursor = connection.execute(
                "DELETE FROM recurring_expenses WHERE chat_id = ? AND id = ?",
                (chat_id, recurring_id),
            )
        api(
            "answerCallbackQuery",
            callback_query_id=callback["id"],
            text="Удалено" if cursor.rowcount else "Не найдено",
        )
        if cursor.rowcount:
            send(chat_id, "Регулярный платёж удалён.")
    elif data.startswith("favorite_delete:"):
        try:
            favorite_id = int(data.split(":", 1)[1])
        except ValueError:
            return
        with db() as connection:
            cursor = connection.execute(
                "DELETE FROM favorites WHERE chat_id = ? AND id = ?",
                (chat_id, favorite_id),
            )
        api(
            "answerCallbackQuery",
            callback_query_id=callback["id"],
            text="Удалено" if cursor.rowcount else "Не найдено",
        )
    elif data.startswith("favorite:"):
        try:
            favorite_id = int(data.split(":", 1)[1])
        except ValueError:
            return
        with db() as connection:
            favorite = connection.execute(
                "SELECT category, place, comment, amount_cents FROM favorites "
                "WHERE chat_id = ? AND id = ?",
                (chat_id, favorite_id),
            ).fetchone()
        if not favorite:
            api(
                "answerCallbackQuery",
                callback_query_id=callback["id"],
                text="Не найдено",
            )
            return
        today = user_today(user)
        save_expenses(
            chat_id,
            [(favorite["category"], favorite["place"], favorite["comment"], favorite["amount_cents"])],
            today,
        )
        api("answerCallbackQuery", callback_query_id=callback["id"], text="Записал")
        place = f" · {favorite['place']}" if favorite["place"] else ""
        message = f"Записал: {favorite['category']}{place} — {money(favorite['amount_cents'])}"
        warning = category_limit_warning(chat_id, favorite["category"], today)
        if warning:
            message += "\n" + warning
        send(chat_id, message)


def was_sent(chat_id, day, message_type):
    with db() as connection:
        return bool(connection.execute(
            "SELECT 1 FROM sent_messages WHERE chat_id = ? AND day = ? AND message_type = ?",
            (chat_id, day.isoformat(), message_type),
        ).fetchone())


def mark_sent(chat_id, day, message_type):
    with db() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO sent_messages(chat_id, day, message_type) VALUES (?, ?, ?)",
            (chat_id, day.isoformat(), message_type),
        )


def is_acknowledged(chat_id, day):
    with db() as connection:
        row = connection.execute(
            "SELECT acknowledged FROM day_status WHERE chat_id = ? AND day = ?",
            (chat_id, day.isoformat()),
        ).fetchone()
    return bool(row and row["acknowledged"])


def reminder(chat_id, day, message_type, text, midnight=False):
    if message_type != "reminder20" and is_acknowledged(chat_id, day):
        return
    if was_sent(chat_id, day, message_type):
        return
    if message_type == "reminder20":
        with db() as connection:
            connection.execute(
                """
                INSERT INTO day_status(chat_id, day, acknowledged, midnight_pending)
                VALUES (?, ?, 0, 0)
                ON CONFLICT(chat_id, day) DO UPDATE SET acknowledged = 0
                """,
                (chat_id, day.isoformat()),
            )
    if midnight:
        with db() as connection:
            connection.execute(
                """
                INSERT INTO day_status(chat_id, day, acknowledged, midnight_pending)
                VALUES (?, ?, 0, 1)
                ON CONFLICT(chat_id, day) DO UPDATE SET midnight_pending = 1
                """,
                (chat_id, day.isoformat()),
            )
    keyboard = {
        "inline_keyboard": [
            [{"text": "Вчера без трат" if midnight else "Сегодня без трат", "callback_data": f"zero:{day.isoformat()}"}],
            [{"text": "Уже всё записал", "callback_data": f"done:{day.isoformat()}"}],
        ]
    }
    if send(chat_id, text, keyboard) is not None:
        mark_sent(chat_id, day, message_type)


def send_morning_update(user, today):
    chat_id = user["chat_id"]
    if user["budget_cents"]:
        if not was_sent(chat_id, today, "morning"):
            if send(chat_id, morning_text(user, today)) is not None:
                mark_sent(chat_id, today, "morning")
    elif today.weekday() == 0 and not was_sent(chat_id, today, "budget_weekly"):
        if send(
            chat_id,
            "Месячный бюджет пока не задан. Если нужен утренний расчёт, напиши /budget 70000.",
        ) is not None:
            mark_sent(chat_id, today, "budget_weekly")


def send_daily_profit(user, day):
    monthly_salary = salary_for_day(user["chat_id"], day)
    if not monthly_salary or was_sent(user["chat_id"], day, "daily_profit"):
        return
    days_in_month = calendar.monthrange(day.year, day.month)[1]
    daily_salary_cents = int(
        (Decimal(monthly_salary) / days_in_month).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )
    expenses_cents = spent(user["chat_id"], day, day)
    other_income = income_total(user["chat_id"], day, day)
    message = (
        f"Итог за {day:%d.%m.%Y}:\n"
        f"Зарплата за день: {money(daily_salary_cents)}\n"
        f"Траты: {money(expenses_cents)}\n"
        + (f"Другие доходы: {money(other_income)}\n" if other_income else "")
        + f"Прибыль за день: {money(daily_salary_cents + other_income - expenses_cents)}"
    )
    if send(user["chat_id"], message) is not None:
        mark_sent(user["chat_id"], day, "daily_profit")


def run_schedule():
    with db() as connection:
        users = connection.execute("SELECT * FROM users").fetchall()
    for user in users:
        local_now = datetime.now(ZoneInfo(user["timezone"]))
        today = local_now.date()
        expense_today = business_day(local_now, user["day_cutoff_hour"])
        hour, minute = local_now.hour, local_now.minute
        process_recurring(user, expense_today)
        if hour == max(user["morning_hour"], user["day_cutoff_hour"]) and minute < 5:
            send_morning_update(user, today)
        if hour == max(user["profit_hour"], user["day_cutoff_hour"]) and minute < 5:
            send_daily_profit(user, today - timedelta(days=1))
        if today.weekday() == 0 and hour == max(11, user["day_cutoff_hour"]) and minute < 5:
            week_end = today - timedelta(days=1)
            if not was_sent(user["chat_id"], week_end, "weekly_digest"):
                if send(user["chat_id"], weekly_digest_text(user["chat_id"], today)) is not None:
                    mark_sent(user["chat_id"], week_end, "weekly_digest")
        if minute < 5:
            reminder_hours = [int(value) for value in user["reminder_hours"].split(",")]
            reminder_types = ["reminder20", "reminder22", "reminder00"]
            default_texts = [
                "Запиши сегодняшние траты. Например: еда 500, транспорт 250",
                "Напоминаю про траты 👀 Скинь всё одним сообщением через запятую.",
                "Последний догон за вчера: укажи дату, например «23.09 еда 500», или нажми «Вчера без трат».",
            ]
            for index, reminder_hour in enumerate(reminder_hours):
                if hour == reminder_hour:
                    day = expense_today
                    after_midnight = day != today
                    label = "вчерашние" if after_midnight else "сегодняшние"
                    message = default_texts[index] if reminder_hours == [20, 22, 0] and user["day_cutoff_hour"] == 4 else f"Запиши {label} траты. Например: еда 500, транспорт 250"
                    reminder(user["chat_id"], day, reminder_types[index], message, midnight=after_midnight)


def main():
    if not TOKEN:
        print("Set TELEGRAM_BOT_TOKEN first.", file=sys.stderr)
        raise SystemExit(1)
    if not ALLOWED_USER_IDS:
        print("Set ALLOWED_TELEGRAM_USER_IDS first.", file=sys.stderr)
        raise SystemExit(1)
    ZoneInfo(DEFAULT_TIMEZONE)  # fail early on a typo
    init_db()
    encoded_patch = os.environ.get("EXPENSE_CORRECTIONS_B64", "")
    if encoded_patch:
        count = apply_expense_corrections(
            encoded_patch, os.path.join(os.path.dirname(DB_PATH), "expenses-before-csv-corrections.db")
        )
        print(f"Verified CSV corrections applied: {count} expense(s)", flush=True)
    print("Bot is running. Press Ctrl+C to stop.")
    offset = 0
    while True:
        run_schedule()
        updates = api("getUpdates", offset=offset, timeout=20, allowed_updates=json.dumps(["message", "callback_query"]))
        if updates is None:
            time.sleep(3)
            continue
        for update in updates:
            offset = update["update_id"] + 1
            try:
                if "message" in update:
                    handle_message(update["message"])
                elif "callback_query" in update:
                    handle_callback(update["callback_query"])
            except Exception as error:
                print("Update failed:", update.get("update_id"), repr(error), file=sys.stderr)


if __name__ == "__main__":
    main()
