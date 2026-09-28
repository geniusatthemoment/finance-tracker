#!/usr/bin/env python3
"""A deliberately small Telegram expense tracker with no AI or dependencies."""

import calendar
import csv
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
from decimal import Decimal, InvalidOperation
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
MORNING_HOUR = int(os.environ.get("MORNING_HOUR", "9"))
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
                comment TEXT NOT NULL DEFAULT '',
                amount_cents INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS pending_actions (
                chat_id INTEGER PRIMARY KEY,
                action TEXT NOT NULL,
                expense_id INTEGER NOT NULL
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
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(expenses)").fetchall()
        }
        if "batch_id" not in columns:
            connection.execute("ALTER TABLE expenses ADD COLUMN batch_id TEXT")
        if "comment" not in columns:
            connection.execute(
                "ALTER TABLE expenses ADD COLUMN comment TEXT NOT NULL DEFAULT ''"
            )
        rows = connection.execute("SELECT id, category FROM expenses").fetchall()
        connection.executemany(
            "UPDATE expenses SET category = ? WHERE id = ?",
            [(normalize_category(row["category"]), row["id"]) for row in rows],
        )


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


def ensure_user(chat_id):
    with db() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO users(chat_id, timezone, created_at) VALUES (?, ?, ?)",
            (chat_id, DEFAULT_TIMEZONE, datetime.utcnow().isoformat()),
        )


def is_allowed(update_part):
    sender = update_part.get("from") or {}
    return sender.get("id") in ALLOWED_USER_IDS


def normalize_category(category):
    return " ".join(category.split()).casefold().capitalize()


def user_today(user):
    return datetime.now(ZoneInfo(user["timezone"])).date()


def get_user(chat_id):
    with db() as connection:
        return connection.execute(
            "SELECT * FROM users WHERE chat_id = ?", (chat_id,)
        ).fetchone()


def parse_expenses(text):
    """Parse `category [comment] amount` using comma as the item separator."""
    parts = [part.strip() for part in text.split(",") if part.strip()]
    if not parts:
        raise ValueError("Пустое сообщение")

    parsed = []
    for part in parts:
        match = EXPENSE_RE.match(part)
        if not match:
            raise ValueError(f"Не понял: «{part}»")
        category = normalize_category(match.group(1))
        comment = " ".join((match.group(2) or "").split())
        if not category or len(category) > 60 or any(char.isdigit() for char in category):
            raise ValueError(f"Некорректная категория: «{category}»")
        if len(comment) > 300:
            raise ValueError("Комментарий слишком длинный")
        try:
            amount = Decimal(match.group(3))
        except InvalidOperation:
            raise ValueError(f"Некорректная сумма: «{match.group(3)}»")
        cents = int(amount * 100)
        if cents <= 0:
            raise ValueError("Сумма должна быть больше нуля")
        parsed.append((category, comment, cents))
    return parsed


def parse_date(value, today):
    pieces = value.split(".")
    day, month = int(pieces[0]), int(pieces[1])
    year = today.year if len(pieces) == 2 else int(pieces[2])
    if year < 100:
        year += 2000
    try:
        result = date(year, month, day)
    except ValueError:
        raise ValueError(f"Некорректная дата: «{value}»")
    if result > today:
        raise ValueError("Нельзя записать трату на будущую дату")
    return result


def parse_expense_message(text, today):
    """Parse expenses with an optional global prefix or per-item date suffix."""
    global_date = None
    prefix = DATE_PREFIX_RE.match(text.strip())
    if prefix:
        global_date = parse_date(prefix.group(1), today)
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
                spent_on = parse_date(suffix.group(2), today)
        parsed = parse_expenses(part)
        category, comment, cents = parsed[0]
        result.append((category, comment, cents, spent_on, monthly))
    return result


