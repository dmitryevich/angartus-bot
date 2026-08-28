"""Магазин: каталог → кошик → оплата з фото квитанції → дані клієнта → Google Таблиця.

Уся навігація — inline-кнопки під текстом повідомлення. Нижня панель (ReplyKeyboard)
не використовується взагалі, тому кнопки ніколи не «зависають» від попереднього кроку.
Перехід між екранами редагує те саме повідомлення, тож чат не засмічується.
"""
import html
import logging
import re
from datetime import datetime, timezone

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

import db
from catalog import CATEGORIES, CATEGORY_BY_ID, PRODUCT_BY_ID
from config import Config
from sheets_service import DELIVERY_TYPES, SheetsService

log = logging.getLogger(__name__)

PHONE_RE = re.compile(r"^\+?[\d\s\-()]{7,20}$")

BACK = "« Повернення"
BTN_CHECKOUT = "🛒 До корзини"


def checkout_kb() -> ReplyKeyboardMarkup:
    """Велика кнопка внизу — зʼявляється, щойно в корзині є бодай одна позиція."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_CHECKOUT)]], resize_keyboard=True
    )

# Поля, де значення може бути коротким (напр., «Відділення №1») — довжину не перевіряємо
FREEFORM_FIELDS = {"address", "patronymic", "district"}
# Поля, які можна пропустити, надіславши «-»
SKIPPABLE_FIELDS = {"patronymic", "district"}

CAPTION_LIMIT = 1024  # ліміт Telegram на підпис до фото/документа

# <code> робить значення клікабельним: тап по ньому копіює текст
PAYMENT_DETAILS = (
    "Поповнення за реквізитами:\n\n"
    "Отримувач: <code>ФОП Жовтяк Дмитро Юрійович</code>\n"
    "IBAN: <code>UA753220010000026008380028271</code>\n"
    "ІПН/ЄДРПОУ: <code>3854312453</code>\n"
    "Призначення платежу: <code>оплата за товар</code>\n\n"
    "Натисніть на реквізити — вони скопіюються.\n\n"
    "Після оплати надішліть скрін оплати."
)

# Підказка для кроку «адреса» залежно від типу отримання
ADDRESS_PROMPTS = {
    "Відділення": "🏤 Номер відділення Нової пошти? (напр., Відділення №12)",
    "Поштомат": "📮 Номер поштомату Нової пошти? (напр., Поштомат №4523)",
    "Адресна доставка": "🏠 Адреса доставки? (вулиця, будинок, квартира)",
    "Укрпошта": "🏤 Індекс та адреса відділення Укрпошти?",
}

# Порядок кроків анкети: телефон → ПІБ → область/район/місто → тип доставки → адреса
FIELDS = [
    ("phone", "Телефон", "📞 Номер телефону? (наприклад, +380 67 123 45 67)"),
    ("surname", "Прізвище", "👤 Ваше <b>прізвище</b>?"),
    ("name", "Ім'я", "👤 Ваше <b>ім'я</b>?"),
    ("patronymic", "По-батькові", "👤 <b>По-батькові</b>? (або надішліть «-», щоб пропустити)"),
    ("region", "Область", "🗺 Область? (напр., Київська)"),
    ("district", "Район", "🗺 Район? (або надішліть «-», щоб пропустити)"),
    ("city", "Місто", "🏙 Місто / населений пункт?"),
    ("delivery_type", "Тип отримання", "🚚 Як зручніше отримати?"),
    ("address", "Адреса / відділення", None),  # текст залежить від типу отримання
]
FIELD_LABELS = {key: label for key, label, _ in FIELDS}


class Shop(StatesGroup):
    quantity = State()
    receipt = State()
    field = State()


def ikb(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    """rows — списки пар (текст, callback_data)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t, callback_data=cb) for t, cb in row] for row in rows
        ]
    )


def main_kb() -> InlineKeyboardMarkup:
    return ikb(
        [("🛒 Замовити", "menu:cats")],
        [("📖 Мої замовлення", "menu:orders")],
        [("✉️ Зв'язок", "menu:contact")],
    )


