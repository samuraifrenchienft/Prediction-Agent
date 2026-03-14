"""
Trader Cache — SQLite persistence for Polymarket trader scores.
===============================================================

Three tiers of storage:

  Tier 2 — trader_profiles   (full vet, 24h TTL)
    Written by daily refresh + /wallet command.
    Read by /traders and /wallet.

  Tier 0/1 — discovery_pool  (fast-scored candidates, 6h TTL)
    Written by hourly discovery_sweep job (5 leaderboard categories × 100 wallets).
    Tier 0: raw leaderboard data only (pnl, volume, rank).
    Tier 1: fast ROI score computed in-process (0 API calls).
    Wallets with high fast_score graduate to Tier 2 full-vet queue.

  Watchlist — watchlist       (user-added wallets, no expiry)
    Written by /watch command.
    Read by watchlist_vet_job every 6h and /watchlist command.
    Full vet always run on watchlist wallets.

DB lives at edge_agent/memory/data/trader_cache.db.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_DB_PATH      = Path(__file__).parent / "data" / "trader_cache.db"
_TTL_SECS     = 86400   # 24h — full-vet profiles
_POOL_TTL     = 21600   # 6h  — discovery pool entries
_WATCHLIST_VET_INTERVAL = 21600   # 6h default re-vet interval for watchlist


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────────

def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init_db(conn: sqlite3.Connection) -> None:
    # ── Tier 2: full-vet profiles ──────────────────────────────────────────
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
            -- specialization
            top_categories         TEXT NOT NULL DEFAULT '',
            -- vetting signals
            timing_score           REAL NOT NULL DEFAULT 0,
            consistency_score      REAL NOT NULL DEFAULT 0,
            fade_score             REAL NOT NULL DEFAULT 0,
            sizing_discipline      REAL NOT NULL DEFAULT 0,
            -- on-chain wallet signals (Polygon RPC + Goldsky subgraph)
            wallet_nonce           INTEGER NOT NULL DEFAULT -1,
            is_fresh_wallet        INTEGER NOT NULL DEFAULT 0,
            onchain_trade_count    INTEGER NOT NULL DEFAULT 0,
            onchain_burst_flag     INTEGER NOT NULL DEFAULT 0,
            -- meta
            fetched_at         REAL NOT NULL,
            expires_at         REAL NOT NULL
        )
    """)

    # Migrations: add columns to existing DBs that predate them
    for _col, _ddl in [
        ("top_categories",      "TEXT NOT NULL DEFAULT ''"),
        ("timing_score",        "REAL NOT NULL DEFAULT 0"),
        ("consistency_score",   "REAL NOT NULL DEFAULT 0"),
        ("fade_score",          "REAL NOT NULL DEFAULT 0"),
        ("sizing_discipline",   "REAL NOT NULL DEFAULT 0"),
        ("wallet_nonce",        "INTEGER NOT NULL DEFAULT -1"),
        ("is_fresh_wallet",     "INTEGER NOT NULL DEFAULT 0"),
        ("onchain_trade_count", "INTEGER NOT NULL DEFAULT 0"),
        ("onchain_burst_flag",  "INTEGER NOT NULL DEFAULT 0"),
    ]:
        try:
            conn.execute(f"ALTER TABLE trader_profiles ADD COLUMN {_col} {_ddl}")
            conn.commit()
        except Exception:
            pass  # column already exists

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_trader_score "
        "ON trader_profiles(final_score)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_trader_expires "
        "ON trader_profiles(expires_at)"
    )

    # ── Tier 0/1: discovery pool ───────────────────────────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS discovery_pool (
            wallet_address  TEXT PRIMARY KEY,
            display_name    TEXT NOT NULL DEFAULT '',
            -- leaderboard metadata
            category        TEXT NOT NULL DEFAULT '',   -- profit|volume|monthly|weekly|daily
            lb_rank         INTEGER NOT NULL DEFAULT 0,
            -- raw leaderboard figures (Tier 0 — always populated)
            pnl_alltime     REAL NOT NULL DEFAULT 0,
            volume_alltime  REAL NOT NULL DEFAULT 0,
            -- fast-scored fields (Tier 1 — populated during fast-score pass)
            roi             REAL NOT NULL DEFAULT 0,    -- pnl / volume
            fast_score      REAL NOT NULL DEFAULT 0,   -- 0-100, computed in-process
            bot_preflag     INTEGER NOT NULL DEFAULT 0, -- 1 = suspicious before full vet
            vet_priority    INTEGER NOT NULL DEFAULT 0, -- higher = vet sooner
            full_vet_done   INTEGER NOT NULL DEFAULT 0, -- 1 = graduated to trader_profiles
            -- meta
            discovered_at   REAL NOT NULL DEFAULT 0,
            expires_at      REAL NOT NULL DEFAULT 0
        )
    """)

    # Migration: add columns that may not exist in older discovery_pool tables
    for _col, _ddl in [
        ("bot_preflag",   "INTEGER NOT NULL DEFAULT 0"),
        ("vet_priority",  "INTEGER NOT NULL DEFAULT 0"),
        ("full_vet_done", "INTEGER NOT NULL DEFAULT 0"),
    ]:
        try:
            conn.execute(f"ALTER TABLE discovery_pool ADD COLUMN {_col} {_ddl}")
            conn.commit()
        except Exception:
            pass

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pool_score "
        "ON discovery_pool(fast_score DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pool_priority "
        "ON discovery_pool(vet_priority DESC, fast_score DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pool_expires "
        "ON discovery_pool(expires_at)"
    )

    # ── Watchlist ──────────────────────────────────────────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS watchlist (
            wallet_address      TEXT PRIMARY KEY,
            display_name        TEXT NOT NULL DEFAULT '',
            added_by            TEXT NOT NULL DEFAULT '',  -- telegram user_id or 'system'
            note                TEXT NOT NULL DEFAULT '',
            -- vet scheduling
            last_vetted_at      REAL NOT NULL DEFAULT 0,
            vet_interval_sec    INTEGER NOT NULL DEFAULT 21600,  -- 6h default
            -- latest score snapshot (populated after each full vet)
            latest_score        REAL NOT NULL DEFAULT 0,
            latest_bot_flag     INTEGER NOT NULL DEFAULT 0,
            -- meta
            added_at            REAL NOT NULL DEFAULT 0
        )
    """)

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_watchlist_due "
        "ON watchlist(last_vetted_at)"
    )

    conn.commit()


