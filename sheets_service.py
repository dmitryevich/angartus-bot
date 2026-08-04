"""Робота з Google Таблицею «Shipment_Registry» (вкладка «Предзамовлення») як сховищем
замовлень, і вкладкою «Шаблони» для авто-відповідей. Офлайн-черга — в JSON.

Колонки вкладки «Предзамовлення» (перевірено через Sheets API, не змінювати без потреби):
A Запаковано | B Номер ТТН | C Склад замовлення | D Номер (тут — телефон клієнта)
E Прізвище | F Ім'я | G По-батькові | H Тип отримання | I Номер/Адреса
J Місто | K Район | L Область | M Фіскальний чек (посилання на квитанцію)
N Тип — бот НЕ заповнює, проставляється вручну
"""
import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build

from config import Config

log = logging.getLogger(__name__)

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

# Опції select «Тип отримання» — назви мають збігатися точно з колонкою H
DELIVERY_TYPES = ["Відділення", "Поштомат", "Адресна доставка", "Укрпошта"]

DATA_START_ROW = 4  # рядки 1-3 — заголовки/зведення
FIRST_ROW_SCAN_LIMIT = 2000
CLIENTS_SCAN_LIMIT = 10000  # скільки рядків вкладки «Клієнти» переглядаємо

TEMPLATES_TTL = 300


