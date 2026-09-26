#!/usr/bin/env python3
"""A deliberately small Telegram expense tracker with no AI or dependencies."""

import calendar
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
            """
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
    if not user["budget_cents"]:
        return "Доброе утро! Задай месячный бюджет командой /budget 70000."
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
    if not first:
        return "Пока нет ни одной траты. Напиши, например: еда 500, транспорт 250"

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
        "/report — отчет\n"
        "/history — последние покупки и комментарии\n"
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
    if text == "/report":
        send(chat_id, report_text(user))
        return
    if text == "/history":
        history_text, keyboard = history_message(chat_id)
        send(chat_id, history_text, keyboard)
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
                if last["batch_id"]:
                    total = connection.execute(
                        "SELECT SUM(amount_cents) AS total FROM expenses WHERE batch_id = ?",
                        (last["batch_id"],),
                    ).fetchone()["total"]
                    connection.execute(
                        "DELETE FROM expenses WHERE batch_id = ?", (last["batch_id"],)
                    )
                else:
                    total = last["amount_cents"]
                    connection.execute("DELETE FROM expenses WHERE id = ?", (last["id"],))
        if last:
            send(chat_id, f"Удалил: {last['category']} — {money(total)}")
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
        send(chat_id, expense_details_text(details))


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


def run_schedule():
    with db() as connection:
        users = connection.execute("SELECT * FROM users").fetchall()
    for user in users:
        local_now = datetime.now(ZoneInfo(user["timezone"]))
        today = local_now.date()
        hour, minute = local_now.hour, local_now.minute
        if hour == MORNING_HOUR and minute < 5 and not already_sent(user["chat_id"], today, "morning"):
            send(user["chat_id"], morning_text(user, today))
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