# ──────────────────────────────────────────────────────────────────────────────
# Main cache class
# ──────────────────────────────────────────────────────────────────────────────

class TraderCache:
    def __init__(self) -> None:
        self._conn = _connect()
        _init_db(self._conn)

    # ══════════════════════════════════════════════════════════════════════
    # TIER 2 — full-vet trader_profiles
    # ══════════════════════════════════════════════════════════════════════

    def upsert(self, row: dict[str, Any]) -> None:
        now = time.time()
        row.setdefault("fetched_at", now)
        row.setdefault("expires_at", now + _TTL_SECS)
        cols         = ", ".join(row.keys())
        placeholders = ", ".join("?" * len(row))
        updates      = ", ".join(
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
        # Mark as fully vetted in the pool (if present)
        with self._conn:
            self._conn.execute(
                "UPDATE discovery_pool SET full_vet_done = 1 "
                "WHERE wallet_address = ?",
                (row.get("wallet_address", "").lower(),),
            )
        self.cleanup()

    def cleanup(self) -> int:
        """Remove expired full-vet profiles and stale pool entries."""
        now = time.time()
        with self._conn:
            r1 = self._conn.execute(
                "DELETE FROM trader_profiles WHERE expires_at <= ?", (now,)
            )
            r2 = self._conn.execute(
                "DELETE FROM discovery_pool WHERE expires_at <= ?", (now,)
            )
        return r1.rowcount + r2.rowcount

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

    # ══════════════════════════════════════════════════════════════════════
    # TIER 0/1 — discovery_pool
    # ══════════════════════════════════════════════════════════════════════

    def pool_upsert(self, row: dict[str, Any]) -> None:
        """
        Insert or refresh a discovery pool entry.
        Normalises wallet_address to lowercase.
        Sets discovered_at + expires_at if not provided.
        """
        now = time.time()
        row = dict(row)
        row["wallet_address"] = row.get("wallet_address", "").lower()
        row.setdefault("discovered_at", now)
        row.setdefault("expires_at",    now + _POOL_TTL)

        cols         = ", ".join(row.keys())
        placeholders = ", ".join("?" * len(row))
        updates      = ", ".join(
            f"{k} = excluded.{k}"
            for k in row.keys()
            if k != "wallet_address"
        )
        with self._conn:
            self._conn.execute(
                f"""
                INSERT INTO discovery_pool ({cols})
                VALUES ({placeholders})
                ON CONFLICT(wallet_address) DO UPDATE SET {updates}
                """,
                list(row.values()),
            )

    def pool_get_vet_queue(
        self,
        limit: int = 20,
        min_fast_score: float = 30.0,
        exclude_done: bool = True,
    ) -> list[dict[str, Any]]:
        """
        Return the top candidates for full Tier-2 vetting.

        Ordered by: vet_priority DESC, fast_score DESC.
        Skips wallets already in trader_profiles (full_vet_done = 1) unless
        exclude_done=False.
        """
        done_filter = "AND full_vet_done = 0" if exclude_done else ""
        rows = self._conn.execute(
            f"""
            SELECT dp.*
            FROM   discovery_pool dp
            WHERE  dp.expires_at > ?
              AND  dp.fast_score >= ?
              {done_filter}
            ORDER BY dp.vet_priority DESC, dp.fast_score DESC
            LIMIT  ?
            """,
            (time.time(), min_fast_score, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def pool_stats(self) -> dict[str, Any]:
        """Summary stats for the discovery pool."""
        now = time.time()
        row = self._conn.execute(
            """
            SELECT
                COUNT(*)                        AS total,
                SUM(CASE WHEN full_vet_done=1 THEN 1 ELSE 0 END) AS vetted,
                SUM(CASE WHEN bot_preflag=1   THEN 1 ELSE 0 END) AS preflaged,
                AVG(fast_score)                 AS avg_fast_score,
                MAX(discovered_at)              AS last_discovery
            FROM discovery_pool
            WHERE expires_at > ?
            """,
            (now,),
        ).fetchone()
        if not row or not row["total"]:
            return {
                "total": 0, "vetted": 0, "preflaged": 0,
                "avg_fast_score": 0, "last_discovery": "never",
            }
        last_dt = (
            datetime.fromtimestamp(row["last_discovery"], tz=timezone.utc).strftime("%H:%M UTC")
            if row["last_discovery"]
            else "never"
        )
        return {
            "total":          row["total"],
            "vetted":         row["vetted"] or 0,
            "preflaged":      row["preflaged"] or 0,
            "avg_fast_score": round(row["avg_fast_score"] or 0, 1),
            "last_discovery": last_dt,
        }

    def pool_get_by_category(
        self, category: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        """All live pool entries for a given leaderboard category."""
        rows = self._conn.execute(
            "SELECT * FROM discovery_pool "
            "WHERE category = ? AND expires_at > ? "
            "ORDER BY fast_score DESC LIMIT ?",
            (category, time.time(), limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ══════════════════════════════════════════════════════════════════════
    # WATCHLIST
    # ══════════════════════════════════════════════════════════════════════

    def watchlist_add(
        self,
        address: str,
        display_name: str = "",
        added_by: str = "user",
        note: str = "",
        vet_interval_sec: int = _WATCHLIST_VET_INTERVAL,
    ) -> bool:
        """
        Add a wallet to the watchlist. Returns True if newly added,
        False if it already exists (updates note/display_name if changed).
        """
        addr = address.lower()
        now  = time.time()
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO watchlist
                    (wallet_address, display_name, added_by, note, vet_interval_sec, added_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(wallet_address) DO UPDATE SET
                    display_name     = CASE WHEN excluded.display_name != '' THEN excluded.display_name ELSE display_name END,
                    note             = CASE WHEN excluded.note != '' THEN excluded.note ELSE note END,
                    vet_interval_sec = excluded.vet_interval_sec
                """,
                (addr, display_name, added_by, note, vet_interval_sec, now),
            )
        return cur.lastrowid is not None

    def watchlist_remove(self, address: str) -> bool:
        """Remove a wallet from the watchlist. Returns True if it existed."""
        addr = address.lower()
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM watchlist WHERE wallet_address = ?", (addr,)
            )
        return cur.rowcount > 0

    def watchlist_list(self, added_by: str | None = None) -> list[dict[str, Any]]:
        """
        Return all watchlist entries, optionally filtered by who added them.
        Joined with latest score from trader_profiles if available.
        """
        filter_clause = "WHERE w.added_by = ?" if added_by else ""
        params: list[Any] = [added_by] if added_by else []

        rows = self._conn.execute(
            f"""
            SELECT
                w.*,
                COALESCE(tp.final_score, w.latest_score) AS current_score,
                tp.bot_flag                               AS current_bot_flag,
                tp.pnl_alltime                            AS tp_pnl,
                tp.win_rate_alltime                       AS tp_win_rate
            FROM watchlist w
            LEFT JOIN trader_profiles tp
                ON  tp.wallet_address = w.wallet_address
                AND tp.expires_at > {time.time()}
            {filter_clause}
            ORDER BY w.added_at DESC
            """,
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def watchlist_due_for_vet(self) -> list[dict[str, Any]]:
        """
        Return watchlist wallets whose last_vetted_at is older than their
        vet_interval_sec. Used by the background watchlist_vet_job.
        """
        now  = time.time()
        rows = self._conn.execute(
            """
            SELECT * FROM watchlist
            WHERE (last_vetted_at + vet_interval_sec) <= ?
            ORDER BY last_vetted_at ASC
            """,
            (now,),
        ).fetchall()
        return [dict(r) for r in rows]

    def watchlist_mark_vetted(
        self,
        address: str,
        score: float = 0.0,
        bot_flag: int = 0,
    ) -> None:
        """
        Record that a watchlist wallet just received a full vet.
        Stores the latest score snapshot for display in /watchlist.
        """
        addr = address.lower()
        now  = time.time()
        with self._conn:
            self._conn.execute(
                """
                UPDATE watchlist
                SET last_vetted_at  = ?,
                    latest_score    = ?,
                    latest_bot_flag = ?
                WHERE wallet_address = ?
                """,
                (now, score, bot_flag, addr),
            )

    def watchlist_get(self, address: str) -> dict[str, Any] | None:
        """Fetch a single watchlist entry by address."""
        row = self._conn.execute(
            "SELECT * FROM watchlist WHERE wallet_address = ?",
            (address.lower(),),
        ).fetchone()
        return dict(row) if row else None

    def watchlist_count(self, added_by: str | None = None) -> int:
        """Count watchlist entries, optionally filtered by adder."""
        if added_by:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM watchlist WHERE added_by = ?", (added_by,)
            ).fetchone()
        else:
            row = self._conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()
        return row[0] if row else 0
