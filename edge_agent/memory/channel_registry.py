"""
ChannelRegistry — per-user chat registration for multi-channel support.

Each user who starts the bot gets their private chat_id stored here.
Broadcast helpers fan out to all registered chats, giving every user
their own dedicated 1-on-1 session with EDGE.

Tables
------
allowed_users  — whitelist: user_ids permitted to use the bot (owner-managed)
registered_chats — maps user_id → their private chat_id (auto-populated on /start)
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_DB_PATH = Path(__file__).parent / "data" / "channel_registry.db"


class ChannelRegistry:
    def __init__(self, db_path: Path = _DB_PATH) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._setup()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _setup(self) -> None:
        c = self._conn
        c.execute("""
            CREATE TABLE IF NOT EXISTS allowed_users (
                user_id    INTEGER PRIMARY KEY,
                username   TEXT,
                added_by   INTEGER,
                added_at   TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS registered_chats (
                user_id    INTEGER PRIMARY KEY,
                chat_id    INTEGER NOT NULL,
                username   TEXT,
                first_name TEXT,
                first_seen TEXT NOT NULL DEFAULT (datetime('now')),
                last_seen  TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        c.commit()

    # ------------------------------------------------------------------
    # Whitelist management (owner commands)
    # ------------------------------------------------------------------

    def add_user(self, user_id: int, username: str = "", added_by: int = 0) -> bool:
        """Add user_id to the whitelist. Returns True if newly added."""
        try:
            self._conn.execute(
                """INSERT INTO allowed_users (user_id, username, added_by)
                   VALUES (?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET username=excluded.username""",
                (user_id, username or "", added_by),
            )
            self._conn.commit()
            log.info("ChannelRegistry: added user %d (%s)", user_id, username)
            return True
        except Exception as exc:
            log.warning("ChannelRegistry add_user failed: %s", exc)
            return False

    def remove_user(self, user_id: int) -> bool:
        """Remove user_id from whitelist AND their registered chat."""
        try:
            self._conn.execute("DELETE FROM allowed_users WHERE user_id = ?", (user_id,))
            self._conn.execute("DELETE FROM registered_chats WHERE user_id = ?", (user_id,))
            self._conn.commit()
            log.info("ChannelRegistry: removed user %d", user_id)
            return True
        except Exception as exc:
            log.warning("ChannelRegistry remove_user failed: %s", exc)
            return False

    def is_allowed(self, user_id: int) -> bool:
        """Return True if user_id is in the whitelist."""
        row = self._conn.execute(
            "SELECT 1 FROM allowed_users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row is not None

    def list_allowed(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT user_id, username, added_at FROM allowed_users ORDER BY added_at"
        ).fetchall()
        return [{"user_id": r[0], "username": r[1], "added_at": r[2]} for r in rows]

    # ------------------------------------------------------------------
    # Chat registration (auto on /start)
    # ------------------------------------------------------------------

    def register(
        self,
        user_id: int,
        chat_id: int,
        username: str = "",
        first_name: str = "",
    ) -> None:
        """Upsert a user's private chat_id. Called every /start."""
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """INSERT INTO registered_chats (user_id, chat_id, username, first_name, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   chat_id    = excluded.chat_id,
                   username   = excluded.username,
                   first_name = excluded.first_name,
                   last_seen  = excluded.last_seen""",
            (user_id, chat_id, username or "", first_name or "", now, now),
        )
        self._conn.commit()

    def touch(self, user_id: int) -> None:
        """Update last_seen timestamp for a user."""
        self._conn.execute(
            "UPDATE registered_chats SET last_seen = datetime('now') WHERE user_id = ?",
            (user_id,),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Broadcast helpers
    # ------------------------------------------------------------------

    def get_all_chat_ids(self) -> list[int]:
        """Return all registered chat_ids for broadcasting."""
        rows = self._conn.execute(
            """SELECT rc.chat_id FROM registered_chats rc
               INNER JOIN allowed_users au ON au.user_id = rc.user_id"""
        ).fetchall()
        return [r[0] for r in rows]

    def get_all_user_ids(self) -> list[int]:
        """Return all whitelisted user_ids (for auth filter)."""
        rows = self._conn.execute("SELECT user_id FROM allowed_users").fetchall()
        return [r[0] for r in rows]

    def get_chat_id(self, user_id: int) -> int | None:
        """Return the private chat_id for a specific user, or None."""
        row = self._conn.execute(
            "SELECT chat_id FROM registered_chats WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row[0] if row else None

    def get_registered_users(self) -> list[dict]:
        """Return info about all registered users (for /status admin view)."""
        rows = self._conn.execute(
            """SELECT rc.user_id, rc.chat_id, rc.username, rc.first_name,
                      rc.first_seen, rc.last_seen
               FROM registered_chats rc
               INNER JOIN allowed_users au ON au.user_id = rc.user_id
               ORDER BY rc.last_seen DESC"""
        ).fetchall()
        return [
            {
                "user_id":    r[0],
                "chat_id":    r[1],
                "username":   r[2],
                "first_name": r[3],
                "first_seen": r[4],
                "last_seen":  r[5],
            }
            for r in rows
        ]
