import asyncio
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

TOKEN = os.getenv("BOT_TOKEN")
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow"))
DB_PATH = os.getenv("DATABASE_PATH", "countdowns.db")
UPDATE_INTERVAL = 60

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=TOKEN) if TOKEN else None
dp = Dispatcher()


@contextmanager
def db():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS countdowns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                target_at TEXT NOT NULL,
                created_by INTEGER,
                finished INTEGER NOT NULL DEFAULT 0
            )
        """)


def format_countdown(title: str, target: datetime) -> str:
    now = datetime.now(TIMEZONE)
    seconds = max(0, int((target - now).total_seconds()))
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)

    return (
        f"⏳ <b>{title}</b>\n\n"
        f"До события осталось:\n"
        f"<b>{days} дн. {hours:02d} ч. {minutes:02d} мин.</b>\n\n"
        f"📅 {target.strftime('%d.%m.%Y в %H:%M')} МСК"
    )


async def is_admin(message: Message) -> bool:
    if not message.from_user:
        return False
    try:
        member = await bot.get_chat_member(message.chat.id, message.from_user.id)
        return member.status in ("creator", "administrator")
    except Exception:
        return False


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "Привет! Я бот обратного отсчёта. ⏳\n\n"
        "Добавь меня в групповой чат и используй команду:\n"
        "<code>/countdown Название события | 2026-12-15 12:00</code>\n\n"
        "Время указывается по Москве (МСК).\n"
        "/list — активные отсчёты\n"
        "/stop ID — остановить отсчёт\n"
        "/help — помощь"
    )


@dp.message(Command("help"))
async def help_command(message: Message):
    await start(message)


@dp.message(Command("countdown"))
async def create_countdown(message: Message):
    if message.chat.type == "private":
        await message.answer("Добавь меня в групповой чат, чтобы создать там отсчёт.")
        return
    if not await is_admin(message):
        await message.reply("Создавать отсчёты могут только администраторы чата.")
        return

    raw = (message.text or "").partition(" ")[2].strip()
    if "|" not in raw:
        await message.reply(
            "Формат команды:\n"
            "<code>/countdown Название события | 2026-12-15 12:00</code>"
        )
        return

    title, date_text = (part.strip() for part in raw.split("|", 1))
    if not title:
        await message.reply("Укажи название события.")
        return
    try:
        target = datetime.strptime(date_text, "%Y-%m-%d %H:%M").replace(tzinfo=TIMEZONE)
    except ValueError:
        await message.reply("Не понял дату. Используй формат ГГГГ-ММ-ДД ЧЧ:ММ, например 2026-12-15 12:00.")
        return

    if target <= datetime.now(TIMEZONE):
        await message.reply("Эта дата уже прошла. Укажи дату и время в будущем.")
        return

    sent = await message.answer(format_countdown(title, target), parse_mode="HTML")
    with db() as conn:
        cursor = conn.execute(
            "INSERT INTO countdowns (chat_id, message_id, title, target_at, created_by) VALUES (?, ?, ?, ?, ?)",
            (message.chat.id, sent.message_id, title, target.isoformat(), message.from_user.id)
        )
        countdown_id = cursor.lastrowid

    await message.reply(f"Готово! Отсчёт создан. ID: <code>{countdown_id}</code>")


@dp.message(Command("list"))
async def list_countdowns(message: Message):
    with db() as conn:
        rows = conn.execute(
            "SELECT id, title, target_at FROM countdowns WHERE chat_id = ? AND finished = 0 ORDER BY target_at",
            (message.chat.id,)
        ).fetchall()

    if not rows:
        await message.answer("В этом чате пока нет активных отсчётов.")
        return

    lines = ["⏳ <b>Активные отсчёты:</b>"]
    for row in rows:
        target = datetime.fromisoformat(row["target_at"]).astimezone(TIMEZONE)
        lines.append(f"\n<b>#{row['id']}</b> — {row['title']}\n📅 {target:%d.%m.%Y %H:%M} МСК")
    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("stop"))
async def stop_countdown(message: Message):
    if message.chat.type == "private":
        await message.answer("Эту команду нужно использовать в групповом чате.")
        return
    if not await is_admin(message):
        await message.reply("Останавливать отсчёты могут только администраторы чата.")
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].isdigit():
        await message.reply("Укажи ID отсчёта: <code>/stop 1</code>")
        return

    countdown_id = int(parts[1])
    with db() as conn:
        row = conn.execute(
            "SELECT message_id FROM countdowns WHERE id = ? AND chat_id = ? AND finished = 0",
            (countdown_id, message.chat.id)
        ).fetchone()
        if row:
            conn.execute("UPDATE countdowns SET finished = 1 WHERE id = ?", (countdown_id,))

    if not row:
        await message.reply("Активный отсчёт с таким ID не найден.")
        return

    try:
        await bot.delete_message(message.chat.id, row["message_id"])
    except Exception:
        pass
    await message.answer(f"Отсчёт #{countdown_id} остановлен.")


async def countdown_worker():
    while True:
        try:
            with db() as conn:
                rows = conn.execute(
                    "SELECT id, chat_id, message_id, title, target_at FROM countdowns WHERE finished = 0"
                ).fetchall()

            now = datetime.now(TIMEZONE)
            for row in rows:
                target = datetime.fromisoformat(row["target_at"]).astimezone(TIMEZONE)
                try:
                    if now >= target:
                        await bot.edit_message_text(
                            f"🎉 <b>Время пришло!</b>\n\n<b>{row['title']}</b>\n"
                            f"Событие уже началось!",
                            chat_id=row["chat_id"],
                            message_id=row["message_id"],
                            parse_mode="HTML"
                        )
                        with db() as conn:
                            conn.execute("UPDATE countdowns SET finished = 1 WHERE id = ?", (row["id"],))
                    else:
                        await bot.edit_message_text(
                            format_countdown(row["title"], target),
                            chat_id=row["chat_id"],
                            message_id=row["message_id"],
                            parse_mode="HTML"
                        )
                except Exception as exc:
                    # A deleted message or a removed bot should not stop other countdowns.
                    logger.warning("Countdown %s update failed: %s", row["id"], exc)
        except Exception:
            logger.exception("Countdown worker error")
        await asyncio.sleep(UPDATE_INTERVAL)


async def main():
    if not TOKEN:
        raise RuntimeError("Set BOT_TOKEN environment variable.")
    init_db()
    worker = asyncio.create_task(countdown_worker())
    try:
        await dp.start_polling(bot)
    finally:
        worker.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