class SheetsService:
    def __init__(self, cfg: Config):
        info = json.loads(cfg.google_service_account_json)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        self._svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
        self._spreadsheet_id = cfg.google_sheets_id
        self._orders_sheet = cfg.orders_sheet_name
        self._templates_sheet = cfg.templates_sheet_name
        self._clients_sheet = cfg.clients_sheet_name
        self._pending_path = Path(cfg.pending_path)
        self._tpl_cache: list[tuple[list[str], str]] = []
        self._tpl_cached_at = 0.0
        # Кого вже записали в цьому процесі — щоб не смикати API на кожне повідомлення
        self._known_clients: set[int] = set()

    def _values(self):
        return self._svc.spreadsheets().values()

    @staticmethod
    async def _run(request) -> dict:
        """googleapiclient — синхронний; виносимо .execute() у окремий потік,
        щоб не блокувати asyncio event loop бота."""
        return await asyncio.to_thread(request.execute)

    # ---------- клієнти (вкладка «Клієнти») ----------
    # Тримаємо базу розсилки в Таблиці, а не в SQLite: вона переживає
    # перезапуск, переїзд на інший хостинг і видно її прямо в документі.

    async def _client_rows(self) -> list[list[str]]:
        rng = f"{self._clients_sheet}!A2:D{CLIENTS_SCAN_LIMIT}"
        res = await self._run(self._values().get(spreadsheetId=self._spreadsheet_id, range=rng))
        return res.get("values", [])

    async def upsert_client(self, user_id: int, full_name: str | None, username: str | None) -> None:
        """Додає клієнта у вкладку «Клієнти» або оновлює імʼя/юзернейм, якщо змінились."""
        if user_id in self._known_clients:
            return
        rows = await self._client_rows()
        for offset, row in enumerate(rows):
            row = row + [""] * (4 - len(row))
            if row[0].strip() == str(user_id):
                self._known_clients.add(user_id)
                if row[1] != (full_name or "") or row[2] != (username or ""):
                    await self._run(self._values().update(
                        spreadsheetId=self._spreadsheet_id,
                        range=f"{self._clients_sheet}!B{2 + offset}:C{2 + offset}",
                        valueInputOption="RAW",
                        body={"values": [[full_name or "", username or ""]]},
                    ))
                return
        await self._run(self._values().append(
            spreadsheetId=self._spreadsheet_id,
            range=f"{self._clients_sheet}!A:D",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [[
                str(user_id),
                full_name or "",
                username or "",
                datetime.now().strftime("%Y-%m-%d %H:%M"),
            ]]},
        ))
        self._known_clients.add(user_id)

    async def all_clients(self) -> list[tuple[int, str, str]]:
        """(user_id, імʼя, username) — база для розсилки."""
        out = []
        for row in await self._client_rows():
            row = row + [""] * (4 - len(row))
            uid = row[0].strip()
            if uid.isdigit():
                out.append((int(uid), row[1], row[2]))
        return out

    # ---------- замовлення (Предзамовлення) ----------

    async def _first_empty_row(self) -> int:
        rng = f"{self._orders_sheet}!E{DATA_START_ROW}:E{FIRST_ROW_SCAN_LIMIT}"
        res = await self._run(self._values().get(spreadsheetId=self._spreadsheet_id, range=rng))
        rows = res.get("values", [])
        for offset, row in enumerate(rows):
            if not row or not row[0].strip():
                return DATA_START_ROW + offset
        return DATA_START_ROW + len(rows)

    async def create_order(self, order: dict) -> int:
        """Записує замовлення в перший вільний рядок вкладки. Повертає номер рядка."""
        row = await self._first_empty_row()
        # RAW, а не USER_ENTERED: інакше Таблиця з'їдає «+» на початку телефону,
        # трактуючи його як формулу
        await self._run(self._values().update(
            spreadsheetId=self._spreadsheet_id,
            range=f"{self._orders_sheet}!C{row}:L{row}",
            valueInputOption="RAW",
            body={
                "values": [[
                    order["items"],
                    order["phone"],
                    order["surname"],
                    order["name"],
                    order.get("patronymic") or "",
                    order["delivery_type"],
                    order["address"],
                    order["city"],
                    order.get("district", ""),
                    order["region"],
                ]]
            },
        ))
        # Колонку N «Тип» бот не заповнює — її проставляють вручну
        receipt = order.get("receipt")
        if receipt:
            await self._run(self._values().update(
                spreadsheetId=self._spreadsheet_id,
                range=f"{self._orders_sheet}!M{row}",
                valueInputOption="USER_ENTERED",
                body={"values": [[receipt]]},
            ))
        return row

    async def set_packed(self, row: int, packed: bool) -> None:
        await self._run(self._values().update(
            spreadsheetId=self._spreadsheet_id,
            range=f"{self._orders_sheet}!A{row}",
            valueInputOption="USER_ENTERED",
            body={"values": [["TRUE" if packed else "FALSE"]]},
        ))

    async def list_orders(self, limit: int = 10) -> list[dict]:
        """Останні (найновіші — знизу таблиці) рядки з реальними замовленнями."""
        rng = f"{self._orders_sheet}!A{DATA_START_ROW}:L{FIRST_ROW_SCAN_LIMIT}"
        res = await self._run(self._values().get(spreadsheetId=self._spreadsheet_id, range=rng))
        rows = res.get("values", [])
        orders = []
        for offset, row in enumerate(rows):
            row = row + [""] * (12 - len(row))
            surname, name = row[4], row[5]
            if not surname.strip():
                continue
            orders.append(
                {
                    "row": DATA_START_ROW + offset,
                    "packed": row[0].strip().upper() == "TRUE",
                    "fio": f"{surname} {name}".strip(),
                    "items": row[2],
                    "phone": row[3] or "—",
                }
            )
        orders.reverse()
        return orders[:limit]

    # ---------- шаблони відповідей ----------

    async def get_templates(self) -> list[tuple[list[str], str]]:
        if time.monotonic() - self._tpl_cached_at < TEMPLATES_TTL:
            return self._tpl_cache
        rng = f"{self._templates_sheet}!A2:C1000"
        res = await self._run(self._values().get(spreadsheetId=self._spreadsheet_id, range=rng))
        templates = []
        for row in res.get("values", []):
            row = row + [""] * (3 - len(row))
            keywords_raw, answer, active = row
            active_flag = active.strip().upper() in ("TRUE", "ТАК", "1")
            keywords = [k.strip().lower() for k in keywords_raw.split(",") if k.strip()]
            if active_flag and keywords and answer.strip():
                templates.append((keywords, answer))
        self._tpl_cache = templates
        self._tpl_cached_at = time.monotonic()
        return templates

    async def find_answer(self, text: str) -> str | None:
        try:
            templates = await self.get_templates()
        except Exception:
            log.warning("Google Sheets недоступні, шаблони не завантажено", exc_info=True)
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

    async def flush_pending(self) -> list[dict]:
        """Довантажує чергу в Таблицю. Повертає [{number, row}, ...] для успішних;
        зупиняється на першій помилці (Sheets ще недоступні)."""
        pending = self._load_pending()
        flushed = []
        while pending:
            try:
                row = await self.create_order(pending[0])
            except Exception:
                break
            flushed.append({"number": pending.pop(0)["number"], "row": row})
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
