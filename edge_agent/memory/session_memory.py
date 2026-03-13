"""
Edge Session Memory — daily short-term memory stored in SQLite, per user.

Tracks conversation history, markets discussed, and user preferences
within the current day. Gives Edge continuity across a session so it
can reference earlier parts of the conversation.

Each user gets their own session row keyed by (user_id, session_date).
user_id=0 is the legacy single-user fallback.

Data resets context after 24 hours but history is kept permanently.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Any

_DB_PATH = Path(__file__).parent / "data" / "sessions.db"


class SessionMemory:
    """
    Daily per-user session memory for Edge.

    Usage:
        mem = SessionMemory(user_id=12345678)
        mem.add_exchange("What is a prediction market?", "A prediction market is...")
        ctx = mem.get_session_context()   # inject into AI prompt
        mem.set_preference("risk_level", "conservative")
    """

    def __init__(self, db_path: Path = _DB_PATH, user_id: int = 0) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._user_id = user_id
        self._session_date = date.today().isoformat()
        self._setup()
        self._ensure_today_session()

    def _setup(self) -> None:
        c = self._conn
        # Create with user_id from the start
        c.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id            INTEGER PRIMARY KEY,
                user_id       INTEGER NOT NULL DEFAULT 0,
                session_date  TEXT NOT NULL,
                exchanges     TEXT NOT NULL DEFAULT '[]',
                markets       TEXT NOT NULL DEFAULT '[]',
                preferences   TEXT NOT NULL DEFAULT '{}',
                UNIQUE(user_id, session_date)
            )
        """)
        # Migrate legacy schema that had UNIQUE on session_date alone
        try:
            c.execute("ALTER TABLE sessions ADD COLUMN user_id INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass  # column already exists — fine
        c.commit()

    def _ensure_today_session(self) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO sessions (user_id, session_date) VALUES (?, ?)",
            (self._user_id, self._session_date),
        )
        self._conn.commit()

    def _get_today(self) -> dict:
        row = self._conn.execute(
            "SELECT exchanges, markets, preferences FROM sessions "
            "WHERE user_id = ? AND session_date = ?",
            (self._user_id, self._session_date),
        ).fetchone()
        return {
            "exchanges":  json.loads(row[0]),
            "markets":    json.loads(row[1]),
            "preferences": json.loads(row[2]),
        }

    def _save_today(self, exchanges: list, markets: list, preferences: dict) -> None:
        self._conn.execute(
            """UPDATE sessions SET exchanges=?, markets=?, preferences=?
               WHERE user_id = ? AND session_date=?""",
            (
                json.dumps(exchanges),
                json.dumps(markets),
                json.dumps(preferences),
                self._user_id,
                self._session_date,
            ),
        )
        self._conn.commit()

    # ── Public API ─────────────────────────────────────────────────────────────

    def add_exchange(
        self,
        question: str,
        answer: str,
        markets_discussed: list[str] | None = None,
        topics: list[str] | None = None,
    ) -> None:
        """Record a Q&A exchange in today's session."""
        data = self._get_today()
        exchange = {
            "time": datetime.now(timezone.utc).strftime("%H:%M"),
            "q": question[:300],   # cap length for storage
            "a": answer[:600],
        }
        if topics:
            exchange["topics"] = topics
        data["exchanges"].append(exchange)

        # Track market tickers mentioned
        if markets_discussed:
            existing = set(data["markets"])
            existing.update(markets_discussed)
            data["markets"] = list(existing)

        self._save_today(data["exchanges"], data["markets"], data["preferences"])

    def set_preference(self, key: str, value: Any) -> None:
        """Store a user preference (e.g. risk_level, bankroll, platform)."""
        data = self._get_today()
        data["preferences"][key] = value
        self._save_today(data["exchanges"], data["markets"], data["preferences"])

    def get_preferences(self) -> dict:
        return self._get_today()["preferences"]

    def get_session_context(self, max_exchanges: int = 5) -> str:
        """
        Returns a formatted summary of today's session for injection
        into the AI prompt. Includes recent Q&A and user preferences.
        Returns empty string if session is fresh with no history.
        """
        data = self._get_today()
        parts: list[str] = []

        prefs = data["preferences"]
        if prefs:
            pref_str = ", ".join(f"{k}: {v}" for k, v in prefs.items())
            parts.append(f"User preferences: {pref_str}")

        markets = data["markets"]
        if markets:
            parts.append(f"Markets discussed today: {', '.join(markets[:10])}")

        exchanges = data["exchanges"][-max_exchanges:]
        if exchanges:
            parts.append("Earlier in this session:")
            for ex in exchanges:
                parts.append(f"  [{ex['time']}] User: {ex['q']}")
                parts.append(
                    f"           Edge: {ex['a'][:200]}"
                    f"{'...' if len(ex['a']) > 200 else ''}"
                )

        if not parts:
            return ""

        return "\n\nSession context:\n" + "\n".join(parts)

    def get_markets_discussed(self) -> list[str]:
        return self._get_today()["markets"]

    def get_exchange_count(self) -> int:
        return len(self._get_today()["exchanges"])

    def stats(self) -> dict:
        today = self._get_today()
        total_sessions = self._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        total_exchanges = sum(
            len(json.loads(row[0]))
            for row in self._conn.execute("SELECT exchanges FROM sessions").fetchall()
        )
        return {
            "session_date":           self._session_date,
            "user_id":                self._user_id,
            "today_exchanges":        len(today["exchanges"]),
            "today_markets":          len(today["markets"]),
            "total_sessions":         total_sessions,
            "total_exchanges_all_time": total_exchanges,
            "preferences":            today["preferences"],
        }

    def close(self) -> None:
        self._conn.close()
