"""Покрокова форма замовлення (FSM) під структуру «Shipment Registry»:
прізвище → ім'я → по-батькові → телефон → тип отримання → область → місто →
відділення/адреса → склад замовлення → підтвердження."""
import html
import logging
import re
from datetime import datetime, timezone

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import db
from config import Config
from notion_service import DELIVERY_TYPES, NotionService

log = logging.getLogger(__name__)

PHONE_RE = re.compile(r"^\+?[\d\s\-()]{7,20}$")

# Підказка для кроку «адреса» залежно від типу отримання
ADDRESS_PROMPTS = {
    "Відділення": "🏤 Номер відділення Нової пошти? (напр., Відділення №12)",
    "Поштомат": "📮 Номер поштомату Нової пошти? (напр., Поштомат №4523)",
    "Адресна доставка": "🏠 Адреса доставки? (вулиця, будинок, квартира)",
    "Укр.пошта": "🏤 Індекс та адреса відділення Укрпошти?",
}


class OrderForm(StatesGroup):
    surname = State()
    name = State()
    patronymic = State()
    phone = State()
    delivery = State()
    region = State()
    city = State()
    address = State()
    items = State()
    confirm = State()


def _summary(data: dict) -> str:
    patronymic = f" {data['patronymic']}" if data.get("patronymic") else ""
    return (
        "📋 <b>Ваше замовлення:</b>\n"
        f"👤 {html.escape(data['surname'])} {html.escape(data['name'])}{html.escape(patronymic)}\n"
        f"📞 {html.escape(data['phone'])}\n"
        f"🚚 {html.escape(data['delivery_type'])}: {html.escape(data['region'])} обл., "
        f"{html.escape(data['city'])}, {html.escape(data['address'])}\n"
        f"📦 {html.escape(data['items'])}"
    )


def _confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Підтвердити", callback_data="order:confirm"),
                InlineKeyboardButton(text="❌ Скасувати", callback_data="order:cancel"),
            ]
        ]
    )


def _delivery_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=dt, callback_data=f"dt:{i}")]
            for i, dt in enumerate(DELIVERY_TYPES)
        ]
    )


