import asyncio
import logging
import html
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from aiogram.exceptions import TelegramRetryAfter

TOKEN = os.getenv("BOT_TOKEN")
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow"))
DB_PATH = os.getenv("DATABASE_PATH", "countdowns.db")
UPDATE_INTERVAL = 5  # Telegram limits message edits; 5 seconds keeps the countdown reliable.

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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tickets_counter (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                remaining INTEGER NOT NULL,
                total INTEGER NOT NULL
            )
        """)


def russian_plural(number: int, forms: tuple[str, str, str]) -> str:
    n = abs(number) % 100
    if 11 <= n <= 14:
        form = forms[2]
    else:
        n %= 10
        if n == 1:
            form = forms[0]
        elif 2 <= n <= 4:
            form = forms[1]
        else:
            form = forms[2]
    return f"{number} {form}"


def format_countdown(title: str, target: datetime) -> str:
    now = datetime.now(TIMEZONE)
    total_seconds = max(0, int((target - now).total_seconds()))
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)

    safe_title = html.escape(title)
    return (
        f"<b>{safe_title}</b>\n\n"
        f"{russian_plural(days, ('день', 'дня', 'дней'))} "
        f"{russian_plural(hours, ('час', 'часа', 'часов'))} "
        f"{russian_plural(minutes, ('минута', 'минуты', 'минут'))} "
        f"{russian_plural(seconds, ('секунда', 'секунды', 'секунд'))}"
    )


async def is_admin(message: Message) -> bool:
    try:
        # В канале команды приходят как channel_post: Telegram уже гарантирует,
        # что публикацию создал администратор канала. Дополнительно проверяем
        # права самого бота на публикацию и редактирование сообщений.
        if message.chat.type == "channel":
            if not message.sender_chat or message.sender_chat.id != message.chat.id:
                return False
            me = await bot.get_me()
            member = await bot.get_chat_member(message.chat.id, me.id)
            return (
                member.status == "administrator"
                and getattr(member, "can_post_messages", False)
                and getattr(member, "can_edit_messages", False)
            )

        if not message.from_user:
            return False
        member = await bot.get_chat_member(message.chat.id, message.from_user.id)
        return member.status in ("creator", "administrator")
    except Exception:
        logger.exception("Admin check failed for chat %s", message.chat.id)
        return False


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "Привет! Я бот обратного отсчёта. ⏳\n\n"
        "Добавь меня в группу или канал и используй команду:\n"
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
@dp.channel_post(Command("countdown"))
async def create_countdown(message: Message):
    if message.chat.type == "private":
        await message.answer("Добавь меня в группу или канал, чтобы создать там отсчёт.")
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
            (message.chat.id, sent.message_id, title, target.isoformat(), message.from_user.id if message.from_user else None)
        )
        countdown_id = cursor.lastrowid
        tickets = conn.execute(
            "SELECT remaining, total FROM tickets_counter WHERE id = 1 AND chat_id = ?",
            (message.chat.id,)
        ).fetchone()
        if tickets:
            conn.execute("UPDATE tickets_counter SET message_id = ? WHERE id = 1", (sent.message_id,))
            conn.execute(
                "UPDATE countdowns SET finished = 1 WHERE chat_id = ? AND id != ? AND finished = 0",
                (message.chat.id, countdown_id)
            )
    if tickets:
        await bot.edit_message_text(
            format_countdown(title, target) + f"\n\n🎟 <b>{format_tickets(tickets['remaining'], tickets['total'])}</b>",
            chat_id=message.chat.id, message_id=sent.message_id, parse_mode="HTML"
        )



async def get_tickets_config():
    with db() as conn:
        return conn.execute(
            "SELECT chat_id, message_id, remaining, total FROM tickets_counter WHERE id = 1"
        ).fetchone()


def format_tickets(remaining: int, total: int) -> str:
    return f"Осталось {remaining} из {total} путёвок"


async def is_group_admin(chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ("creator", "administrator")
    except Exception:
        return False


@dp.message(Command("tickets_post"))
@dp.channel_post(Command("tickets_post"))
async def create_tickets_post(message: Message):
    if message.chat.type == "private" or (not message.from_user and message.chat.type != "channel"):
        await message.answer("Эту команду нужно отправить в общем чате лагеря.")
        return
    if not await is_admin(message):
        return

    # Удаляем команду, чтобы участники видели только публичный счётчик.
    try:
        await message.delete()
    except Exception:
        await message.answer(
            "Не удалось скрыть команду. Дай боту право удалять сообщения и повтори."
        )
        return

    with db() as conn:
        countdown = conn.execute(
            """SELECT message_id, title, target_at FROM countdowns
               WHERE chat_id = ? AND finished = 0 ORDER BY id DESC LIMIT 1""",
            (message.chat.id,)
        ).fetchone()
        if not countdown:
            await message.answer("Сначала создай таймер командой /countdown, затем отправь /tickets_post.")
            return
        existing = conn.execute(
            "SELECT chat_id, message_id, remaining, total FROM tickets_counter WHERE id = 1"
        ).fetchone()
        if existing and existing["chat_id"] == message.chat.id:
            remaining, total = existing["remaining"], existing["total"]
            old_message_id = existing["message_id"]
        else:
            remaining, total, old_message_id = 48, 100, None
        conn.execute(
            """INSERT INTO tickets_counter (id, chat_id, message_id, remaining, total)
               VALUES (1, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET chat_id=excluded.chat_id,
               message_id=excluded.message_id, remaining=excluded.remaining, total=excluded.total""",
            (message.chat.id, countdown["message_id"], remaining, total)
        )
    if old_message_id and old_message_id != countdown["message_id"]:
        try:
            await bot.delete_message(message.chat.id, old_message_id)
        except Exception:
            pass
    target = datetime.fromisoformat(countdown["target_at"]).astimezone(TIMEZONE)
    combined = format_countdown(countdown["title"], target)
    combined += f"\n\n🎟 <b>{format_tickets(remaining, total)}</b>"
    await bot.edit_message_text(
        combined, chat_id=message.chat.id, message_id=countdown["message_id"], parse_mode="HTML"
    )


@dp.message(Command("tickets"))
async def set_tickets_remaining(message: Message):
    if message.chat.type != "private" or not message.from_user:
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /tickets 48")
        return

    row = await get_tickets_config()
    if not row:
        await message.answer("Сначала создай публичный счётчик командой /tickets_post в общем чате.")
        return
    if not await is_group_admin(row["chat_id"], message.from_user.id):
        await message.answer("Изменять количество путёвок могут только администраторы лагерного чата.")
        return

    remaining = int(parts[1])
    if remaining < 0 or remaining > row["total"]:
        await message.answer(f"Введи число от 0 до {row['total']}.")
        return

    try:
        with db() as conn:
            countdown = conn.execute(
                "SELECT title, target_at, finished FROM countdowns WHERE chat_id = ? AND message_id = ?",
                (row["chat_id"], row["message_id"])
            ).fetchone()
        if not countdown:
            await message.answer("Таймер не найден. Создай новый таймер и снова привяжи счётчик командой /tickets_post.")
            return
        target = datetime.fromisoformat(countdown["target_at"]).astimezone(TIMEZONE)
        timer_text = (
            f"🎉 <b>{html.escape(countdown['title'])}</b>\n\nСобытие уже началось!"
            if countdown["finished"] else format_countdown(countdown["title"], target)
        )
        await bot.edit_message_text(
            timer_text + f"\n\n🎟 <b>{format_tickets(remaining, row['total'])}</b>",
            chat_id=row["chat_id"], message_id=row["message_id"], parse_mode="HTML"
        )
    except Exception as exc:
        logger.warning("Tickets message update failed: %s", exc)
        await message.answer("Не удалось обновить публичный счётчик. Возможно, сообщение удалено.")
        return

    with db() as conn:
        conn.execute("UPDATE tickets_counter SET remaining = ? WHERE id = 1", (remaining,))
    await message.answer(f"Готово. Осталось {remaining} из {row['total']} путёвок.")


@dp.message(Command("tickets_total"))
async def set_tickets_total(message: Message):
    if message.chat.type != "private" or not message.from_user:
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /tickets_total 100")
        return

    row = await get_tickets_config()
    if not row:
        await message.answer("Сначала создай публичный счётчик командой /tickets_post в общем чате.")
        return
    if not await is_group_admin(row["chat_id"], message.from_user.id):
        await message.answer("Изменять вместимость могут только администраторы лагерного чата.")
        return

    total = int(parts[1])
    if total < 1 or total < row["remaining"]:
        await message.answer(
            f"Общая вместимость должна быть не меньше остатка ({row['remaining']}) и минимум 1."
        )
        return

    try:
        with db() as conn:
            countdown = conn.execute(
                "SELECT title, target_at, finished FROM countdowns WHERE chat_id = ? AND message_id = ?",
                (row["chat_id"], row["message_id"])
            ).fetchone()
        if not countdown:
            await message.answer("Таймер не найден. Создай новый таймер и снова привяжи счётчик командой /tickets_post.")
            return
        target = datetime.fromisoformat(countdown["target_at"]).astimezone(TIMEZONE)
        timer_text = (
            f"🎉 <b>{html.escape(countdown['title'])}</b>\n\nСобытие уже началось!"
            if countdown["finished"] else format_countdown(countdown["title"], target)
        )
        await bot.edit_message_text(
            timer_text + f"\n\n🎟 <b>{format_tickets(row['remaining'], total)}</b>",
            chat_id=row["chat_id"], message_id=row["message_id"], parse_mode="HTML"
        )
    except Exception as exc:
        logger.warning("Tickets total update failed: %s", exc)
        await message.answer("Не удалось обновить публичный счётчик. Возможно, сообщение удалено.")
        return

    with db() as conn:
        conn.execute("UPDATE tickets_counter SET total = ? WHERE id = 1", (total,))
    await message.answer(f"Готово. Вместимость теперь {total} путёвок.")


@dp.message(Command("tickets_status"))
async def tickets_status(message: Message):
    if message.chat.type != "private" or not message.from_user:
        return
    row = await get_tickets_config()
    if not row:
        await message.answer("Публичный счётчик ещё не создан.")
        return
    if not await is_group_admin(row["chat_id"], message.from_user.id):
        await message.answer("Эта команда доступна только администраторам лагерного чата.")
        return
    await message.answer(format_tickets(row["remaining"], row["total"]))


@dp.message(Command("list"))
@dp.channel_post(Command("list"))
async def list_countdowns(message: Message):
    with db() as conn:
        rows = conn.execute(
            "SELECT id, title FROM countdowns WHERE chat_id = ? AND finished = 0 ORDER BY target_at",
            (message.chat.id,)
        ).fetchall()

    if not rows:
        await message.answer("В этом чате пока нет активных отсчётов.")
        return

    lines = ["⏳ <b>Активные отсчёты:</b>"]
    for row in rows:
        lines.append(f"\n<b>#{row['id']}</b> — {html.escape(row['title'])}")
    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("stop"))
@dp.channel_post(Command("stop"))
async def stop_countdown(message: Message):
    if message.chat.type == "private":
        await message.answer("Эту команду нужно использовать в группе или канале.")
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
                    with db() as conn:
                        tickets = conn.execute(
                            "SELECT remaining, total FROM tickets_counter WHERE id = 1 AND chat_id = ? AND message_id = ?",
                            (row["chat_id"], row["message_id"])
                        ).fetchone()
                    if now >= target:
                        text = f"🎉 <b>{html.escape(row['title'])}</b>\n\nСобытие уже началось!"
                        with db() as conn:
                            conn.execute("UPDATE countdowns SET finished = 1 WHERE id = ?", (row["id"],))
                    else:
                        text = format_countdown(row["title"], target)
                    if tickets:
                        text += f"\n\n🎟 <b>{format_tickets(tickets['remaining'], tickets['total'])}</b>"
                    await bot.edit_message_text(
                        text, chat_id=row["chat_id"], message_id=row["message_id"], parse_mode="HTML"
                    )
                except TelegramRetryAfter as exc:
                    # Respect Telegram's flood-control response instead of retrying every tick.
                    logger.warning(
                        "Telegram rate limit for countdown %s; retrying after %s seconds",
                        row["id"], exc.retry_after
                    )
                    await asyncio.sleep(exc.retry_after)
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
