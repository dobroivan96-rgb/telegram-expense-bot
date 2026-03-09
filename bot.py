import os
import logging
import re
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ==============================
# CONFIG
# ==============================
BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = "expenses.db"
TIMEZONE = "Europe/Kyiv"

# ==============================
# LOGGING
# ==============================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ==============================
# DB
# ==============================
def get_connection() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


def init_db() -> None:
    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            raw_text TEXT NOT NULL,
            category TEXT NOT NULL,
            amount REAL NOT NULL,
            comment TEXT,
            created_at TEXT NOT NULL
        )
        """
    )

    conn.commit()
    conn.close()


def save_expense(user_id: int, raw_text: str, category: str, amount: float, comment: str | None) -> int:
    conn = get_connection()
    cur = conn.cursor()
    created_at = datetime.now(ZoneInfo(TIMEZONE)).isoformat()

    cur.execute(
        """
        INSERT INTO expenses (user_id, raw_text, category, amount, comment, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (user_id, raw_text, category, amount, comment, created_at),
    )
    expense_id = cur.lastrowid
    conn.commit()
    conn.close()
    return expense_id


def fetch_total_for_period(user_id: int, start_iso: str, end_iso: str) -> float:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT COALESCE(SUM(amount), 0)
        FROM expenses
        WHERE user_id = ? AND created_at >= ? AND created_at < ?
        """,
        (user_id, start_iso, end_iso),
    )
    total = float(cur.fetchone()[0] or 0)
    conn.close()
    return total


def fetch_grouped_for_period(user_id: int, start_iso: str, end_iso: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT category, COALESCE(SUM(amount), 0) AS total
        FROM expenses
        WHERE user_id = ? AND created_at >= ? AND created_at < ?
        GROUP BY category
        ORDER BY total DESC, category ASC
        """,
        (user_id, start_iso, end_iso),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def fetch_expenses_for_period(user_id: int, start_iso: str, end_iso: str, limit: int | None = None):
    conn = get_connection()
    cur = conn.cursor()

    query = """
        SELECT id, category, amount, comment, created_at
        FROM expenses
        WHERE user_id = ? AND created_at >= ? AND created_at < ?
        ORDER BY id DESC
    """
    params: list = [user_id, start_iso, end_iso]

    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)

    cur.execute(query, tuple(params))
    rows = cur.fetchall()
    conn.close()
    return rows


def fetch_last_expenses(user_id: int, limit: int = 5):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, category, amount, comment, created_at
        FROM expenses
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (user_id, limit),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def delete_last_expense(user_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, category, amount, comment FROM expenses WHERE user_id = ? ORDER BY id DESC LIMIT 1",
        (user_id,),
    )
    row = cur.fetchone()
    if not row:
        conn.close()
        return None

    cur.execute("DELETE FROM expenses WHERE id = ?", (row[0],))
    conn.commit()
    conn.close()
    return row


# ==============================
# PARSER
# Examples:
# кофе 85
# такси 140
# еда 320 магазин
# ==============================
AMOUNT_REGEX = re.compile(r"(\d+[\.,]?\d*)")


def parse_expense_message(text: str):
    cleaned = " ".join(text.strip().split())
    if not cleaned:
        return None

    match = AMOUNT_REGEX.search(cleaned)
    if not match:
        return None

    amount_raw = match.group(1).replace(",", ".")
    try:
        amount = float(amount_raw)
    except ValueError:
        return None

    if amount <= 0:
        return None

    start, end = match.span()
    before = cleaned[:start].strip()
    after = cleaned[end:].strip()

    if not before:
        return None

    category = before.lower()
    comment = after if after else None

    return {
        "category": category,
        "amount": amount,
        "comment": comment,
        "raw_text": cleaned,
    }


# ==============================
# HELPERS
# ==============================
def format_amount(amount: float) -> str:
    if amount.is_integer():
        return str(int(amount))
    return f"{amount:.2f}"



def get_today_range() -> tuple[str, str]:
    now = datetime.now(ZoneInfo(TIMEZONE))
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start.isoformat(), end.isoformat()



def get_month_range() -> tuple[str, str]:
    now = datetime.now(ZoneInfo(TIMEZONE))
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start.isoformat(), end.isoformat()



def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["Сегодня", "Месяц"],
            ["Категории", "Последние"],
            ["Удалить последнюю"],
        ],
        resize_keyboard=True,
    )



def build_expense_line(category: str, amount: float, comment: str | None = None) -> str:
    line = f"• {category} — {format_amount(amount)}"
    if comment:
        line += f" ({comment})"
    return line


