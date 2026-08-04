"""Команди адміна в групі + доставка reply-відповідей із теми «Замовлення» клієнтам."""
import asyncio
import html
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReactionTypeEmoji,
)

import db
from config import Config
from sheets_service import STATUS_ALIASES, STATUS_PACKED, STATUSES, SheetsService

log = logging.getLogger(__name__)

CAPTION_LIMIT = 1024  # ліміт Telegram на підпис до фото
SEND_PAUSE = 0.06  # пауза між надсиланнями, щоб не впертись у ліміт ~30 msg/s

# Слово в лапках після /mailings → чи додавати кнопку «Замовити»
MAILING_MODES = {"товар": True, "івент": False}


def _reply_user(message: Message) -> int | None:
    """Клієнт, якому належить повідомлення, на яке зроблено reply."""
    if not message.reply_to_message:
        return None
    return db.user_by_group_message(message.reply_to_message.message_id)


def build_admin_router(cfg: Config, sheets: SheetsService) -> Router:
    router = Router(name="admin")
    router.message.filter(F.chat.id == cfg.admin_group_id)

    def _allowed_here(message: Message) -> bool:
        """Якщо задано BOT_TOPIC_ID — команди працюють лише в темі «Замовлення»."""
        return cfg.bot_topic_id is None or message.message_thread_id == cfg.bot_topic_id

    @router.message(Command("importclients"))
    async def cmd_import_clients(message: Message):
        """Разова міграція: клієнти зі старої локальної SQLite → вкладка «Клієнти»."""
        if not _allowed_here(message):
            return
        legacy = db.all_clients()
        if not legacy:
            await message.reply("У локальній базі клієнтів немає — імпортувати нічого.")
            return
        try:
            existing = {uid for uid, _, _ in await sheets.all_clients()}
        except Exception:
            log.exception("Імпорт клієнтів: не вдалося прочитати Таблицю")
            await message.reply("❌ Не вдалося прочитати вкладку «Клієнти».")
            return

        added = skipped = failed = 0
        for user_id, full_name, username in legacy:
            if user_id in existing:
                skipped += 1
                continue
            try:
                await sheets.upsert_client(user_id, full_name, username)
                added += 1
            except Exception:
                failed += 1
                log.warning("Імпорт клієнтів: не вдалося додати %s", user_id, exc_info=True)

        report = (
            f"📥 <b>Імпорт зі старої бази.</b>\n"
            f"Знайдено локально: {len(legacy)}\n"
            f"➕ Додано в Таблицю: {added}\n"
            f"↩️ Уже були: {skipped}"
        )
        if failed:
            report += f"\n⚠️ Помилки: {failed}"
        await message.reply(report)

    @router.message(Command("mailings"))
    async def cmd_mailings(message: Message, command: CommandObject, bot: Bot):
        """Reply на повідомлення + /mailings «товар»|«івент» → розсилка всім клієнтам."""
        if not _allowed_here(message):
            return
        usage = (
            "Зробіть <b>reply</b> на повідомлення (текст і/або фото) і напишіть:\n"
            "<code>/mailings «товар»</code> — з кнопкою «🛒 Замовити»\n"
            "<code>/mailings «івент»</code> — без кнопки"
        )
        mode = (command.args or "").strip().strip("\"'«»„“”‘’` ").lower()
        if mode not in MAILING_MODES:
            await message.reply(usage)
            return
        with_button = MAILING_MODES[mode]

        src = message.reply_to_message
        if not src:
            await message.reply(usage)
            return
        try:
            body = src.html_text
        except Exception:
            body = src.text or src.caption or ""
        photo_id = src.photo[-1].file_id if src.photo else None
        if not body and not photo_id:
            await message.reply("У тому повідомленні немає ні тексту, ні фото 🤔")
            return

        if cfg.mailing_test_mode:
            # Тестовий режим: лист отримує тільки той, хто набрав команду
            me = message.from_user
            clients = [(me.id, me.full_name, me.username)]
        else:
            try:
                clients = await sheets.all_clients()
            except Exception:
                log.exception("Не вдалося прочитати «Клієнти» з Таблиці")
                await message.reply("❌ Не вдалося прочитати список клієнтів із Таблиці.")
                return
        if not clients:
            await message.reply("Поки що немає жодного клієнта для розсилки.")
            return

        kb = (
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🛒 Замовити", callback_data="promo:order")]
                ]
            )
            if with_button
            else None
        )
        if cfg.mailing_test_mode:
            status = await message.reply(
                "🧪 <b>Тестовий режим</b> — лист піде тільки вам.\n"
                "Щоб слати всім: приберіть <code>MAILING_TEST_MODE</code> з .env "
                "і перезапустіть бота."
            )
        else:
            status = await message.reply(f"📣 Розсилаю… отримувачів: {len(clients)}")

        async def deliver(user_id: int, text: str) -> None:
            if photo_id:
                if len(text) <= CAPTION_LIMIT:
                    await bot.send_photo(user_id, photo_id, caption=text, reply_markup=kb)
                else:
                    await bot.send_photo(user_id, photo_id)
                    await bot.send_message(user_id, text, reply_markup=kb)
            else:
                await bot.send_message(user_id, text, reply_markup=kb)

        sent = blocked = failed = 0
        for user_id, full_name, username in clients:
            # Індивідуальне тегання: @username, а якщо його немає — іменне посилання
            if username:
                mention = f"@{username}"
            else:
                name = html.escape(full_name or "друже")
                mention = f'<a href="tg://user?id={user_id}">{name}</a>'
            greeting = f"Вітаю {mention}, диви що у нас нового для тебе:"
            text = f"{greeting}\n\n{body}" if body else greeting
            try:
                await deliver(user_id, text)
                sent += 1
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
                try:
                    await deliver(user_id, text)
                    sent += 1
                except Exception:
                    failed += 1
            except Exception as e:
                # Найчастіше — клієнт заблокував бота
                if "bot was blocked" in str(e).lower() or "user is deactivated" in str(e).lower():
                    blocked += 1
                else:
                    failed += 1
                    log.warning("Розсилка: не доставлено %s: %s", user_id, e)
            await asyncio.sleep(SEND_PAUSE)

        head = "🧪 <b>Тестову розсилку завершено.</b>" if cfg.mailing_test_mode else (
            "📣 <b>Розсилку завершено.</b>"
        )
        kind = f"«{mode}»" + (" — з кнопкою «Замовити»" if with_button else " — без кнопки")
        report = f"{head}\n{kind}\n✅ Доставлено: {sent} з {len(clients)}"
        if blocked:
            report += f"\n🚫 Заблокували бота: {blocked}"
        if failed:
            report += f"\n⚠️ Помилки: {failed}"
        try:
            await status.edit_text(report)
        except Exception:
            await message.reply(report)

    @router.message(Command("topicid"))
    async def cmd_topicid(message: Message):
        if message.message_thread_id:
            await message.reply(
                f"ID цієї теми: <code>{message.message_thread_id}</code>\n"
                "Щоб бот працював саме тут, додайте в .env:\n"
                f"<code>BOT_TOPIC_ID={message.message_thread_id}</code> і перезапустіть бота."
            )
        else:
            await message.reply("Це General — окремого ID теми тут немає.")

    @router.message(Command("pause"))
    async def cmd_pause(message: Message):
        user_id = _reply_user(message)
        if not user_id:
            await message.reply(
                "Зробіть reply на повідомлення клієнта і напишіть /pause."
            )
            return
        db.set_paused(user_id, True)
        await message.reply(
            "⏸ Авто-відповіді для цього клієнта вимкнено. Бот мовчить — "
            "відповідає тільки адмін. /resume (reply) — увімкнути назад."
        )

    @router.message(Command("resume"))
    async def cmd_resume(message: Message):
        user_id = _reply_user(message)
        if not user_id:
            await message.reply(
                "Зробіть reply на повідомлення клієнта і напишіть /resume."
            )
            return
        db.set_paused(user_id, False)
        await message.reply("▶️ Авто-відповіді для цього клієнта знову увімкнено.")

    @router.message(Command("status"))
    async def cmd_status(message: Message, command: CommandObject):
        usage = (
            "Використання: <code>/status &lt;номер&gt; &lt;статус&gt;</code>\n"
            "Наприклад: <code>/status 12 запаковано</code>\n"
            "Статуси: " + ", ".join(STATUSES)
        )
        if not _allowed_here(message):
            return
        parts = (command.args or "").strip().split(maxsplit=1)
        if len(parts) < 2 or not parts[0].lstrip("#").isdigit():
            await message.reply(usage)
            return
        number = int(parts[0].lstrip("#"))
        status = STATUS_ALIASES.get(parts[1].strip().lower())
        if not status:
            await message.reply("Невідомий статус.\n" + usage)
            return
        row = db.row_for_order(number)
        if row is None:
            await message.reply(f"❌ Замовлення #{number} не знайдено.")
            return
        try:
            await sheets.set_packed(row, packed=(status == STATUS_PACKED))
        except Exception:
            log.exception("Помилка Google Таблиці при зміні статусу #%s", number)
            await message.reply("❌ Таблиця недоступна, спробуйте пізніше.")
            return
        await message.reply(f"✅ Замовлення #{number} → {status}")

    @router.message(Command("orders"))
    async def cmd_orders(message: Message):
        if not _allowed_here(message):
            return
        try:
            orders = await sheets.list_orders(200)
        except Exception:
            log.exception("Помилка Google Таблиці при запиті замовлень")
            await message.reply("❌ Таблиця недоступна, спробуйте пізніше.")
            return
        orders = [o for o in orders if not o["packed"]][:10]
        if not orders:
            await message.reply("Немає незапакованих замовлень 🎉")
            return
        lines = ["🟡 <b>Незапаковані замовлення (останні 10):</b>"]
        for o in orders:
            number = db.order_for_row(o["row"])
            label = f"#{number}" if number is not None else f"рядок {o['row']}"
            items = o["items"] if len(o["items"]) <= 60 else o["items"][:60] + "…"
            lines.append(
                f"• <b>{label} {html.escape(o['fio'])}</b> — "
                f"{html.escape(items)} · {html.escape(o['phone'])}"
            )
        lines.append("\nЗмінити статус: <code>/status &lt;номер&gt; &lt;статус&gt;</code>")
        await message.reply("\n".join(lines))

    @router.message(F.reply_to_message)
    async def relay_to_client(message: Message, bot: Bot):
        # Reply адміна на повідомлення клієнта в темі «Замовлення» → доставляємо клієнту
        if message.text and message.text.startswith("/"):
            return
        user_id = _reply_user(message)
        if not user_id:
            return
        try:
            await bot.copy_message(user_id, message.chat.id, message.message_id)
        except Exception:
            log.exception("Не вдалося доставити відповідь клієнту %s", user_id)
            await message.reply(
                "❌ Не вдалося доставити повідомлення (можливо, клієнт заблокував бота)."
            )
            return
        # Тиха відмітка «доставлено» реакцією, щоб не засмічувати тему
        try:
            await bot.set_message_reaction(
                message.chat.id, message.message_id, reaction=[ReactionTypeEmoji(emoji="👌")]
            )
        except Exception:
            pass

    return router
