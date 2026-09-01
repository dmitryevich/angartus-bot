"""Замовлення в Notion — база «Shipment Registry» («🧠 Ангарта Operations Hub»).

Схема бази вибудувана під відвантаження Новою Поштою, тому:
- основна властивість (title) — «Телефон», а не номер ТТН;
- «Номер ТТН» бот не заповнює: його проставляють при створенні накладної;
- статус замовлення — чекбокс «Запаковано», окремого поля «Статус» немає;
- № замовлення і user_id у Notion не зберігаються, зв'язка живе в SQLite.

Квитанція клієнта вантажиться у властивість «Фото чека» як справжній файл
Notion (File Upload API), а не посиланням — інакше в базі не видно, що саме
надіслала людина.
"""
import asyncio
import json
import logging
from pathlib import Path

import httpx

from config import Config

log = logging.getLogger(__name__)

API = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"
TIMEOUT = httpx.Timeout(30.0)

STATUS_PACKED = "🟢 Запаковано"
STATUS_NOT_PACKED = "🟡 Не запаковано"
STATUSES = [STATUS_NOT_PACKED, STATUS_PACKED]
STATUS_ALIASES = {
    "запаковано": STATUS_PACKED,
    "готово": STATUS_PACKED,
    "виконано": STATUS_PACKED,
    "не запаковано": STATUS_NOT_PACKED,
    "нове": STATUS_NOT_PACKED,
    "в роботі": STATUS_NOT_PACKED,
    "скасовано": STATUS_NOT_PACKED,
}
for _s in STATUSES:
    STATUS_ALIASES[_s.lower()] = _s

# Те, що бот показує клієнту
DELIVERY_TYPES = ["Відділення", "Поштомат", "Адресна доставка", "Укрпошта"]
# У Notion опція називається інакше, і create_shipments.py звіряється саме з нею —
# перейменування там зламало б пропуск укрпоштових замовлень
DELIVERY_TO_NOTION = {"Укрпошта": "Укр.пошта"}
DELIVERY_FROM_NOTION = {v: k for k, v in DELIVERY_TO_NOTION.items()}


def _rt(text: str) -> dict:
    return {"rich_text": [{"text": {"content": text[:2000]}}] if text else []}


def _title(text: str) -> dict:
    return {"title": [{"text": {"content": text[:2000]}}] if text else []}


def _select(name: str | None) -> dict:
    return {"select": {"name": name} if name else None}


def _plain(prop: dict | None) -> str:
    if not prop:
        return ""
    parts = prop.get("title") or prop.get("rich_text") or []
    return "".join(p.get("plain_text", "") for p in parts).strip()


