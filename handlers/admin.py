"""Команди адміна в групі + доставка reply-відповідей із теми «Замовлення» клієнтам."""
import asyncio
import html
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReactionTypeEmoji,
)

import db
from catalog import CATEGORIES, PRODUCT_BY_ID
from config import Config
from notion_service import STATUS_ALIASES, STATUS_PACKED, STATUSES, NotionService
from sheets_service import SheetsService

log = logging.getLogger(__name__)

CAPTION_LIMIT = 1024  # ліміт Telegram на підпис до фото
SEND_PAUSE = 0.06  # пауза між надсиланнями, щоб не впертись у ліміт ~30 msg/s

# Слово в лапках після /mailings → чи додавати кнопку «Замовити»
MAILING_MODES = {"товар": True, "івент": False}


def _left_label(product) -> str:
    """Залишок словами. None — кількість ще не задавали, обмежень немає."""
    left = db.get_stock(product.id)
    if not product.available:
        return "вимкнено в каталозі"
    if left is None:
        return "не задано"
    if left == 0:
        return "0 — немає в наявності"
    return f"{left} шт"


def stock_text() -> str:
    lines = ["📦 <b>Залишки товарів</b>"]
    for category in CATEGORIES:
        lines.append("\n<b>" + html.escape(category.title) + "</b>")
        for product in category.products:
            lines.append(
                f"• {html.escape(product.title)} — <b>{_left_label(product)}</b>"
            )
    lines.append("\nНатисніть на товар, щоб вказати, скільки лишилось.")
    return "\n".join(lines)


def stock_kb() -> InlineKeyboardMarkup:
    """По кнопці на товар: у підписі — поточний залишок, щоб не тицяти навмання."""
    rows = []
    for category in CATEGORIES:
        for product in category.products:
            left = db.get_stock(product.id)
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"{product.title}: {'—' if left is None else left}",
                        callback_data=f"stock:{product.id}",
                    )
                ]
            )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def real_reply(message: Message):
    """Справжня відповідь, а не артефакт форуму. У темах Telegram підставляє
    reply_to_message = службове повідомлення самої теми (його id збігається з
    message_thread_id), тож просто «є reply» ще нічого не означає."""
    reply = message.reply_to_message
    if reply is None or reply.message_id == message.message_thread_id:
        return None
    return reply


def _reply_user(message: Message) -> int | None:
    """Клієнт, якому належить повідомлення, на яке зроблено reply."""
    reply = real_reply(message)
    if not reply:
        return None
    return db.user_by_group_message(reply.message_id)


