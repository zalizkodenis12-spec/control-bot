from __future__ import annotations

import html
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

import config
from config import DB_PATH

_db_lock = threading.RLock()


@contextmanager
def _get_connection(db_path: str = DB_PATH) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_db(db_path: str = DB_PATH) -> None:
    """Initialize SQLite tables for contest state, participants, and single-admin username binding."""
    with _db_lock, _get_connection(db_path) as conn:
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS contest_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    is_active INTEGER NOT NULL DEFAULT 1,
                    contest_id INTEGER NOT NULL DEFAULT 1,
                    started_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS participants (
                    number INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL UNIQUE,
                    first_name TEXT NOT NULL DEFAULT '',
                    last_name TEXT,
                    username TEXT,
                    joined_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS admin_binding (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    user_id INTEGER,
                    bound_at TEXT
                )
                """
            )
            conn.execute("DROP TABLE IF EXISTS admins")
            now_iso = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """
                INSERT OR IGNORE INTO contest_state (id, is_active, contest_id, started_at)
                VALUES (1, 1, 1, ?)
                """,
                (now_iso,),
            )


def _clean_username(username: str | None) -> str | None:
    if username is None:
        return None
    cleaned = username.strip().lstrip("@").strip()
    return cleaned if cleaned else None


def is_admin(user_id: int, username: str | None = None, db_path: str = DB_PATH) -> bool:
    """
    Return True if the user is the single configured admin:
    1. Matches config.ADMIN_ID (if set).
    2. Matches the already-bound user_id in admin_binding.
    3. If admin_binding is not yet bound and config.ADMIN_USERNAME is set,
       and the user's username matches config.ADMIN_USERNAME (case-insensitive),
       permanently binds this user_id in admin_binding and returns True.
    """
    try:
        uid = int(user_id)
    except (ValueError, TypeError):
        return False

    if config.ADMIN_ID is not None and uid == int(config.ADMIN_ID):
        return True

    init_db(db_path)
    with _db_lock, _get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT user_id FROM admin_binding WHERE id = 1"
        ).fetchone()
        if row is not None and row["user_id"] is not None:
            return int(row["user_id"]) == uid

        target_uname = _clean_username(getattr(config, "ADMIN_USERNAME", None))
        given_uname = _clean_username(username)
        if target_uname and given_uname and given_uname.lower() == target_uname.lower():
            now_iso = datetime.now(timezone.utc).isoformat()
            with conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO admin_binding (id, user_id, bound_at)
                    VALUES (1, ?, ?)
                    """,
                    (uid, now_iso),
                )
            return True

    return False


def get_all_admins(db_path: str = DB_PATH) -> list[int]:
    """Return the bound admin's user_id (from admin_binding or config.ADMIN_ID) if known, or []."""
    admins: set[int] = set()
    if config.ADMIN_ID is not None:
        admins.add(int(config.ADMIN_ID))

    init_db(db_path)
    with _db_lock, _get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT user_id FROM admin_binding WHERE id = 1"
        ).fetchone()
        if row is not None and row["user_id"] is not None:
            admins.add(int(row["user_id"]))

    return sorted(admins)


def is_contest_active(db_path: str = DB_PATH) -> bool:
    """Return True if the contest is currently active."""
    init_db(db_path)
    with _db_lock, _get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT is_active FROM contest_state WHERE id = 1"
        ).fetchone()
        if row is None:
            return True
        return bool(row["is_active"])


def set_contest_active(active: bool, db_path: str = DB_PATH) -> None:
    """Set contest active state (True = active, False = finished)."""
    init_db(db_path)
    with _db_lock, _get_connection(db_path) as conn:
        with conn:
            conn.execute(
                "UPDATE contest_state SET is_active = ? WHERE id = 1",
                (1 if active else 0,),
            )


def register_participant(
    user_id: int,
    first_name: str,
    last_name: str | None = None,
    username: str | None = None,
    db_path: str = DB_PATH,
) -> tuple[bool, int]:
    """
    Register a participant in the active contest.
    - Returns (is_new, number).
    - If user_id already exists, updates first_name, last_name, username and returns (False, existing_number).
    - If new, assigns next_number = max(number) + 1 (starting from 1) atomically and returns (True, number).
    """
    init_db(db_path)
    if username:
        is_admin(user_id, username=username, db_path=db_path)
    clean_first = (first_name or "").strip()
    clean_last = (last_name or "").strip() or None
    clean_uname = _clean_username(username)
    now_iso = datetime.now(timezone.utc).isoformat()

    with _db_lock, _get_connection(db_path) as conn:
        conn.isolation_level = None  # Manual transaction control for BEGIN IMMEDIATE
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute(
                "SELECT number FROM participants WHERE user_id = ?",
                (int(user_id),),
            ).fetchone()

            if existing is not None:
                number = int(existing["number"])
                conn.execute(
                    """
                    UPDATE participants
                    SET first_name = ?, last_name = ?, username = ?
                    WHERE user_id = ?
                    """,
                    (clean_first, clean_last, clean_uname, int(user_id)),
                )
                conn.execute("COMMIT")
                return False, number

            max_row = conn.execute(
                "SELECT COALESCE(MAX(number), 0) AS max_num FROM participants"
            ).fetchone()
            next_number = int(max_row["max_num"]) + 1

            conn.execute(
                """
                INSERT INTO participants (number, user_id, first_name, last_name, username, joined_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (next_number, int(user_id), clean_first, clean_last, clean_uname, now_iso),
            )
            conn.execute("COMMIT")
            return True, next_number
        except Exception:
            conn.execute("ROLLBACK")
            raise


def get_participant(user_id: int, db_path: str = DB_PATH) -> dict[str, Any] | None:
    """Return a single participant dict by Telegram user_id, or None if not registered."""
    init_db(db_path)
    with _db_lock, _get_connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT number, user_id, first_name, last_name, username, joined_at
            FROM participants
            WHERE user_id = ?
            """,
            (int(user_id),),
        ).fetchone()
        return dict(row) if row is not None else None