class NotionService:
    def __init__(self, cfg: Config):
        self._ds = cfg.notion_orders_ds_id
        self._pending_path = Path(cfg.pending_path)
        self._headers = {
            "Authorization": f"Bearer {cfg.notion_token}",
            "Notion-Version": NOTION_VERSION,
        }

    async def _api(self, method: str, path: str, payload: dict | None = None) -> dict:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.request(
                method,
                f"{API}{path}",
                headers={**self._headers, "Content-Type": "application/json"},
                json=payload,
            )
        if r.status_code >= 400:
            raise RuntimeError(f"Notion {method} {path} -> {r.status_code}: {r.text[:400]}")
        return r.json()

    # ---------- квитанція ----------

    async def upload_receipt(self, data: bytes, filename: str, content_type: str) -> str | None:
        """Заливає файл у Notion і повертає file_upload_id.
        None — якщо не вийшло: замовлення важливіше за картинку, тому не падаємо."""
        try:
            created = await self._api(
                "POST", "/file_uploads", {"filename": filename, "content_type": content_type}
            )
            upload_url = created["upload_url"]
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                r = await client.post(
                    upload_url,
                    headers=self._headers,
                    files={"file": (filename, data, content_type)},
                )
            if r.status_code >= 400:
                raise RuntimeError(f"upload -> {r.status_code}: {r.text[:300]}")
            return created["id"]
        except Exception:
            log.warning("Не вдалося залити квитанцію в Notion", exc_info=True)
            return None

    # ---------- замовлення ----------

    @staticmethod
    def order_properties(o: dict) -> dict:
        delivery = o.get("delivery_type") or ""
        props = {
            "Телефон": _title(o.get("phone") or ""),
            "Прізвище": _rt(o.get("surname") or ""),
            "Ім'я": _rt(o.get("name") or ""),
            "По-батькові": _rt(o.get("patronymic") or ""),
            "Тип отримання": _select(DELIVERY_TO_NOTION.get(delivery, delivery) or None),
            "Номер / Адреса": _rt(o.get("address") or ""),
            "Місто": _rt(o.get("city") or ""),
            "Район": _rt(o.get("district") or ""),
            "Область": _rt(o.get("region") or ""),
            "Склад замовлення": _rt(o.get("items") or ""),
            "Тип": _select("Default"),
            "Запаковано": {"checkbox": False},
        }
        # Дати не пишемо: властивості «Дата» в базі немає, а час створення
        # Notion веде сам (created_time) — саме за ним і сортуємо в /orders
        if o.get("receipt_upload_id"):
            props["Фото чека"] = {
                "files": [{
                    "type": "file_upload",
                    "name": o.get("receipt_name") or "квитанція",
                    "file_upload": {"id": o["receipt_upload_id"]},
                }]
            }
        return props

    async def create_order(self, order: dict) -> str:
        """Створює сторінку замовлення. Повертає page_id."""
        page = await self._api(
            "POST",
            "/pages",
            {
                "parent": {"type": "data_source_id", "data_source_id": self._ds},
                "properties": self.order_properties(order),
            },
        )
        return page["id"]

    async def set_packed(self, page_id: str, packed: bool) -> None:
        await self._api("PATCH", f"/pages/{page_id}", {"properties": {"Запаковано": {"checkbox": packed}}})

    async def get_order(self, page_id: str) -> dict | None:
        try:
            page = await self._api("GET", f"/pages/{page_id}")
        except RuntimeError as e:
            if "404" in str(e):
                return None
            raise
        if page.get("archived") or page.get("in_trash"):
            return None
        return self._parse(page)

    @staticmethod
    def _parse(page: dict) -> dict:
        p = page.get("properties", {})
        delivery = (p.get("Тип отримання", {}).get("select") or {}).get("name", "")
        return {
            "packed": bool(p.get("Запаковано", {}).get("checkbox")),
            "ttn": _plain(p.get("Номер ТТН")),
            "items": _plain(p.get("Склад замовлення")),
            "phone": _plain(p.get("Телефон")),
            "surname": _plain(p.get("Прізвище")),
            "name": _plain(p.get("Ім'я")),
            "patronymic": _plain(p.get("По-батькові")),
            "delivery_type": DELIVERY_FROM_NOTION.get(delivery, delivery),
            "address": _plain(p.get("Номер / Адреса")),
            "city": _plain(p.get("Місто")),
            "district": _plain(p.get("Район")),
            "region": _plain(p.get("Область")),
        }

    async def list_orders(self, limit: int = 10) -> list[dict]:
        """Незапаковані замовлення, найновіші спершу — для /orders."""
        res = await self._api(
            "POST",
            f"/data_sources/{self._ds}/query",
            {
                "filter": {"property": "Запаковано", "checkbox": {"equals": False}},
                "sorts": [{"timestamp": "created_time", "direction": "descending"}],
                "page_size": min(limit, 100),
            },
        )
        orders = []
        for page in res.get("results", []):
            o = self._parse(page)
            if not o["surname"] and not o["items"]:
                continue
            orders.append({
                "page_id": page["id"],
                "packed": o["packed"],
                "fio": f"{o['surname']} {o['name']}".strip(),
                "items": o["items"],
                "phone": o["phone"] or "—",
            })
        return orders

    # ---------- офлайн-черга ----------

    def queue_order(self, order: dict) -> None:
        pending = self._load_pending()
        pending.append(order)
        self._save_pending(pending)

    def pending_count(self) -> int:
        return len(self._load_pending())

    async def flush_pending(self) -> list[dict]:
        """Довантажує чергу. Зупиняється на першій помилці (Notion ще лежить)."""
        pending = self._load_pending()
        flushed = []
        while pending:
            try:
                page_id = await self.create_order(pending[0])
            except Exception:
                break
            done = pending.pop(0)
            flushed.append({
                "number": done["number"],
                "page_id": page_id,
                "user_id": done.get("user_id"),
            })
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