def build_admin_router(cfg: Config, sheets: SheetsService, orders: NotionService) -> Router:
    router = Router(name="admin")
    router.message.filter(F.chat.id == cfg.admin_group_id)
    # Кнопки залишків живуть тільки в адмін-групі — з приватних чатів їх не смикнути
    router.callback_query.filter(F.message.chat.id == cfg.admin_group_id)

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

        # real_reply, а не message.reply_to_message: у темі forum-псевдореплай
        # підсунув би службове повідомлення теми замість справжнього допису
        src = real_reply(message)
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
        page_id = db.page_for_order(number)
        if page_id is None:
            await message.reply(f"❌ Замовлення #{number} не знайдено.")
            return
        try:
            await orders.set_packed(page_id, packed=(status == STATUS_PACKED))
        except Exception:
            log.exception("Помилка Notion при зміні статусу #%s", number)
            await message.reply("❌ Notion недоступний, спробуйте пізніше.")
            return
        # Клієнта сповіщаємо, якщо знаємо, чиє це замовлення. Старі записи
        # (до появи user_id у order_rows) тихо лишаються без сповіщення.
        notified = ""
        client_id = db.user_for_order(number)
        if client_id:
            try:
                await message.bot.send_message(
                    client_id,
                    "📦 Ваше замовлення "
                    f"<b>{db.label_for_order(number) or f'№{number}'}</b>: <b>{status}</b>",
                )
                notified = " · клієнта сповіщено"
            except Exception:
                log.warning("Не вдалося сповістити клієнта %s про #%s", client_id, number, exc_info=True)
                notified = " · клієнту не доставлено"
        await message.reply(f"✅ Замовлення #{number} → {status}{notified}")

    @router.message(Command("orders"))
    async def cmd_orders(message: Message):
        if not _allowed_here(message):
            return
        try:
            # list_orders уже віддає лише незапаковані, найновіші спершу
            pending = (await orders.list_orders(100))[:10]
        except Exception:
            log.exception("Помилка Notion при запиті замовлень")
            await message.reply("❌ Notion недоступний, спробуйте пізніше.")
            return
        if not pending:
            await message.reply("Немає незапакованих замовлень 🎉")
            return
        lines = ["🟡 <b>Незапаковані замовлення (останні 10):</b>"]
        for o in pending:
            number = db.order_for_page(o["page_id"])
            label = f"#{number}" if number is not None else "без номера"
            items = o["items"] if len(o["items"]) <= 60 else o["items"][:60] + "…"
            lines.append(
                f"• <b>{label} {html.escape(o['fio'])}</b> — "
                f"{html.escape(items)} · {html.escape(o['phone'])}"
            )
        lines.append("\nЗмінити статус: <code>/status &lt;номер&gt; &lt;статус&gt;</code>")
        await message.reply("\n".join(lines))

    # ---------- залишки товарів ----------

    @router.message(Command("remnants"))
    async def cmd_remnants(message: Message):
        """Список товарів із залишками. Кнопка на товарі → запит нової кількості."""
        if not _allowed_here(message):
            return
        db.clear_stock_prompt(message.chat.id, message.from_user.id)
        await message.reply(stock_text(), reply_markup=stock_kb())

    @router.callback_query(F.data.startswith("stock:"))
    async def cb_stock(callback: CallbackQuery):
        target = callback.data.split(":", 1)[1]
        chat_id = callback.message.chat.id
        if target == "list":
            db.clear_stock_prompt(chat_id, callback.from_user.id)
            await callback.message.edit_text(stock_text(), reply_markup=stock_kb())
            await callback.answer()
            return
        product = PRODUCT_BY_ID.get(target)
        if not product:
            await callback.answer("Товар не знайдено.", show_alert=True)
            return
        # Запит живе в SQLite, а не в пам'яті процесу: інакше деплой Railway
        # посеред діалогу лишає адміна з мовчазним ботом
        db.set_stock_prompt(chat_id, callback.from_user.id, product.id, callback.message.message_id)
        await callback.message.edit_text(
            f"📦 <b>{html.escape(product.title)}</b>"
            f"\nЗараз: <b>{_left_label(product)}</b>"
            "\n\n🔢 Скільки одиниць лишилось? Напишіть число"
            " — окремим повідомленням або reply на це."
            "\n0 — товар зникне з продажу («немає у наявності»).",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="« Повернення", callback_data="stock:list")]
                ]
            ),
        )
        await callback.answer()

    def is_stock_answer(message: Message) -> bool:
        """Число від того, кого бот питав про залишок. Reply приймаємо лише на
        сам запит — інакше «20» у відповідь клієнту пішло б у залишки."""
        prompt = db.get_stock_prompt(message.chat.id, message.from_user.id)
        if not prompt:
            return False
        reply = real_reply(message)
        return reply is None or reply.message_id == prompt[1]

    @router.message(F.text.regexp(r"^\d+$"), is_stock_answer)
    async def stock_quantity(message: Message, bot: Bot):
        product_id, msg_id = db.get_stock_prompt(message.chat.id, message.from_user.id)
        product = PRODUCT_BY_ID[product_id]
        qty = int(message.text.strip())
        db.set_stock(product.id, qty)
        db.clear_stock_prompt(message.chat.id, message.from_user.id)

        # Список перемальовуємо в тому самому повідомленні, щоб тема не засмічувалась
        try:
            await bot.edit_message_text(
                stock_text(),
                chat_id=message.chat.id,
                message_id=msg_id,
                reply_markup=stock_kb(),
            )
        except Exception:
            log.debug("Не вдалося оновити список залишків", exc_info=True)

        note = f"✅ <b>{html.escape(product.title)}</b> — залишок: <b>{qty} шт</b>"
        if qty == 0:
            note += "\n🚫 У каталозі показується як «немає у наявності»."
        if not product.available:
            note += (
                "\n⚠️ Цей товар вимкнено в каталозі (available=False) — "
                "клієнти його не замовлять, хоч би який був залишок."
            )
        await message.reply(note)

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
