"""Точка входу: Telegram-бот замовлень з Google Таблицею як сховищем даних."""
import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

import db
from config import Config, load_config
from handlers import build_admin_router, build_client_router, build_order_router
from notion_service import NotionService
from sheets_service import SheetsService

log = logging.getLogger(__name__)

FLUSH_INTERVAL = 60  # секунд між спробами довантажити офлайн-чергу


async def pending_flush_loop(bot: Bot, cfg: Config, sheets: SheetsService) -> None:
    while True:
        await asyncio.sleep(FLUSH_INTERVAL)
        try:
            if not sheets.pending_count():
                continue
            flushed = await sheets.flush_pending()
            if flushed:
                for item in flushed:
                    db.map_order_page(item["number"], item["page_id"], item.get("user_id"))
                nums = ", ".join(f"#{item['number']}" for item in flushed)
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
        log.info("Бот запущено")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        flush_task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
