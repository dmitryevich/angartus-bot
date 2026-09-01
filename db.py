"""SQLite-сховище: зв'язка повідомлень у групі з клієнтами, пауза авто-відповідей,
лічильник замовлень, залишки товарів."""
import sqlite3
import threading

_conn: sqlite3.Connection | None = None
_lock = threading.Lock()


def init_db(path: str) -> None:
    global _conn
    _conn = sqlite3.connect(path, check_same_thread=False)
    _conn.execute(
        """
        CREATE TABLE IF NOT EXISTS clients (
            user_id   INTEGER PRIMARY KEY,
            topic_id  INTEGER,
            full_name TEXT,
            username  TEXT,
            paused    INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    _conn.execute("CREATE INDEX IF NOT EXISTS idx_clients_topic ON clients(topic_id)")
    _conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL)")
    # Зв'язка «повідомлення в адмін-групі -> клієнт» для reply-відповідей
    _conn.execute(
        """
        CREATE TABLE IF NOT EXISTS msg_map (
            group_msg_id INTEGER PRIMARY KEY,
            user_id      INTEGER NOT NULL
        )
        """
    )
    # Зв'язка «№ замовлення (показуємо клієнту) -> сторінка Notion»,
    # бо в самій базі Notion номера замовлення немає.
    # sheet_row лишається від епохи Google Таблиці — старі записи не чіпаємо.
    # user_id тримаємо тут же — інакше за номером замовлення нема кому писати
    # про зміну статусу і нема з чого зібрати «Мої замовлення».
    _conn.execute(
        """
        CREATE TABLE IF NOT EXISTS order_rows (
            order_number INTEGER PRIMARY KEY,
            sheet_row    INTEGER,
            user_id      INTEGER,
            page_id      TEXT
        )
        """
    )
    # Міграція для баз, створених до появи user_id (том Railway переживає деплой)
    columns = {r[1] for r in _conn.execute("PRAGMA table_info(order_rows)")}
    if "user_id" not in columns:
        _conn.execute("ALTER TABLE order_rows ADD COLUMN user_id INTEGER")
    if "page_id" not in columns:
        _conn.execute("ALTER TABLE order_rows ADD COLUMN page_id TEXT")
    _conn.execute("CREATE INDEX IF NOT EXISTS idx_order_rows_user ON order_rows(user_id)")
    # Залишки товарів. Рядка немає — облік по цій позиції не ведеться, тобто
    # обмежень на замовлення немає. 0 — товар показується як «немає в наявності».
    _conn.execute(
        """
        CREATE TABLE IF NOT EXISTS stock (
            product_id TEXT PRIMARY KEY,
            qty        INTEGER NOT NULL
        )
        """
    )
    _conn.commit()


def map_group_message(group_msg_id: int, user_id: int) -> None:
    with _lock:
        _conn.execute(
            "INSERT OR REPLACE INTO msg_map (group_msg_id, user_id) VALUES (?, ?)",
            (group_msg_id, user_id),
        )
        _conn.commit()


def user_by_group_message(group_msg_id: int) -> int | None:
    row = _conn.execute(
        "SELECT user_id FROM msg_map WHERE group_msg_id = ?", (group_msg_id,)
    ).fetchone()
    return row[0] if row else None


FIRST_ORDER_NUMBER = 100


def next_order_number() -> int:
    with _lock:
        # Стартуємо з FIRST_ORDER_NUMBER: лічильник тримаємо на одиницю меншим
        _conn.execute(
            "INSERT INTO meta (key, value) VALUES ('order_counter', ?) ON CONFLICT(key) DO NOTHING",
            (FIRST_ORDER_NUMBER - 1,),
        )
        _conn.execute("UPDATE meta SET value = value + 1 WHERE key = 'order_counter'")
        row = _conn.execute("SELECT value FROM meta WHERE key = 'order_counter'").fetchone()
        _conn.commit()
        return row[0]


def upsert_client(user_id: int, full_name: str, username: str | None) -> None:
    with _lock:
        _conn.execute(
            """
            INSERT INTO clients (user_id, full_name, username) VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET full_name = excluded.full_name,
                                              username  = excluded.username
            """,
            (user_id, full_name, username),
        )
        _conn.commit()


def all_clients() -> list[tuple[int, str | None, str | None]]:
    """Усі, хто бодай раз писав боту: (user_id, full_name, username) — для розсилки."""
    rows = _conn.execute(
        "SELECT user_id, full_name, username FROM clients ORDER BY user_id"
    ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def get_topic(user_id: int) -> int | None:
    row = _conn.execute("SELECT topic_id FROM clients WHERE user_id = ?", (user_id,)).fetchone()
    return row[0] if row and row[0] else None


def set_topic(user_id: int, topic_id: int | None) -> None:
    with _lock:
        _conn.execute("UPDATE clients SET topic_id = ? WHERE user_id = ?", (topic_id, user_id))
        _conn.commit()


def get_user_by_topic(topic_id: int) -> int | None:
    row = _conn.execute("SELECT user_id FROM clients WHERE topic_id = ?", (topic_id,)).fetchone()
    return row[0] if row else None


def is_paused(user_id: int) -> bool:
    row = _conn.execute("SELECT paused FROM clients WHERE user_id = ?", (user_id,)).fetchone()
    return bool(row and row[0])


def set_paused(user_id: int, paused: bool) -> None:
    with _lock:
        _conn.execute("UPDATE clients SET paused = ? WHERE user_id = ?", (int(paused), user_id))
        _conn.commit()


def map_order_page(order_number: int, page_id: str, user_id: int | None = None) -> None:
    """Зв'язка «№ замовлення -> сторінка Notion + клієнт». Ані номера, ані
    user_id у Notion немає, тому це єдине місце, де вони тримаються разом."""
    with _lock:
        _conn.execute(
            "INSERT OR REPLACE INTO order_rows (order_number, page_id, user_id) VALUES (?, ?, ?)",
            (order_number, page_id, user_id),
        )
        _conn.commit()


def user_for_order(order_number: int) -> int | None:
    """Кому належить замовлення. None для старих записів без user_id."""
    row = _conn.execute(
        "SELECT user_id FROM order_rows WHERE order_number = ?", (order_number,)
    ).fetchone()
    return row[0] if row and row[0] else None


def page_for_order(order_number: int) -> str | None:
    row = _conn.execute(
        "SELECT page_id FROM order_rows WHERE order_number = ?", (order_number,)
    ).fetchone()
    return row[0] if row and row[0] else None


def order_for_page(page_id: str) -> int | None:
    row = _conn.execute(
        "SELECT order_number FROM order_rows WHERE page_id = ?", (page_id,)
    ).fetchone()
    return row[0] if row else None


def orders_for_user(user_id: int, limit: int = 10) -> list[tuple[int, str]]:
    """[(номер замовлення, сторінка Notion), ...] — найновіші спершу.
    Замовлення епохи Google Таблиці (без page_id) сюди не потрапляють:
    показати їх картку все одно нема звідки."""
    return [
        (r[0], r[1])
        for r in _conn.execute(
            "SELECT order_number, page_id FROM order_rows "
            "WHERE user_id = ? AND page_id IS NOT NULL "
            "ORDER BY order_number DESC LIMIT ?",
            (user_id, limit),
        )
    ]


def get_stock(product_id: str) -> int | None:
    """Скільки лишилось. None — облік по цьому товару не ведеться."""
    row = _conn.execute("SELECT qty FROM stock WHERE product_id = ?", (product_id,)).fetchone()
    return row[0] if row else None


def set_stock(product_id: str, qty: int) -> None:
    with _lock:
        _conn.execute(
            "INSERT OR REPLACE INTO stock (product_id, qty) VALUES (?, ?)",
            (product_id, max(qty, 0)),
        )
        _conn.commit()


def take_stock(product_id: str, qty: int) -> int | None:
    """Списати продане. Повертає залишок, або None, якщо облік не ведеться.
    Нижче нуля не опускаємось: два клієнти можуть встигнути купити останнє."""
    with _lock:
        row = _conn.execute(
            "SELECT qty FROM stock WHERE product_id = ?", (product_id,)
        ).fetchone()
        if row is None:
            return None
        left = max(row[0] - qty, 0)
        _conn.execute("UPDATE stock SET qty = ? WHERE product_id = ?", (left, product_id))
        _conn.commit()
        return left
