"""SQLite-сховище: зв'язка повідомлень у групі з клієнтами, пауза авто-відповідей,
лічильник замовлень."""
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


def next_order_number() -> int:
    with _lock:
        _conn.execute(
            "INSERT INTO meta (key, value) VALUES ('order_counter', 0) ON CONFLICT(key) DO NOTHING"
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
