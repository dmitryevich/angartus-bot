"""Точка входу: Telegram-бот замовлень з Google Таблицею як сховищем даних."""
import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
)

import db
from config import Config, load_config
from handlers import build_admin_router, build_client_router, build_order_router
from notion_service import NotionService
from sheets_service import SheetsService

log = logging.getLogger(__name__)

FLUSH_INTERVAL = 60  # секунд між спробами довантажити офлайн-чергу

# Підказки по «/». Окремо для клієнтів і для адмін-групи: у темі «Замовлення»
# клієнтські команди зайві, а адмінські клієнтам показувати не можна.
CLIENT_COMMANDS = [
    BotCommand(command="start", description="Головне меню — каталог і замовлення"),
    BotCommand(command="cancel", description="Скасувати поточне оформлення"),
]
ADMIN_COMMANDS = [
    BotCommand(command="remnants", description="Залишки товарів — переглянути і змінити"),
    BotCommand(command="orders", description="Незапаковані замовлення (останні 10)"),
    BotCommand(command="status", description="Статус замовлення: /status <номер> запаковано"),
    BotCommand(command="mailings", description="Розсилка: reply + /mailings «товар»|«івент»"),
    BotCommand(command="pause", description="Reply на клієнта — вимкнути авто-відповіді"),
    BotCommand(command="resume", description="Reply на клієнта — увімкнути назад"),
    BotCommand(command="topicid", description="Показати ID цієї теми"),
    BotCommand(command="importclients", description="Разовий імпорт клієнтів у Таблицю"),
]


async def setup_commands(bot: Bot, cfg: Config) -> None:
    """Список команд по «/». Область — весь чат: окремої області під тему
    форуму Telegram не має, тому в адмін-групі підказки однакові в усіх темах."""
    await bot.set_my_commands(CLIENT_COMMANDS, scope=BotCommandScopeAllPrivateChats())
    try:
        await bot.set_my_commands(
            ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=cfg.admin_group_id)
        )
    except Exception:
        log.warning("Не вдалося поставити команди для адмін-групи", exc_info=True)


async def pending_flush_loop(bot: Bot, cfg: Config, sheets: SheetsService) -> None:
    while True:
        await asyncio.sleep(FLUSH_INTERVAL)
        try:
            if not sheets.pending_count():
                continue
            flushed = await sheets.flush_pending()
            if flushed:
                for item in flushed:
                    db.map_order_page(
                        item["number"], item["page_id"], item.get("user_id"), item.get("label")
                    )
                nums = ", ".join(
                    item.get("label") or f"#{item['number']}" for item in flushed
                )
                await bot.send_message(
                    cfg.admin_group_id,
                    f"✅ Зв'язок із Google Таблицею відновлено. Довантажено замовлення: {nums}.",
                    message_thread_id=cfg.bot_topic_id,
                )
        except Exception:
            log.exception("Помилка в циклі догрузки офлайн-черги")


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = load_config()
    db.init_db(cfg.db_path)
    sheets = SheetsService(cfg)   # клієнти для розсилки і шаблони відповідей
    orders = NotionService(cfg)   # самі замовлення

    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    # Порядок важливий: команди/FSM → адмін-група → всі інші повідомлення клієнтів
    dp.include_router(build_order_router(cfg, sheets, orders))
    dp.include_router(build_admin_router(cfg, sheets, orders))
    dp.include_router(build_client_router(cfg, sheets))

    flush_task = asyncio.create_task(pending_flush_loop(bot, cfg, orders))
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await setup_commands(bot, cfg)
        log.info("Бот запущено")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        flush_task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