def orders_kb(orders: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    """Список замовлень клієнта — інлайн, по кнопці на замовлення."""
    rows = [[(label, f"myord:{number}")] for number, label in orders]
    rows.append([(BACK, "menu:main")])
    return ikb(*rows)


def back_kb(target: str = "menu:main") -> InlineKeyboardMarkup:
    return ikb([(BACK, target)])


def categories_kb() -> InlineKeyboardMarkup:
    rows = [[(c.title, f"cat:{c.id}")] for c in CATEGORIES]
    rows.append([(BACK, "menu:main")])
    return ikb(*rows)


def category_kb(category_id: str) -> InlineKeyboardMarkup:
    rows = [
        [(f"{p.title} — {p.price} грн", f"prod:{p.id}")]
        for p in CATEGORY_BY_ID[category_id].products
    ]
    rows.append([(BACK, "menu:cats")])
    return ikb(*rows)


def cart_kb() -> InlineKeyboardMarkup:
    return ikb(
        [("✅ Так, оформити", "cart:ok")],
        [("🗑 Відхилити корзину", "cart:clear")],
        [(BACK, "menu:cats")],
    )


def delivery_kb() -> InlineKeyboardMarkup:
    return ikb(*[[(dt, f"dt:{i}")] for i, dt in enumerate(DELIVERY_TYPES)])


def review_kb() -> InlineKeyboardMarkup:
    return ikb([("✅ Підтвердити", "order:confirm")], [("✏️ Змінити", "edit:menu")])


def edit_menu_kb() -> InlineKeyboardMarkup:
    rows = [[(label, f"edit:{key}")] for key, label, _ in FIELDS]
    rows.append([(BACK, "data:review")])
    return ikb(*rows)


def category_text(category_id: str) -> str:
    """Тільки заголовок — самі позиції живуть у кнопках."""
    return f"<b>{html.escape(CATEGORY_BY_ID[category_id].title)}</b>"


def cart_items(data: dict) -> dict:
    return data.get("cart") or {}


def cart_total(cart: dict) -> int:
    return sum(PRODUCT_BY_ID[pid].price * qty for pid, qty in cart.items() if pid in PRODUCT_BY_ID)


def cart_text(cart: dict) -> str:
    lines = []
    for pid, qty in cart.items():
        product = PRODUCT_BY_ID.get(pid)
        if not product:
            continue
        lines.append(f"• {html.escape(product.title)} × {qty} — {product.price * qty} грн")
    return "\n".join(lines)


def cart_plain(cart: dict) -> str:
    """Рядок для колонки «Склад замовлення» в Таблиці."""
    parts = []
    for pid, qty in cart.items():
        product = PRODUCT_BY_ID.get(pid)
        if product:
            parts.append(f"{product.title} ×{qty}")
    return ", ".join(parts)


def order_status(o: dict) -> str:
    """Статус словами. Свіже замовлення ще не має рядка в Таблиці — тоді «Прийнято»."""
    if "packed" not in o:
        return "✅ Прийнято"
    if o.get("ttn"):
        return "🚚 Відправлено"
    return "🟢 Запаковано" if o["packed"] else "🟡 В роботі"


def order_card(number: int, o: dict, total: int | None = None) -> str:
    """Картка замовлення — і одразу після оформлення, і в «Моїх замовленнях»."""
    fio = " ".join(
        p for p in (o.get("surname"), o.get("name"), o.get("patronymic")) if p
    ).strip()
    place = ", ".join(p for p in (o.get("region"), o.get("district"), o.get("city")) if p)

    lines = [
        f"🧾 <b>Замовлення №{number}</b>",
        f"Статус: <b>{order_status(o)}</b>",
    ]
    if o.get("ttn"):
        lines.append(f"📦 ТТН: <code>{html.escape(o['ttn'])}</code>")
    lines += [
        "",
        f"👤 {html.escape(fio) or '—'}",
        f"📱 {html.escape(o.get('phone') or '—')}",
        "",
        f"🚚 {html.escape(o.get('delivery_type') or '—')}",
        f"📍 {html.escape(place) or '—'}",
        f"🏠 {html.escape(o.get('address') or '—')}",
        "",
        "🛒 <b>Замовлення:</b>",
        html.escape(o.get("items") or "—"),
    ]
    if total is not None:
        lines.append(f"\n💰 Разом: <b>{total} грн</b>")
    return "\n".join(lines)


def review_text(data: dict) -> str:
    cart = cart_items(data)
    patronymic = data.get("patronymic") or ""
    fio = " ".join(p for p in (data.get("surname"), data.get("name"), patronymic) if p)
    return (
        "📋 <b>Перевірте дані:</b>\n"
        f"👤 {html.escape(fio)}\n"
        f"📞 {html.escape(data.get('phone', '—'))}\n"
        f"🚚 {html.escape(data.get('delivery_type', '—'))}: "
        f"{html.escape(data.get('region', '—'))} обл."
        + (f", {html.escape(data['district'])} р-н" if data.get("district") else "")
        + f", {html.escape(data.get('city', '—'))}, "
        f"{html.escape(data.get('address', '—'))}\n\n"
        f"🧾 <b>Замовлення:</b>\n{cart_text(cart)}\n"
        f"Разом: <b>{cart_total(cart)} грн</b>\n\n"
        "Все вірно?"
    )


async def swap(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    """Перемальовує поточний екран у тому самому повідомленні.
    Якщо відредагувати не можна (напр., це фото) — надсилає нове."""
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except Exception:
        await callback.message.answer(text, reply_markup=markup)


def build_order_router(cfg: Config, sheets: SheetsService) -> Router:
    router = Router(name="order")
    router.message.filter(F.chat.type == "private")

    # ---------- екрани ----------

    async def ask_field(message: Message, state: FSMContext, key: str, prefix: str = "") -> None:
        """Питання по конкретному полю анкети.
        prefix клеїмо до тексту, щоб не плодити зайвих повідомлень."""
        await state.update_data(current_field=key)
        await state.set_state(Shop.field)
        if key == "delivery_type":
            await message.answer(f"{prefix}🚚 Як зручніше отримати?", reply_markup=delivery_kb())
            return
        if key == "address":
            data = await state.get_data()
            prompt = ADDRESS_PROMPTS.get(data.get("delivery_type"), "🏠 Адреса доставки?")
            await message.answer(f"{prefix}{prompt}")
            return
        prompt = next(p for k, _, p in FIELDS if k == key)
        await message.answer(f"{prefix}{prompt}")

    async def next_field(message: Message, state: FSMContext, current: str) -> None:
        """Після відповіді: якщо це правка одного поля — назад на перевірку,
        інакше наступне питання анкети."""
        data = await state.get_data()
        if data.get("editing_single"):
            await state.update_data(editing_single=False, current_field=None)
            await state.set_state(None)
            await message.answer(review_text(await state.get_data()), reply_markup=review_kb())
            return
        keys = [k for k, _, _ in FIELDS]
        idx = keys.index(current)
        if idx + 1 < len(keys):
            await ask_field(message, state, keys[idx + 1])
        else:
            await state.set_state(None)
            await message.answer(review_text(await state.get_data()), reply_markup=review_kb())

    # ---------- команди ----------

    @router.message(Command("start"))
    async def cmd_start(message: Message, state: FSMContext):
        await state.set_state(None)
        user = message.from_user
        db.upsert_client(user.id, user.full_name, user.username)
        try:
            await sheets.upsert_client(user.id, user.full_name, user.username)
        except Exception:
            log.warning("Не вдалося записати клієнта %s у Таблицю", user.id, exc_info=True)
        await message.answer(
            "Вітаємо у боті Видавництва «Ангарта»! 👋\n\nОберіть дію:",
            reply_markup=main_kb(),
        )

    @router.message(Command("cancel"))
    async def cmd_cancel(message: Message, state: FSMContext):
        data = await state.get_data()
        cart = cart_items(data)
        await state.clear()
        await state.update_data(cart=cart)
        await message.answer("❌ Скасовано. Оберіть дію:", reply_markup=main_kb())

    # ---------- велика кнопка «До корзини» ----------

    @router.message(F.text == BTN_CHECKOUT)
    async def btn_checkout(message: Message, state: FSMContext):
        if await state.get_state() in (Shop.field.state, Shop.receipt.state):
            await message.answer("Спочатку завершіть оформлення поточного замовлення 🙂")
            return
        await state.set_state(None)
        data = await state.get_data()
        cart = cart_items(data)
        if not cart:
            await message.answer("🧾 Корзина порожня.", reply_markup=ReplyKeyboardRemove())
            await message.answer("🗂 Оберіть категорію:", reply_markup=categories_kb())
            return
        sent = await message.answer(
            f"🧾 <b>Ваше замовлення:</b>\n{cart_text(cart)}\n\n"
            f"Разом: <b>{cart_total(cart)} грн</b>\n\nОформити?",
            reply_markup=cart_kb(),
        )
        await state.update_data(screen_msg_id=sent.message_id)

    # ---------- меню ----------

    @router.callback_query(F.data == "menu:main")
    async def cb_main(callback: CallbackQuery, state: FSMContext):
        await state.set_state(None)
        await swap(callback, "Головне меню. Оберіть дію:", main_kb())
        await callback.answer()

    @router.callback_query(F.data == "menu:contact")
    async def cb_contact(callback: CallbackQuery, state: FSMContext):
        await state.set_state(None)
        await swap(
            callback,
            "✉️ Напишіть ваше повідомлення — ми його прочитаємо і відповімо, щойно зможемо.",
            back_kb(),
        )
        await callback.answer()

    @router.callback_query(F.data == "menu:cats")
    async def cb_cats(callback: CallbackQuery, state: FSMContext):
        await state.set_state(None)
        await swap(callback, "🗂 Оберіть категорію:", categories_kb())
        await state.update_data(screen_msg_id=callback.message.message_id)
        await callback.answer()

    # ---------- мої замовлення ----------

    @router.callback_query(F.data == "menu:orders")
    async def cb_my_orders(callback: CallbackQuery, state: FSMContext):
        await state.set_state(None)
        pairs = db.orders_for_user(callback.from_user.id, limit=10)
        if not pairs:
            await swap(
                callback,
                "📖 У вас поки що немає замовлень.",
                back_kb("menu:main"),
            )
            await callback.answer()
            return

        buttons: list[tuple[int, str]] = []
        for number, row in pairs:
            try:
                o = await sheets.get_order(row)
            except Exception:
                log.warning("Таблиця недоступна при читанні замовлення #%s", number, exc_info=True)
                o = None
            # Підпис кнопки — номер і статус, щоб не тицяти навмання
            suffix = f" — {order_status(o)}" if o else ""
            buttons.append((number, f"№{number}{suffix}"))

        await swap(callback, "📖 <b>Ваші замовлення</b>", orders_kb(buttons))
        await callback.answer()

    @router.callback_query(F.data.startswith("myord:"))
    async def cb_my_order(callback: CallbackQuery):
        raw = callback.data.split(":", 1)[1]
        if not raw.isdigit():
            await callback.answer()
            return
        number = int(raw)
        # Чуже замовлення не показуємо, навіть якщо хтось підробить callback
        if db.user_for_order(number) != callback.from_user.id:
            await callback.answer("Замовлення не знайдено.", show_alert=True)
            return
        row = db.row_for_order(number)
        try:
            o = await sheets.get_order(row) if row else None
        except Exception:
            log.exception("Таблиця недоступна при показі замовлення #%s", number)
            await callback.answer("Таблиця тимчасово недоступна 🙁", show_alert=True)
            return
        if not o:
            await swap(callback, f"Замовлення №{number} не знайдено.", back_kb("menu:orders"))
            await callback.answer()
            return
        await swap(callback, order_card(number, o), back_kb("menu:orders"))
        await callback.answer()

    @router.callback_query(F.data == "promo:order")
    async def cb_promo_order(callback: CallbackQuery, state: FSMContext):
        """Кнопка «Замовити» під розсилкою — саму розсилку не чіпаємо, шлемо новий екран."""
        await state.set_state(None)
        sent = await callback.message.answer("🗂 Оберіть категорію:", reply_markup=categories_kb())
        await state.update_data(screen_msg_id=sent.message_id)
        await callback.answer()

    # ---------- каталог ----------

    @router.callback_query(F.data.startswith("cat:"))
    async def cb_category(callback: CallbackQuery, state: FSMContext):
        cat_id = callback.data.split(":", 1)[1]
        if cat_id not in CATEGORY_BY_ID:
            await callback.answer()
            return
        await state.update_data(screen_cat=cat_id, screen_msg_id=callback.message.message_id)
        await swap(callback, category_text(cat_id), category_kb(cat_id))
        await callback.answer()

    @router.callback_query(F.data.startswith("prod:"))
    async def cb_product(callback: CallbackQuery, state: FSMContext):
        product = PRODUCT_BY_ID.get(callback.data.split(":", 1)[1])
        if not product:
            await callback.answer()
            return
        cat_id = next(c.id for c in CATEGORIES if any(p.id == product.id for p in c.products))
        if not product.available:
            # Позиція лишається в меню, але замовити її не можна — у стан
            # Shop.quantity не заходимо, щоб бот не питав кількість.
            await state.set_state(None)
            await state.update_data(pending_product=None, screen_cat=cat_id)
            await swap(
                callback,
                "Упс... Цього товару більше немає у наявності",
                ikb([("🗂 До категорій", "menu:cats")]),
            )
            await callback.answer()
            return
        await state.update_data(
            pending_product=product.id,
            screen_cat=cat_id,
            screen_msg_id=callback.message.message_id,
        )
        await state.set_state(Shop.quantity)
        await swap(
            callback,
            f"📌 <b>{html.escape(product.title)}</b>\n"
            f"{html.escape(product.description)}\n"
            f"Ціна: <b>{product.price} грн</b>\n\n"
            "🔢 Скільки одиниць? Напишіть число:",
            back_kb(f"cat:{cat_id}"),
        )
        await callback.answer()

    @router.message(Shop.quantity, F.text)
    async def step_quantity(message: Message, state: FSMContext, bot: Bot):
        raw = message.text.strip()
        if not raw.isdigit() or int(raw) < 1:
            await message.answer("Введіть, будь ласка, число більше нуля 🙂")
            return
        qty = int(raw)
        data = await state.get_data()
        product = PRODUCT_BY_ID.get(data.get("pending_product"))
        if not product:
            await state.set_state(None)
            await message.answer("🗂 Оберіть категорію:", reply_markup=categories_kb())
            return
        cart = dict(cart_items(data))
        cart[product.id] = cart.get(product.id, 0) + qty
        await state.update_data(cart=cart, pending_product=None)
        await state.set_state(None)

        cat_id = next(c.id for c in CATEGORIES if any(p.id == product.id for p in c.products))
        # Екран каталогу повертаємо на місце (редагуємо старе повідомлення, не плодимо нові)
        screen_msg_id = data.get("screen_msg_id")
        if screen_msg_id:
            try:
                await bot.edit_message_text(
                    category_text(cat_id),
                    chat_id=message.chat.id,
                    message_id=screen_msg_id,
                    reply_markup=category_kb(cat_id),
                )
            except Exception:
                log.debug("Не вдалося оновити екран каталогу", exc_info=True)
        # А підтвердження несе на собі велику кнопку «До корзини»
        await message.answer(
            f"✅ Додано: {html.escape(product.title)} × {qty}.\n"
            f"🧾 У корзині на <b>{cart_total(cart)} грн</b>.",
            reply_markup=checkout_kb(),
        )

    # ---------- кошик ----------

    @router.callback_query(F.data == "cart:show")
    async def cb_cart(callback: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        cart = cart_items(data)
        if not cart:
            await swap(callback, "🧾 Корзина порожня.\n\n🗂 Оберіть категорію:", categories_kb())
            await callback.answer()
            return
        await swap(
            callback,
            f"🧾 <b>Ваше замовлення:</b>\n{cart_text(cart)}\n\n"
            f"Разом: <b>{cart_total(cart)} грн</b>\n\nОформити?",
            cart_kb(),
        )
        await callback.answer()

    @router.callback_query(F.data == "cart:clear")
    async def cb_cart_clear(callback: CallbackQuery, state: FSMContext):
        await state.update_data(cart={}, pending_product=None)
        await state.set_state(None)
        await swap(callback, "🗑 Корзину очищено.\n\n🗂 Оберіть категорію:", categories_kb())
        # Корзина порожня — велика кнопка більше не потрібна
        await callback.message.answer("Корзина порожня.", reply_markup=ReplyKeyboardRemove())
        await callback.answer("Корзину очищено")

    @router.callback_query(F.data == "cart:ok")
    async def cb_cart_ok(callback: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        cart = cart_items(data)
        if not cart:
            await swap(callback, "🧾 Корзина порожня.\n\n🗂 Оберіть категорію:", categories_kb())
            await callback.answer()
            return
        await state.set_state(Shop.receipt)
        await swap(
            callback,
            f"💳 Сума до оплати: <b>{cart_total(cart)} грн</b>\n\n{PAYMENT_DETAILS}",
            back_kb("cart:show"),
        )
        await callback.answer()

    @router.message(Shop.receipt, F.photo | F.document)
    async def step_receipt(message: Message, state: FSMContext):
        # Квитанцію не пересилаємо одразу: вона піде в адмін-групу одним
        # повідомленням разом із даними замовлення (див. cb_confirm)
        if message.photo:
            await state.update_data(receipt_file_id=message.photo[-1].file_id, receipt_kind="photo")
        else:
            await state.update_data(
                receipt_file_id=message.document.file_id, receipt_kind="document"
            )
        await ask_field(
            message,
            state,
            FIELDS[0][0],
            prefix="✅ Квитанцію отримано. Тепер заповнимо дані для відправки.\n\n",
        )

    @router.message(Shop.receipt)
    async def step_receipt_wrong(message: Message):
        await message.answer("📸 Надішліть, будь ласка, саме фото або скрін квитанції.")

    # ---------- анкета ----------

    @router.callback_query(Shop.field, F.data.startswith("dt:"))
    async def cb_delivery(callback: CallbackQuery, state: FSMContext):
        try:
            value = DELIVERY_TYPES[int(callback.data.split(":", 1)[1])]
        except (ValueError, IndexError):
            await callback.answer()
            return
        await state.update_data(delivery_type=value)
        await swap(callback, f"🚚 Тип отримання: <b>{value}</b>", ikb())
        await callback.answer()
        await next_field(callback.message, state, "delivery_type")

    @router.message(Shop.field, F.text)
    async def step_field(message: Message, state: FSMContext):
        data = await state.get_data()
        key = data.get("current_field")
        if not key:
            await message.answer("Оберіть дію:", reply_markup=main_kb())
            return
        if key == "delivery_type":
            await message.answer("Оберіть, будь ласка, тип отримання кнопкою вище 🙂")
            return
        value = message.text.strip()

        if key in FREEFORM_FIELDS:
            # Адреса/відділення може бути коротким («№1»), тому довжину не міряємо
            if key in SKIPPABLE_FIELDS and value == "-":
                value = ""
            elif not value:
                await message.answer("Напишіть, будь ласка, значення.")
                return
        elif key == "phone":
            digits = re.sub(r"\D", "", value)
            if digits.startswith("380"):
                digits = digits[2:]
            if not PHONE_RE.match(value) or len(digits) < 10:
                await message.answer(
                    "Номер закороткий або в неправильному форматі 🤔\n"
                    "Напишіть, будь ласка, повний номер: "
                    "<code>+380 96 054 64 56</code> або <code>096 034 07 52</code>"
                )
                return
        elif len(value) < 2:
            await message.answer("Закоротко. Напишіть, будь ласка, ще раз.")
            return

        await state.update_data(**{key: value})
        await next_field(message, state, key)

    # ---------- перевірка / правки ----------

    @router.callback_query(F.data == "data:review")
    async def cb_review(callback: CallbackQuery, state: FSMContext):
        await state.set_state(None)
        await swap(callback, review_text(await state.get_data()), review_kb())
        await callback.answer()

    @router.callback_query(F.data == "edit:menu")
    async def cb_edit_menu(callback: CallbackQuery):
        await swap(callback, "✏️ Що саме потрібно змінити?", edit_menu_kb())
        await callback.answer()

    @router.callback_query(F.data.startswith("edit:"))
    async def cb_edit_field(callback: CallbackQuery, state: FSMContext):
        key = callback.data.split(":", 1)[1]
        if key not in FIELD_LABELS:
            await callback.answer()
            return
        await state.update_data(editing_single=True)
        await callback.answer()
        await ask_field(callback.message, state, key)

    # ---------- фінал ----------

    @router.callback_query(F.data == "order:confirm")
    async def cb_confirm(callback: CallbackQuery, state: FSMContext, bot: Bot):
        data = await state.get_data()
        cart = cart_items(data)
        if not cart:
            await swap(callback, "🧾 Корзина порожня. Оберіть дію:", main_kb())
            await callback.answer()
            return
        user = callback.from_user
        number = db.next_order_number()
        summary = review_text(data).replace("Все вірно?", "").strip()
        who = f"@{user.username}" if user.username else user.full_name
        admin_text = (
            f"🆕 <b>Замовлення #{number}</b> від {html.escape(who)}\n{summary}"
            "\n\n💬 Reply на це повідомлення — відповідь піде клієнту."
        )

        # Квитанція і дані йдуть в адмін-групу ОДНИМ повідомленням (фото + підпис),
        # а посилання на нього лягає в колонку M
        receipt_link = ""
        try:
            db.upsert_client(user.id, user.full_name, user.username)
            try:
                await sheets.upsert_client(user.id, user.full_name, user.username)
            except Exception:
                log.warning("Не вдалося записати клієнта %s у Таблицю", user.id, exc_info=True)
            file_id = data.get("receipt_file_id")
            kind = data.get("receipt_kind")
            fits = len(admin_text) <= CAPTION_LIMIT
            common = {
                "chat_id": cfg.admin_group_id,
                "message_thread_id": cfg.bot_topic_id,
                "caption": admin_text if fits else f"🆕 <b>Замовлення #{number}</b>",
            }
            if kind == "photo":
                sent = await bot.send_photo(photo=file_id, **common)
            elif kind == "document":
                sent = await bot.send_document(document=file_id, **common)
            else:
                sent = await bot.send_message(
                    cfg.admin_group_id, admin_text, message_thread_id=cfg.bot_topic_id
                )
                fits = True
            if not fits:
                await bot.send_message(
                    cfg.admin_group_id,
                    admin_text,
                    message_thread_id=cfg.bot_topic_id,
                    reply_to_message_id=sent.message_id,
                )
            db.map_group_message(sent.message_id, user.id)
            internal_id = str(cfg.admin_group_id).removeprefix("-100")
            receipt_link = f"https://t.me/c/{internal_id}/{sent.message_id}"
        except Exception:
            log.exception("Не вдалося сповістити адмін-групу про замовлення #%s", number)

        order = {
            "number": number,
            "user_id": user.id,
            "username": f"@{user.username}" if user.username else None,
            "surname": data.get("surname", ""),
            "name": data.get("name", ""),
            "patronymic": data.get("patronymic", ""),
            "phone": data.get("phone", ""),
            "delivery_type": data.get("delivery_type", ""),
            "region": data.get("region", ""),
            "district": data.get("district", ""),
            "city": data.get("city", ""),
            "address": data.get("address", ""),
            "items": cart_plain(cart),
            "receipt": receipt_link,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        try:
            row = await sheets.create_order(order)
            db.map_order_row(number, row, user.id)
        except Exception:
            log.exception("Google Таблиця недоступна, замовлення #%s йде в офлайн-чергу", number)
            sheets.queue_order(order)
            try:
                await bot.send_message(
                    cfg.admin_group_id,
                    f"⚠️ Замовлення #{number}: Google Таблиця недоступна — "
                    "збережено локально, буде довантажено автоматично.",
                    message_thread_id=cfg.bot_topic_id,
                )
            except Exception:
                log.exception("Не вдалося попередити групу про офлайн-чергу")

        await state.clear()
        await swap(callback, "Головне меню. Оберіть дію:", main_kb())
        # Замовлення оформлене — прибираємо велику кнопку «До корзини»
        await callback.message.answer(
            "🎉 <b>Вітаємо з покупкою!</b>\n\n"
            + order_card(number, order, total=cart_total(cart))
            + "\n\nМи зв'яжемося з вами найближчим часом.",
            reply_markup=ReplyKeyboardRemove(),
        )
        await callback.answer("Замовлення прийнято!")

    return router
