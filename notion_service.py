"""Робота з Notion: вкладка «Shipment Registry» (замовлення), база «Шаблони відповідей»,
офлайн-черга в JSON. Використовує нове Notion API (2025-09-03) з data sources."""
import json
import logging
import time
from pathlib import Path

from notion_client import AsyncClient

from config import Config

log = logging.getLogger(__name__)

STATUS_NEW = "🟡 Нове"
STATUSES = [STATUS_NEW, "🔵 В роботі", "🟢 Виконано", "🔴 Скасовано"]
STATUS_ALIASES = {
    "нове": "🟡 Нове",
    "нові": "🟡 Нове",
    "в роботі": "🔵 В роботі",
    "робота": "🔵 В роботі",
    "виконано": "🟢 Виконано",
    "готово": "🟢 Виконано",
    "скасовано": "🔴 Скасовано",
    "відміна": "🔴 Скасовано",
}
for _s in STATUSES:
    STATUS_ALIASES[_s.lower()] = _s

# Опції select «Тип отримання» у Shipment Registry — назви мають збігатися точно
DELIVERY_TYPES = ["Відділення", "Поштомат", "Адресна доставка", "Укр.пошта"]

# Кеш шаблонів на 5 хвилин, щоб не впиратися в ліміт Notion API (3 запити/сек)
TEMPLATES_TTL = 300


def _rt(text: str) -> dict:
    return {"rich_text": [{"text": {"content": text}}]}


def _plain(prop: dict) -> str:
    """Витягує plain-текст із властивості title або rich_text."""
    parts = prop.get("title") or prop.get("rich_text") or []
    return "".join(p.get("plain_text", "") for p in parts).strip()


class NotionService:
    def __init__(self, cfg: Config):
        self._client = AsyncClient(auth=cfg.notion_token, notion_version="2025-09-03")
        self._orders_ds = cfg.notion_orders_db_id
        self._templates_ds = cfg.notion_templates_db_id
        self._pending_path = Path(cfg.pending_path)
        self._tpl_cache: list[tuple[list[str], str]] = []
        self._tpl_cached_at = 0.0

    # ---------- замовлення (Shipment Registry) ----------

    @staticmethod
    def order_properties(o: dict) -> dict:
        return {
            # «Номер ТТН» (title) бот не чіпає — його заповнюють вручну при відправці
            "№ замовлення": {"number": o["number"]},
            "Дата": {"date": {"start": o["created_at"]}},
            "Статус": {"select": {"name": STATUS_NEW}},
            "User ID": {"number": o["user_id"]},
            "Username": _rt(o.get("username") or "—"),
            "Прізвище": _rt(o["surname"]),
            "Ім'я": _rt(o["name"]),
            "По-батькові": _rt(o.get("patronymic") or "—"),
            "Телефон": _rt(o["phone"]),
            "Тип отримання": {"select": {"name": o["delivery_type"]}},
            "Область": _rt(o["region"]),
            "Місто": _rt(o["city"]),
            "Номер / Адреса": _rt(o["address"]),
            "Склад замовлення": _rt(o["items"]),
            "Тип": {"select": {"name": "Default"}},
        }

    async def create_order(self, order: dict) -> None:
        await self._client.pages.create(
            parent={"type": "data_source_id", "data_source_id": self._orders_ds},
            properties=self.order_properties(order),
        )

    async def set_status(self, number: int, status: str) -> bool:
        """Змінює статус замовлення за «№ замовлення». False — не знайдено."""
        res = await self._client.data_sources.query(
            self._orders_ds,
            filter={"property": "№ замовлення", "number": {"equals": number}},
            page_size=1,
        )
        results = res.get("results", [])
        if not results:
            return False
        await self._client.pages.update(
            results[0]["id"], properties={"Статус": {"select": {"name": status}}}
        )
        return True

    async def last_new_orders(self, limit: int = 10) -> list[dict]:
        res = await self._client.data_sources.query(
            self._orders_ds,
            filter={"property": "Статус", "select": {"equals": STATUS_NEW}},
            sorts=[{"property": "Дата", "direction": "descending"}],
            page_size=limit,
        )
        orders = []
        for page in res.get("results", []):
            props = page["properties"]
            fio = " ".join(
                part
                for part in (
                    _plain(props.get("Прізвище", {})),
                    _plain(props.get("Ім'я", {})),
                )
                if part
            )
            orders.append(
                {
                    "number": props.get("№ замовлення", {}).get("number"),
                    "fio": fio or _plain(props.get("Номер ТТН", {})),
                    "items": _plain(props.get("Склад замовлення", {})),
                    "phone": _plain(props.get("Телефон", {})) or "—",
                    "date": (props.get("Дата", {}).get("date") or {}).get("start") or "",
                }
            )
        return orders

    # ---------- шаблони відповідей ----------

    async def get_templates(self) -> list[tuple[list[str], str]]:
        if time.monotonic() - self._tpl_cached_at < TEMPLATES_TTL:
            return self._tpl_cache
        res = await self._client.data_sources.query(
            self._templates_ds,
            filter={"property": "Активний", "checkbox": {"equals": True}},
        )
        templates = []
        for page in res.get("results", []):
            props = page["properties"]
            keywords = [
                k.strip().lower()
                for k in _plain(props.get("Ключові слова", {})).split(",")
                if k.strip()
            ]
            answer = _plain(props.get("Відповідь", {}))
            if keywords and answer:
                templates.append((keywords, answer))
        self._tpl_cache = templates
        self._tpl_cached_at = time.monotonic()
        return templates

    async def find_answer(self, text: str) -> str | None:
        try:
            templates = await self.get_templates()
        except Exception:
            log.warning("Notion недоступний, шаблони не завантажено", exc_info=True)
            return None
        lowered = text.lower()
        for keywords, answer in templates:
            if any(kw in lowered for kw in keywords):
                return answer
        return None

    # ---------- офлайн-черга ----------

    def queue_order(self, order: dict) -> None:
        pending = self._load_pending()
        pending.append(order)
        self._save_pending(pending)

    def pending_count(self) -> int:
        return len(self._load_pending())

    async def flush_pending(self) -> list[int]:
        """Довантажує чергу в Notion. Повертає номери збережених замовлень;
        зупиняється на першій помилці (Notion ще недоступний)."""
        pending = self._load_pending()
        flushed = []
        while pending:
            try:
                await self.create_order(pending[0])
            except Exception:
                break
            flushed.append(pending.pop(0)["number"])
            self._save_pending(pending)
        return flushed

    def _load_pending(self) -> list[dict]:
        if not self._pending_path.exists():
            return []
        try:
            return json.loads(self._pending_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            log.exception("Не вдалося прочитати %s", self._pending_path)
            return []

    def _save_pending(self, pending: list[dict]) -> None:
        self._pending_path.write_text(
            json.dumps(pending, ensure_ascii=False, indent=2), encoding="utf-8"
        )