# ==============================
# REPORTS
# ==============================
async def send_today_report(update: Update) -> None:
    user_id = update.effective_user.id
    start_iso, end_iso = get_today_range()
    total = fetch_total_for_period(user_id, start_iso, end_iso)
    rows = fetch_expenses_for_period(user_id, start_iso, end_iso, limit=20)

    if not rows:
        await update.message.reply_text("Сегодня расходов пока нет.", reply_markup=main_keyboard())
        return

    lines = ["Сегодня:", ""]
    for _, category, amount, comment, _ in rows:
        lines.append(build_expense_line(category, amount, comment))

    lines.append("")
    lines.append(f"Итого: {format_amount(total)}")
    await update.message.reply_text("\n".join(lines), reply_markup=main_keyboard())


async def send_month_report(update: Update) -> None:
    user_id = update.effective_user.id
    start_iso, end_iso = get_month_range()
    total = fetch_total_for_period(user_id, start_iso, end_iso)
    rows = fetch_grouped_for_period(user_id, start_iso, end_iso)

    if not rows:
        await update.message.reply_text("За месяц расходов пока нет.", reply_markup=main_keyboard())
        return

    lines = ["За месяц:", ""]
    for category, amount in rows:
        lines.append(f"• {category} — {format_amount(float(amount))}")

    lines.append("")
    lines.append(f"Итого: {format_amount(total)}")
    await update.message.reply_text("\n".join(lines), reply_markup=main_keyboard())


async def send_categories_report(update: Update) -> None:
    user_id = update.effective_user.id
    start_iso, end_iso = get_month_range()
    rows = fetch_grouped_for_period(user_id, start_iso, end_iso)

    if not rows:
        await update.message.reply_text("Категорий пока нет.", reply_markup=main_keyboard())
        return

    lines = ["Категории за месяц:", ""]
    for category, amount in rows:
        lines.append(f"• {category} — {format_amount(float(amount))}")

    await update.message.reply_text("\n".join(lines), reply_markup=main_keyboard())


async def send_last_report(update: Update) -> None:
    user_id = update.effective_user.id
    rows = fetch_last_expenses(user_id, 5)
    if not rows:
        await update.message.reply_text("Пока нет расходов.", reply_markup=main_keyboard())
        return

    lines = ["Последние 5 записей:", ""]
    for expense_id, category, amount, comment, _ in rows:
        line = f"#{expense_id} {build_expense_line(category, amount, comment)[2:]}"
        lines.append(line)

    await update.message.reply_text("\n".join(lines), reply_markup=main_keyboard())


# ==============================
# HANDLERS
# ==============================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "Привет. Я бот для учета расходов.\n\n"
        "Пиши обычным сообщением, например:\n"
        "кофе 85\n"
        "такси 140\n"
        "еда 320 магазин\n\n"
        "Кнопки снизу покажут отчеты и последние записи."
    )
    await update.message.reply_text(text, reply_markup=main_keyboard())


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    lower = text.lower()

    if lower == "сегодня":
        await send_today_report(update)
        return
    if lower == "месяц":
        await send_month_report(update)
        return
    if lower == "категории":
        await send_categories_report(update)
        return
    if lower == "последние":
        await send_last_report(update)
        return
    if lower == "удалить последнюю":
        row = delete_last_expense(update.effective_user.id)
        if not row:
            await update.message.reply_text("Удалять нечего.", reply_markup=main_keyboard())
            return

        _, category, amount, comment = row
        text_reply = f"Удалил: {category} — {format_amount(float(amount))}"
        if comment:
            text_reply += f" ({comment})"
        await update.message.reply_text(text_reply, reply_markup=main_keyboard())
        return

    parsed = parse_expense_message(text)
    if not parsed:
        await update.message.reply_text(
            "Не понял запись. Пример: еда 320 магазин",
            reply_markup=main_keyboard(),
        )
        return

    expense_id = save_expense(
        user_id=update.effective_user.id,
        raw_text=parsed["raw_text"],
        category=parsed["category"],
        amount=parsed["amount"],
        comment=parsed["comment"],
    )

    response = f"Сохранил #{expense_id}: {parsed['category']} — {format_amount(parsed['amount'])}"
    if parsed["comment"]:
        response += f" ({parsed['comment']})"

    await update.message.reply_text(response, reply_markup=main_keyboard())


# ==============================
# MAIN
# ==============================
def main() -> None:
    init_db()

    if not BOT_TOKEN:
        raise ValueError("Не найден BOT_TOKEN в переменных окружения.")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
