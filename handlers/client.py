"""Повідомлення клієнтів у приваті: пересилання в єдину тему «Замовлення» + авто-відповіді."""
import html
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.types import Message

import db
from config import Config
from sheets_service import SheetsService

log = logging.getLogger(__name__)


def build_client_router(cfg: Config, sheets: SheetsService) -> Router:
    router = Router(name="client")
    router.message.filter(F.chat.type == "private", StateFilter(None))

    @router.message()
    async def on_client_message(message: Message, bot: Bot):
        if message.text and message.text.startswith("/"):
            await message.answer("Невідома команда 🤔 /start — каталог і замовлення.")
            return

        user = message.from_user
        db.upsert_client(user.id, user.full_name, user.username)
        # Паралельно ведемо базу розсилки у вкладці «Клієнти»
        try:
            await sheets.upsert_client(user.id, user.full_name, user.username)
        except Exception:
            log.warning("Не вдалося записати клієнта %s у Таблицю", user.id, exc_info=True)

        # Пересилаємо повідомлення в тему «Замовлення» і запам'ятовуємо,
        # від кого воно, — щоб reply адміна повернувся клієнту
        fwd = None
        try:
            fwd = await bot.forward_message(
                cfg.admin_group_id,
                message.chat.id,
                message.message_id,
                message_thread_id=cfg.bot_topic_id,
            )
        except TelegramBadRequest:
            try:
                fwd = await bot.forward_message(
                    cfg.admin_group_id, message.chat.id, message.message_id
                )
            except TelegramBadRequest:
                log.exception("Не вдалося переслати повідомлення від %s", user.id)
        if fwd:
            db.map_group_message(fwd.message_id, user.id)

        # Пауза: бот мовчить, відповідає тільки адмін
        if db.is_paused(user.id):
            return

        text = message.text or message.caption or ""
        answer = await sheets.find_answer(text) if text else None

        who = f"@{user.username}" if user.username else user.full_name
        try:
            if answer:
                await message.answer(answer)
                note = await bot.send_message(
                    cfg.admin_group_id,
                    f"🤖 Авто-відповідь для {html.escape(who)}: {html.escape(answer)}",
                    message_thread_id=cfg.bot_topic_id,
                    reply_to_message_id=fwd.message_id if fwd else None,
                )
            else:
                note = await bot.send_message(
                    cfg.admin_group_id,
                    f"⚠️ Потрібна відповідь адміна для {html.escape(who)}.\n"
                    "Зробіть reply на повідомлення клієнта — бот доставить відповідь.",
                    message_thread_id=cfg.bot_topic_id,
                    reply_to_message_id=fwd.message_id if fwd else None,
                )
            db.map_group_message(note.message_id, user.id)
        except TelegramBadRequest:
            log.exception("Не вдалося написати в тему «Замовлення»")

    return router
