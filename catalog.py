"""Каталог товарів. Щоб додати позицію — допишіть словник у потрібну категорію."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Product:
    id: str
    title: str
    price: int
    description: str


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
)

CATEGORY_BY_ID = {c.id: c for c in CATEGORIES}
PRODUCT_BY_ID = {p.id: p for c in CATEGORIES for p in c.products}
