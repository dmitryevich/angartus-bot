import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_group_id: int
    notion_token: str
    # ID data source (вкладки) — не бази цілком: Operations Hub має кілька вкладок
    notion_orders_db_id: str
    notion_templates_db_id: str
    db_path: str
    pending_path: str
    # ID службової теми бота в адмін-групі; None — працювати без обмежень
    bot_topic_id: int | None


def load_config() -> Config:
    return Config(
        bot_token=os.environ["BOT_TOKEN"],
        admin_group_id=int(os.environ["ADMIN_GROUP_ID"]),
        notion_token=os.environ["NOTION_TOKEN"],
        notion_orders_db_id=os.environ["NOTION_ORDERS_DB_ID"],
        notion_templates_db_id=os.environ["NOTION_TEMPLATES_DB_ID"],
        db_path=os.getenv("DB_PATH", "bot.db"),
        pending_path=os.getenv("PENDING_PATH", "pending_orders.json"),
        bot_topic_id=int(os.environ["BOT_TOPIC_ID"]) if os.getenv("BOT_TOPIC_ID") else None,
    )
