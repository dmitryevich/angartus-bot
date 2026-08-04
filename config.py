import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_group_id: int
    google_sheets_id: str
    google_service_account_json: str
    orders_sheet_name: str
    templates_sheet_name: str
    clients_sheet_name: str
    db_path: str
    pending_path: str
    # ID службової теми бота в адмін-групі; None — працювати без обмежень
    bot_topic_id: int | None
    # Тестовий режим розсилки: /mailings шле лише тому, хто набрав команду
    mailing_test_mode: bool


def load_config() -> Config:
    return Config(
        bot_token=os.environ["BOT_TOKEN"],
        admin_group_id=int(os.environ["ADMIN_GROUP_ID"]),
        google_sheets_id=os.environ["GOOGLE_SHEETS_ID"],
        google_service_account_json=os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"],
        orders_sheet_name=os.getenv("ORDERS_SHEET_NAME", "Предзамовлення"),
        templates_sheet_name=os.getenv("TEMPLATES_SHEET_NAME", "Шаблони"),
        clients_sheet_name=os.getenv("CLIENTS_SHEET_NAME", "Клієнти"),
        db_path=os.getenv("DB_PATH", "bot.db"),
        pending_path=os.getenv("PENDING_PATH", "pending_orders.json"),
        bot_topic_id=int(os.environ["BOT_TOPIC_ID"]) if os.getenv("BOT_TOPIC_ID") else None,
        mailing_test_mode=os.getenv("MAILING_TEST_MODE", "").strip().lower()
        in ("1", "true", "yes", "on"),
    )