def get_all_participants(db_path: str = DB_PATH) -> list[dict[str, Any]]:
    """Return all participants ordered by number ASC (1..N)."""
    init_db(db_path)
    with _db_lock, _get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT number, user_id, first_name, last_name, username, joined_at
            FROM participants
            ORDER BY number ASC
            """
        ).fetchall()
        return [dict(r) for r in rows]


def get_participants_count(db_path: str = DB_PATH) -> int:
    """Return total number of registered participants."""
    init_db(db_path)
    with _db_lock, _get_connection(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) AS cnt FROM participants").fetchone()
        return int(row["cnt"]) if row is not None else 0


def reset_contest(db_path: str = DB_PATH) -> None:
    """Clear all participants and activate a new contest starting from number 1."""
    init_db(db_path)
    now_iso = datetime.now(timezone.utc).isoformat()
    with _db_lock, _get_connection(db_path) as conn:
        with conn:
            conn.execute("DELETE FROM participants")
            conn.execute(
                """
                UPDATE contest_state
                SET is_active = 1,
                    contest_id = contest_id + 1,
                    started_at = ?
                WHERE id = 1
                """,
                (now_iso,),
            )


def format_participant_line(p: dict[str, Any], include_number: bool = True) -> str:
    """
    Format a participant dict as an HTML line with HTML-escaped name and spoiler-wrapped @username.
    - If username exists: <tg-spoiler>@username_value</tg-spoiler>
    - If username is None/empty: <tg-spoiler>@username</tg-spoiler>
    - With include_number=True: "{number}. {full_name} <tg-spoiler>@{uname}</tg-spoiler>"
    - With include_number=False: "{full_name} <tg-spoiler>@{uname}</tg-spoiler>"
    """
    first_raw = (p.get("first_name") or "").strip()
    last_raw = (p.get("last_name") or "").strip()
    full_name_raw = f"{first_raw} {last_raw}".strip() or "Участник"
    full_name = html.escape(full_name_raw)

    raw_uname = _clean_username(p.get("username"))
    uname_display = html.escape(raw_uname) if raw_uname else "username"
    spoiler_tag = f"<tg-spoiler>@{uname_display}</tg-spoiler>"

    if include_number:
        number = p.get("number", 1)
        return f"{number}. {full_name} {spoiler_tag}"
    return f"{full_name} {spoiler_tag}"


class Database:
    """Object-oriented wrapper around database operations bound to a specific db_path."""

    def __init__(self, db_path: str = DB_PATH) -> None:
        self.db_path = db_path
        init_db(self.db_path)

    def init_db(self) -> None:
        init_db(self.db_path)

    def is_admin(self, user_id: int, username: str | None = None) -> bool:
        return is_admin(user_id, username=username, db_path=self.db_path)

    def get_all_admins(self) -> list[int]:
        return get_all_admins(self.db_path)

    def is_contest_active(self) -> bool:
        return is_contest_active(self.db_path)

    def set_contest_active(self, active: bool) -> None:
        set_contest_active(active, self.db_path)

    def register_participant(
        self,
        user_id: int,
        first_name: str,
        last_name: str | None = None,
        username: str | None = None,
    ) -> tuple[bool, int]:
        return register_participant(
            user_id=user_id,
            first_name=first_name,
            last_name=last_name,
            username=username,
            db_path=self.db_path,
        )

    def get_participant(self, user_id: int) -> dict[str, Any] | None:
        return get_participant(user_id, self.db_path)

    def get_all_participants(self) -> list[dict[str, Any]]:
        return get_all_participants(self.db_path)

    def get_participants_count(self) -> int:
        return get_participants_count(self.db_path)

    def reset_contest(self) -> None:
        reset_contest(self.db_path)

    @staticmethod
    def format_participant_line(p: dict[str, Any], include_number: bool = True) -> str:
        return format_participant_line(p, include_number=include_number)