def build_order_router(cfg: Config, notion: NotionService) -> Router:
    router = Router(name="order")
    router.message.filter(F.chat.type == "private")

    @router.message(Command("start"))
    async def cmd_start(message: Message, state: FSMContext):
        await state.clear()
        await message.answer(
            "Вітаємо! 👋\n\n"
            "🛒 /order — оформити замовлення\n"
            "❌ /cancel — скасувати оформлення\n\n"
            "Або просто напишіть повідомлення — ми відповімо."
        )

    @router.message(Command("cancel"))
    async def cmd_cancel(message: Message, state: FSMContext):
        if await state.get_state() is None:
            await message.answer("Зараз нема чого скасовувати 🙂 /order — оформити замовлення.")
            return
        await state.clear()
        await message.answer("❌ Оформлення скасовано. /order — почати заново.")

    @router.message(Command("order"))
    async def cmd_order(message: Message, state: FSMContext):
        await state.set_state(OrderForm.surname)
        await message.answer(
            "🛒 Оформлюємо замовлення. Заповнимо дані для відправки.\n\n"
            "👤 Ваше <b>прізвище</b>?\n\n(/cancel — скасувати в будь-який момент)"
        )

    @router.message(OrderForm.surname, F.text)
    async def step_surname(message: Message, state: FSMContext):
        surname = message.text.strip()
        if len(surname) < 2:
            await message.answer("Закоротко. Напишіть, будь ласка, прізвище.")
            return
        await state.update_data(surname=surname)
        await state.set_state(OrderForm.name)
        await message.answer("👤 Ваше <b>ім'я</b>?")

    @router.message(OrderForm.name, F.text)
    async def step_name(message: Message, state: FSMContext):
        name = message.text.strip()
        if len(name) < 2:
            await message.answer("Закоротко. Напишіть, будь ласка, ім'я.")
            return
        await state.update_data(name=name)
        await state.set_state(OrderForm.patronymic)
        await message.answer("👤 <b>По-батькові</b>? (або надішліть «-», щоб пропустити)")

    @router.message(OrderForm.patronymic, F.text)
    async def step_patronymic(message: Message, state: FSMContext):
        patronymic = message.text.strip()
        await state.update_data(patronymic="" if patronymic == "-" else patronymic)
        await state.set_state(OrderForm.phone)
        await message.answer("📞 Номер телефону? (наприклад, +380 67 123 45 67)")

    @router.message(OrderForm.phone, F.text)
    async def step_phone(message: Message, state: FSMContext):
        phone = message.text.strip()
        # Нормалізація: лишаємо цифри; префікс +38/38 не рахуємо —
        # має залишитися щонайменше 10 цифр (0XX XXX XX XX)
        digits = re.sub(r"\D", "", phone)
        if digits.startswith("380"):
            digits = digits[2:]
        if not PHONE_RE.match(phone) or len(digits) < 10:
            await message.answer(
                "Номер закороткий або в неправильному форматі 🤔\n"
                "Введіть, будь ласка, повний номер, наприклад:\n"
                "<code>+380 96 054 64 56</code> або <code>096 034 07 52</code>"
            )
            return
        await state.update_data(phone=phone)
        await state.set_state(OrderForm.delivery)
        await message.answer("🚚 Як зручніше отримати?", reply_markup=_delivery_kb())

    @router.callback_query(OrderForm.delivery, F.data.startswith("dt:"))
    async def cb_delivery(callback: CallbackQuery, state: FSMContext):
        try:
            delivery_type = DELIVERY_TYPES[int(callback.data.split(":", 1)[1])]
        except (ValueError, IndexError):
            await callback.answer()
            return
        await state.update_data(delivery_type=delivery_type)
        await state.set_state(OrderForm.region)
        await callback.message.edit_text(f"🚚 Тип отримання: <b>{delivery_type}</b>")
        await callback.message.answer("🗺 Область? (напр., Київська)")
        await callback.answer()

    @router.message(OrderForm.delivery)
    async def step_delivery_hint(message: Message):
        await message.answer("Оберіть, будь ласка, тип отримання кнопкою вище 🙂")

    @router.message(OrderForm.region, F.text)
    async def step_region(message: Message, state: FSMContext):
        region = message.text.strip().removesuffix("область").removesuffix("обл.").strip()
        if len(region) < 3:
            await message.answer("Закоротко. Напишіть, будь ласка, область.")
            return
        await state.update_data(region=region)
        await state.set_state(OrderForm.city)
        await message.answer("🏙 Місто / населений пункт?")

    @router.message(OrderForm.city, F.text)
    async def step_city(message: Message, state: FSMContext):
        city = message.text.strip()
        if len(city) < 2:
            await message.answer("Закоротко. Напишіть, будь ласка, місто.")
            return
        await state.update_data(city=city)
        await state.set_state(OrderForm.address)
        data = await state.get_data()
        await message.answer(ADDRESS_PROMPTS[data["delivery_type"]])

    @router.message(OrderForm.address, F.text)
    async def step_address(message: Message, state: FSMContext):
        address = message.text.strip()
        if len(address) < 2:
            await message.answer("Закоротко. Напишіть, будь ласка, ще раз.")
            return
        await state.update_data(address=address)
        await state.set_state(OrderForm.items)
        await message.answer(
            "📦 Що замовляєте? Напишіть перелік і кількість,\n"
            "напр.: «Володар Перснів» ×1, «Кобзар» ×2"
        )

    @router.message(OrderForm.items, F.text)
    async def step_items(message: Message, state: FSMContext):
        items = message.text.strip()
        if len(items) < 2:
            await message.answer("Закоротко. Напишіть, будь ласка, що замовляєте.")
            return
        await state.update_data(items=items)
        await state.set_state(OrderForm.confirm)
        data = await state.get_data()
        await message.answer(_summary(data) + "\n\nВсе вірно?", reply_markup=_confirm_kb())

    @router.callback_query(OrderForm.confirm, F.data == "order:cancel")
    async def cb_cancel(callback: CallbackQuery, state: FSMContext):
        await state.clear()
        await callback.message.edit_text("❌ Оформлення скасовано. /order — почати заново.")
        await callback.answer()

    @router.callback_query(OrderForm.confirm, F.data == "order:confirm")
    async def cb_confirm(callback: CallbackQuery, state: FSMContext, bot: Bot):
        data = await state.get_data()
        await state.clear()
        user = callback.from_user

        number = db.next_order_number()
        order = {
            "number": number,
            "user_id": user.id,
            "username": f"@{user.username}" if user.username else None,
            "surname": data["surname"],
            "name": data["name"],
            "patronymic": data.get("patronymic", ""),
            "phone": data["phone"],
            "delivery_type": data["delivery_type"],
            "region": data["region"],
            "city": data["city"],
            "address": data["address"],
            "items": data["items"],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        saved_offline = False
        try:
            await notion.create_order(order)
        except Exception:
            log.exception("Notion недоступний, замовлення #%s йде в офлайн-чергу", number)
            notion.queue_order(order)
            saved_offline = True

        await callback.message.edit_text(
            _summary(data)
            + f"\n\n✅ Дякуємо! Ваше замовлення <b>№{number}</b> прийнято.\n"
            "Ми зв'яжемося з вами найближчим часом."
        )
        await callback.answer("Замовлення прийнято!")

        # Сповіщення в тему «Замовлення» в адмін-групі
        who = f"@{user.username}" if user.username else user.full_name
        admin_text = (
            f"🆕 <b>Замовлення #{number}</b> від {html.escape(who)}\n"
            + _summary(data)
            + "\n\n💬 Reply на це повідомлення — відповідь піде клієнту."
        )
        if saved_offline:
            admin_text += (
                "\n\n⚠️ Notion недоступний — замовлення збережено локально "
                "і буде довантажено автоматично."
            )
        try:
            db.upsert_client(user.id, user.full_name, user.username)
            sent = await bot.send_message(
                cfg.admin_group_id, admin_text, message_thread_id=cfg.bot_topic_id
            )
            db.map_group_message(sent.message_id, user.id)
        except Exception:
            log.exception("Не вдалося сповістити адмін-групу про замовлення #%s", number)

    @router.message(StateFilter(OrderForm))
    async def fsm_fallback(message: Message):
        await message.answer(
            "Надішліть, будь ласка, текстове повідомлення 🙂\n/cancel — скасувати оформлення."
        )

    return router
