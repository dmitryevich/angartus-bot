"""Команди адміна в групі + доставка reply-відповідей із теми «Замовлення» клієнтам."""
import html
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, ReactionTypeEmoji

import db
from config import Config
from notion_service import STATUS_ALIASES, STATUSES, NotionService

log = logging.getLogger(__name__)


def _reply_user(message: Message) -> int | None:
    """Клієнт, якому належить повідомлення, на яке зроблено reply."""
    if not message.reply_to_message:
        return None
    return db.user_by_group_message(message.reply_to_message.message_id)


def build_admin_router(cfg: Config, notion: NotionService) -> Router:
    router = Router(name="admin")
    router.message.filter(F.chat.id == cfg.admin_group_id)

    def _allowed_here(message: Message) -> bool:
        """Якщо задано BOT_TOPIC_ID — команди працюють лише в темі «Замовлення»."""
        return cfg.bot_topic_id is None or message.message_thread_id == cfg.bot_topic_id

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
            "Наприклад: <code>/status 12 в роботі</code>\n"
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
        try:
            found = await notion.set_status(number, status)
        except Exception:
            log.exception("Помилка Notion при зміні статусу #%s", number)
            await message.reply("❌ Notion недоступний, спробуйте пізніше.")
            return
        if found:
            await message.reply(f"✅ Замовлення #{number} → {status}")
        else:
            await message.reply(f"❌ Замовлення #{number} не знайдено в Notion.")

    @router.message(Command("orders"))
    async def cmd_orders(message: Message):
        if not _allowed_here(message):
            return
        try:
            orders = await notion.last_new_orders(10)
        except Exception:
            log.exception("Помилка Notion при запиті замовлень")
            await message.reply("❌ Notion недоступний, спробуйте пізніше.")
            return
        if not orders:
            await message.reply("Немає замовлень зі статусом «🟡 Нове» 🎉")
            return
        lines = ["🟡 <b>Нові замовлення (останні 10):</b>"]
        for o in orders:
            date = o["date"][:10] if o["date"] else "—"
            number = f"#{o['number']:.0f}" if o["number"] is not None else "#?"
            items = o["items"] if len(o["items"]) <= 60 else o["items"][:60] + "…"
            lines.append(
                f"• <b>{number} {html.escape(o['fio'])}</b> — "
                f"{html.escape(items)} · {html.escape(o['phone'])} · {date}"
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