def save_expenses(chat_id, items, spent_on):
    now = datetime.utcnow().isoformat()
    with db() as connection:
        connection.executemany(
            """
            INSERT INTO expenses(
                chat_id, category, comment, amount_cents, spent_on, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    chat_id,
                    normalize_category(category),
                    comment,
                    cents,
                    spent_on.isoformat(),
                    now,
                )
                for category, comment, cents in items
            ],
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
    chat_id, category, comment, amount_cents, month_date, acknowledge=False
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
                chat_id, category, comment, amount_cents, spent_on, created_at, batch_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
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
            SELECT id, category, comment, amount_cents, spent_on, batch_id
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
        label = f"{start_day:%d.%m} · {row['category']} · {money(row['total'])}"
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
    category, comment, cents, spent_on, monthly = item
    delete_expense(chat_id, expense_id)
    if monthly:
        save_monthly_expense(
            chat_id,
            category,
            comment,
            cents,
            spent_on,
            acknowledge=spent_on == today,
        )
    else:
        save_expenses(chat_id, [(category, comment, cents)], spent_on)
    return category, comment, cents, spent_on, monthly


def period_report_text(chat_id, start, end):
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
    if rows:
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
    return "\n".join(lines)


def report_keyboard():
    return {
        "inline_keyboard": [
            [
                {"text": "Сегодня", "callback_data": "report:today"},
                {"text": "Неделя", "callback_data": "report:week"},
                {"text": "Месяц", "callback_data": "report:month"},
            ],
            [{"text": "Сравнить недели", "callback_data": "report:compare"}],
        ]
    }


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


def add_recurring(chat_id, day_of_month, category, comment, amount_cents):
    with db() as connection:
        cursor = connection.execute(
            """
            INSERT INTO recurring_expenses(
                chat_id, category, comment, amount_cents, day_of_month
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (chat_id, normalize_category(category), comment, amount_cents, day_of_month),
        )
        return cursor.lastrowid


def recurring_message(chat_id):
    with db() as connection:
        rows = connection.execute(
            """
            SELECT id, category, comment, amount_cents, day_of_month
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
        comment = f" — {row['comment']}" if row["comment"] else ""
        lines.append(
            f"• {row['day_of_month']}-го: {row['category']}{comment}, {money(row['amount_cents'])}"
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
            [(row["category"], row["comment"], row["amount_cents"])],
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


def add_favorite(chat_id, category, comment, amount_cents):
    with db() as connection:
        cursor = connection.execute(
            "INSERT INTO favorites(chat_id, category, comment, amount_cents) "
            "VALUES (?, ?, ?, ?)",
            (chat_id, normalize_category(category), comment, amount_cents),
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
            SELECT MAX(id) AS id, category, comment, SUM(amount_cents) AS total,
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
    return [row for row in rows if needle in row["comment"].casefold()][:limit]


def search_message(chat_id, query):
    rows = search_expenses(chat_id, query)
    if not rows:
        return f"По комментарию «{query}» ничего не найдено.", None
    buttons = []
    for row in rows:
        day = date.fromisoformat(row["start_day"])
        buttons.append(
            [
                {
                    "text": f"{day:%d.%m} · {row['category']} · {money(row['total'])}",
                    "callback_data": f"expense:{row['id']}",
                }
            ]
        )
    return f"Найдено по запросу «{query}»:", {"inline_keyboard": buttons}


def export_csv(chat_id):
    with db() as connection:
        rows = connection.execute(
            """
            SELECT id, spent_on, category, comment, amount_cents, batch_id, created_at
            FROM expenses WHERE chat_id = ? ORDER BY spent_on, id
            """,
            (chat_id,),
        ).fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        ["id", "date", "category", "comment", "amount_rub", "monthly_batch", "created_at"]
    )
    for row in rows:
        writer.writerow(
            [
                row["id"],
                row["spent_on"],
                row["category"],
                row["comment"],
                f"{Decimal(row['amount_cents']) / 100:.2f}",
                row["batch_id"] or "",
                row["created_at"],
            ]
        )
    return b"\xef\xbb\xbf" + output.getvalue().encode("utf-8")


def budget_snapshot(user, today):
    month_start = today.replace(day=1)
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    month_end = today.replace(day=days_in_month)
    week_start = today - timedelta(days=today.weekday())
    week_end = min(week_start + timedelta(days=6), month_end)
    month_spent = spent(user["chat_id"], month_start, today)
    week_spent = spent(user["chat_id"], week_start, today)
    remaining = max(user["budget_cents"] - month_spent, 0)
    days_left = max((month_end - today).days + 1, 1)
    daily = remaining // days_left
    week_allowance = user["budget_cents"] * ((week_end - week_start).days + 1) // days_in_month
    week_left = max(week_allowance - week_spent, 0)
    return daily, week_left, remaining


def morning_text(user, today):
    daily, week_left, month_left = budget_snapshot(user, today)
    lines = [
        "Доброе утро!",
        f"На сегодня: {money(daily)}",
        f"На неделю осталось: {money(week_left)}",
        f"До конца месяца: {money(month_left)}",
    ]
    last_week = today - timedelta(days=6)
    top = category_totals(user["chat_id"], last_week, today, 1)
    if top:
        lines.append(f"Факт: за 7 дней больше всего ушло на «{top[0]['category']}» — {money(top[0]['total'])}.")
    return "\n".join(lines)


def report_text(user):
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
    if user["salary_cents"]:
        salary_lines = [
            f"Зарплата за месяц: {money(user['salary_cents'])}",
            f"Прибыль за месяц (зарплата − траты на сегодня): {money(user['salary_cents'] - current_total)}",
        ]
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
        "Первое слово — категория, последнее — сумма, между ними можно написать комментарий:\n"
        "еда обед с Колей 850\n\n"
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
        "/salary 100000 — зарплата за месяц (0 — убрать)\n"
        "/report — отчет и выбор периода\n"
        "/report 01.09 15.09 — отчет за период\n"
        "/history — последние покупки и комментарии\n"
        "/limits — лимиты категорий\n"
        "/limit Еда 20000 — задать лимит\n"
        "/recurring — регулярные платежи\n"
        "/favorites — быстрые траты\n"
        "/search текст — поиск по комментариям\n"
        "/compare — сравнить недели\n"
        "/export — выгрузить CSV\n"
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
    text = message.get("text", "").strip()
    ensure_user(chat_id)
    user = get_user(chat_id)

    if text in ("/start", "/help"):
        send(chat_id, "Готово. Я буду записывать твои траты.\n\n" + help_text())
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
            connection.execute(
                "UPDATE users SET salary_cents = ? WHERE chat_id = ?", (cents, chat_id)
            )
        send(
            chat_id,
            f"Зарплата за месяц сохранена: {money(cents)}" if cents else "Зарплата удалена.",
        )
        return
    if text.startswith("/report"):
        pieces = text.split()
        if len(pieces) == 1:
            send(chat_id, report_text(user), report_keyboard())
            return
        if len(pieces) != 3:
            send(chat_id, "Формат: /report 01.09 15.09")
            return
        today = user_today(user)
        try:
            start = parse_date(pieces[1], today)
            end = parse_date(pieces[2], today)
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
        category, comment, cents = parsed[0]
        add_recurring(chat_id, day_of_month, category, comment, cents)
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
        category, comment, cents = parsed[0]
        add_favorite(chat_id, category, comment, cents)
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
    pending = get_pending_action(chat_id)
    if pending and pending["action"] == "edit":
        try:
            items = parse_expense_message(text, today)
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
        category, comment, cents, spent_on, monthly = replacement
        suffix = " ЕЖЕМЕСЯЧНО" if monthly else ""
        send(
            chat_id,
            f"Обновил: {spent_on:%d.%m.%Y} · {category} · {money(cents)}{suffix}"
            + (f"\nКомментарий: {comment}" if comment else ""),
        )
        return
    try:
        items = parse_expense_message(text, today)
    except ValueError as error:
        send(
            chat_id,
            f"{error}. Формат: категория комментарий сумма\n"
            "Например: еда обед с Колей 850, транспорт такси 250\n"
            "С датой: 23.09 еда обед 500, транспорт такси 250",
        )
        return

    by_date = {}
    monthly_items = []
    for category, comment, cents, spent_on, monthly in items:
        if monthly:
            monthly_items.append((category, comment, cents, spent_on))
        else:
            by_date.setdefault(spent_on, []).append((category, comment, cents))
    response = []
    for spent_on in sorted(by_date):
        dated_items = by_date[spent_on]
        save_expenses(chat_id, dated_items, spent_on)
        saved = ", ".join(
            f"{category} — {money(cents)}"
            + (f" ({comment})" if comment else "")
            for category, comment, cents in dated_items
        )
        day_total = spent(chat_id, spent_on, spent_on)
        label = "сегодня" if spent_on == today else spent_on.strftime("%d.%m.%Y")
        response.append(f"Записал за {label}: {saved}\nВсего за день: {money(day_total)}")
        for category, _comment, _cents in dated_items:
            warning = category_limit_warning(chat_id, category, spent_on)
            if warning and warning not in response:
                response.append(warning)
    for category, comment, cents, spent_on in monthly_items:
        daily_cents, days_in_month = save_monthly_expense(
            chat_id,
            category,
            comment,
            cents,
            spent_on,
            acknowledge=spent_on == today,
        )
        month_label = spent_on.strftime("%m.%Y")
        response.append(
            f"Распределил за {month_label}: {category} — {money(cents)}\n"
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
            "Еда новый комментарий 900 25.09\n\n"
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
            start = today.replace(day=1)
            text = period_report_text(chat_id, start, today)
        elif period == "compare":
            text = comparison_text(chat_id, today)
        else:
            return
        api("answerCallbackQuery", callback_query_id=callback["id"])
        send(chat_id, text, report_keyboard())
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
                "SELECT category, comment, amount_cents FROM favorites "
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
            [(favorite["category"], favorite["comment"], favorite["amount_cents"])],
            today,
        )
        api("answerCallbackQuery", callback_query_id=callback["id"], text="Записал")
        message = f"Записал: {favorite['category']} — {money(favorite['amount_cents'])}"
        warning = category_limit_warning(chat_id, favorite["category"], today)
        if warning:
            message += "\n" + warning
        send(chat_id, message)


def already_sent(chat_id, day, message_type):
    with db() as connection:
        exists = connection.execute(
            "SELECT 1 FROM sent_messages WHERE chat_id = ? AND day = ? AND message_type = ?",
            (chat_id, day.isoformat(), message_type),
        ).fetchone()
        if exists:
            return True
        connection.execute(
            "INSERT INTO sent_messages(chat_id, day, message_type) VALUES (?, ?, ?)",
            (chat_id, day.isoformat(), message_type),
        )
    return False


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
    if already_sent(chat_id, day, message_type):
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
            [{"text": "Сегодня без трат", "callback_data": f"zero:{day.isoformat()}"}],
            [{"text": "Уже всё записал", "callback_data": f"done:{day.isoformat()}"}],
        ]
    }
    send(chat_id, text, keyboard)


def send_morning_update(user, today):
    chat_id = user["chat_id"]
    if user["budget_cents"]:
        if not already_sent(chat_id, today, "morning"):
            send(chat_id, morning_text(user, today))
    elif today.weekday() == 0 and not already_sent(chat_id, today, "budget_weekly"):
        send(
            chat_id,
            "Месячный бюджет пока не задан. Если нужен утренний расчёт, напиши /budget 70000.",
        )


def run_schedule():
    with db() as connection:
        users = connection.execute("SELECT * FROM users").fetchall()
    for user in users:
        local_now = datetime.now(ZoneInfo(user["timezone"]))
        today = local_now.date()
        hour, minute = local_now.hour, local_now.minute
        process_recurring(user, today)
        if hour == MORNING_HOUR and minute < 5:
            send_morning_update(user, today)
        if hour == 20 and minute < 5:
            reminder(
                user["chat_id"], today, "reminder20",
                "Запиши сегодняшние траты. Например: еда 500, транспорт 250",
            )
        if hour == 22 and minute < 5:
            reminder(
                user["chat_id"], today, "reminder22",
                "Напоминаю про траты 👀 Скинь всё одним сообщением через запятую.",
            )
        if hour == 0 and minute < 5:
            yesterday = today - timedelta(days=1)
            reminder(
                user["chat_id"], yesterday, "reminder00",
                "Последний догон за вчера: укажи дату, например «23.09 еда 500», или нажми «Сегодня без трат».",
                midnight=True,
            )


def main():
    if not TOKEN:
        print("Set TELEGRAM_BOT_TOKEN first.", file=sys.stderr)
        raise SystemExit(1)
    if not ALLOWED_USER_IDS:
        print("Set ALLOWED_TELEGRAM_USER_IDS first.", file=sys.stderr)
        raise SystemExit(1)
    ZoneInfo(DEFAULT_TIMEZONE)  # fail early on a typo
    init_db()
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
