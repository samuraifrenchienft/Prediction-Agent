"""
Trader Cache — SQLite persistence for Polymarket trader scores.
===============================================================

Records are written by the daily trader refresh job and read by
/traders and /wallet Telegram commands.

Retention policy:
  • Scores expire after 2 hours (refreshed daily at 8am PT or on-demand).
  • cleanup() removes expired rows on every write.
  • DB lives at edge_agent/memory/data/trader_cache.db.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_DB_PATH  = Path(__file__).parent / "data" / "trader_cache.db"
_TTL_SECS = 7200  # 2 hours


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trader_profiles (
            wallet_address     TEXT PRIMARY KEY,
            display_name       TEXT NOT NULL DEFAULT '',
            verified           INTEGER NOT NULL DEFAULT 0,
            -- composite score
            final_score        REAL NOT NULL DEFAULT 0,
            anti_bot_score     REAL NOT NULL DEFAULT 0,
            performance_score  REAL NOT NULL DEFAULT 0,
            reliability_score  REAL NOT NULL DEFAULT 0,
            bot_flag           INTEGER NOT NULL DEFAULT 0,
            -- performance by window
            win_rate_alltime   REAL NOT NULL DEFAULT 0,
            win_rate_30d       REAL NOT NULL DEFAULT 0,
            win_rate_7d        REAL NOT NULL DEFAULT 0,
            pnl_alltime        REAL NOT NULL DEFAULT 0,
            pnl_alltime_adj    REAL NOT NULL DEFAULT 0,
            pnl_30d            REAL NOT NULL DEFAULT 0,
            pnl_7d             REAL NOT NULL DEFAULT 0,
            volume_alltime     REAL NOT NULL DEFAULT 0,
            trades_alltime     INTEGER NOT NULL DEFAULT 0,
            -- streak
            current_streak     INTEGER NOT NULL DEFAULT 0,
            max_streak_50      INTEGER NOT NULL DEFAULT 0,
            -- hidden-loss risk
            unsettled_count        INTEGER NOT NULL DEFAULT 0,
            hidden_loss_exposure   REAL NOT NULL DEFAULT 0,
            -- meta
            fetched_at         REAL NOT NULL,
            expires_at         REAL NOT NULL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_trader_score "
        "ON trader_profiles(final_score)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_trader_expires "
        "ON trader_profiles(expires_at)"
    )
    conn.commit()


class TraderCache:
    def __init__(self) -> None:
        self._conn = _connect()
        _init_db(self._conn)

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def upsert(self, row: dict[str, Any]) -> None:
        now = time.time()
        row.setdefault("fetched_at", now)
        row.setdefault("expires_at", now + _TTL_SECS)
        cols   = ", ".join(row.keys())
        placeholders = ", ".join("?" * len(row))
        updates = ", ".join(
            f"{k} = excluded.{k}"
            for k in row.keys()
            if k != "wallet_address"
        )
        with self._conn:
            self._conn.execute(
                f"""
                INSERT INTO trader_profiles ({cols})
                VALUES ({placeholders})
                ON CONFLICT(wallet_address) DO UPDATE SET {updates}
                """,
                list(row.values()),
            )
        self.cleanup()

    def cleanup(self) -> int:
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM trader_profiles WHERE expires_at <= ?",
                (time.time(),),
            )
        return cur.rowcount

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get(self, address: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM trader_profiles "
            "WHERE wallet_address = ? AND expires_at > ?",
            (address.lower(), time.time()),
        ).fetchone()
        return dict(row) if row else None

    def get_top(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM trader_profiles "
            "WHERE expires_at > ? AND bot_flag = 0 "
            "ORDER BY final_score DESC LIMIT ?",
            (time.time(), limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict[str, Any]:
        row = self._conn.execute(
            """
            SELECT COUNT(*) AS total,
                   MAX(fetched_at) AS last_fetch,
                   AVG(final_score) AS avg_score
            FROM   trader_profiles
            WHERE  expires_at > ?
            """,
            (time.time(),),
        ).fetchone()
        if not row or not row["total"]:
            return {"count": 0, "last_fetch": "never", "avg_score": 0}
        last_dt = datetime.fromtimestamp(row["last_fetch"], tz=timezone.utc)
        return {
            "count":      row["total"],
            "last_fetch": last_dt.strftime("%H:%M UTC"),
            "avg_score":  round(row["avg_score"] or 0, 1),
        }
