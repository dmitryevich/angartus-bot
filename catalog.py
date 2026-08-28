"""Каталог товарів. Щоб додати позицію — допишіть словник у потрібну категорію.

`available=False` лишає позицію у списку, але не дає її замовити: бот показує
повідомлення «немає у наявності» замість запиту кількості.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Product:
    id: str
    title: str
    price: int
    description: str
    available: bool = True


@dataclass(frozen=True)
class Category:
    id: str
    title: str
    products: tuple[Product, ...]


CATEGORIES: tuple[Category, ...] = (
    Category(
        id="journals",
        title="📖 Журнали",
        products=(
            Product(
                id="patrimonium_valkyrie",
                title="Patrimonium: Valkyrie",
                price=400,
                description="Журнал серії Patrimonium.",
            ),
        ),
    ),
    Category(
        id="merch",
        title="👕 Мерч",
        products=(
            Product(
                id="pin_patronus",
                title="Пін «Anghartus Patronus»",
                price=575,
                description="Посріблений лімітований пін.\nРозмір: 5.8 см",
                available=False,
            ),
        ),
    ),
)

CATEGORY_BY_ID = {c.id: c for c in CATEGORIES}
PRODUCT_BY_ID = {p.id: p for c in CATEGORIES for p in c.products}
