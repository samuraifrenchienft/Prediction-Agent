"""
Injury Cache — SQLite persistence for daily injury data.
=========================================================

Records are written by the scheduled injury refresh job (every 4 hours) and
read by InjuryAPIClient.build_injury_catalysts() during market scans so scans
never make live HTTP calls to injury APIs.

Retention policy:
  • Records expire after 24 hours (configurable via INJURY_CACHE_TTL_HOURS).
  • cleanup() removes expired rows and is called automatically on every write.
  • The DB file lives at edge_agent/memory/data/injury_cache.db.

Change detection:
  • fetch_and_store() in injury_api.py compares new records against previous
    cache and calls store_change_alerts() when a player's status worsens.
  • injury_refresh_job() in run_edge_bot.py calls get_pending_change_alerts()
    after each refresh to dispatch proactive Telegram alerts.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_DB_PATH = Path(__file__).parent / "data" / "injury_cache.db"
_TTL_HOURS = 24  # records older than this are discarded on next write

# Severity ordering for get_all() sorting (index 0 = most severe)
_SEVERITY_ORDER = ["Out", "Suspension", "Doubtful", "Questionable", "Day-To-Day"]


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS injury_cache (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            sport        TEXT NOT NULL,
            player_name  TEXT NOT NULL,
            team         TEXT NOT NULL,
            position     TEXT NOT NULL DEFAULT '',
            status       TEXT NOT NULL,
            injury_type  TEXT NOT NULL DEFAULT '',
            injury_detail TEXT NOT NULL DEFAULT '',
            return_date  TEXT NOT NULL DEFAULT '',
            comment      TEXT NOT NULL DEFAULT '',
            source_api   TEXT NOT NULL DEFAULT 'espn',
            fetched_at   REAL NOT NULL,
            expires_at   REAL NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sport ON injury_cache(sport)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_expires ON injury_cache(expires_at)")

    # Change detection alerts — proactive Telegram notifications
    conn.execute("""
        CREATE TABLE IF NOT EXISTS injury_change_alerts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            sport       TEXT NOT NULL,
            player_name TEXT NOT NULL,
            team        TEXT NOT NULL DEFAULT '',
            position    TEXT NOT NULL DEFAULT '',
            old_status  TEXT NOT NULL,
            new_status  TEXT NOT NULL,
            created_at  REAL NOT NULL,
            sent        INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.commit()


class InjuryCache:
    """
    SQLite-backed daily injury store.

    Usage:
        cache = InjuryCache()
        cache.store("nba", records)                 # write ESPN/PDF records
        records = cache.get("nba")                  # read for catalyst building
        all_nba = cache.get_all("nba")              # read for /injuries command
        cache.store_change_alerts(changes)          # write status-worsening alerts
        pending = cache.get_pending_change_alerts() # read + mark as sent
        cache.cleanup()                             # remove expired rows
    """

    def __init__(self) -> None:
        self._conn = _connect()
        _init_db(self._conn)

    # ── Write ────────────────────────────────────────────────────────────────

    def store(self, sport: str, records: list[dict[str, Any]]) -> None:
        """
        Replace all cached records for this sport with new ones.
        Old records for the sport are deleted before inserting so there are no
        stale duplicates. Also prunes expired records for ALL sports.
        """
        now = time.time()
        expires = now + _TTL_HOURS * 3600

        with self._conn:
            # Remove existing records for this sport (full replacement)
            self._conn.execute(
                "DELETE FROM injury_cache WHERE sport = ?", (sport.lower(),)
            )

            for r in records:
                self._conn.execute(
                    """
                    INSERT INTO injury_cache
                        (sport, player_name, team, position, status,
                         injury_type, injury_detail, return_date, comment,
                         source_api, fetched_at, expires_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        sport.lower(),
                        r.get("player_name", ""),
                        r.get("team", ""),
                        r.get("position", ""),
                        r.get("status", ""),
                        r.get("injury_type", ""),
                        r.get("injury_detail", ""),
                        r.get("return_date", ""),
                        r.get("comment", ""),
                        r.get("source_api", "espn"),
                        now,
                        expires,
                    ),
                )

        # Always clean up expired rows from all sports
        self.cleanup()

        log.info(
            "[InjuryCache] Stored %d %s records (expires in %dh)",
            len(records),
            sport.upper(),
            _TTL_HOURS,
        )

    # ── Read ─────────────────────────────────────────────────────────────────

    def get(self, sport: str) -> list[dict[str, Any]]:
        """
        Return non-expired injury records for the given sport.
        Returns [] if the cache is empty or all records have expired.
        """
        now = time.time()
        rows = self._conn.execute(
            """
            SELECT player_name, team, position, status,
                   injury_type, injury_detail, return_date, comment,
                   source_api, fetched_at
            FROM   injury_cache
            WHERE  sport = ? AND expires_at > ?
            ORDER BY player_name
            """,
            (sport.lower(), now),
        ).fetchall()

        return [dict(row) for row in rows]

    def get_all(
        self,
        sport: str,
        team_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Return all non-expired injury records for the given sport, sorted by
        severity (Out first) then team name.

        team_filter: optional case-insensitive substring match on team name.
        Used by the enhanced /injuries command to filter by team.
        """
        now = time.time()
        rows = self._conn.execute(
            """
            SELECT player_name, team, position, status,
                   injury_type, injury_detail, return_date, comment,
                   source_api, fetched_at
            FROM   injury_cache
            WHERE  sport = ? AND expires_at > ?
            """,
            (sport.lower(), now),
        ).fetchall()

        records = [dict(row) for row in rows]

        if team_filter:
            tf = team_filter.lower()
            records = [r for r in records if tf in r.get("team", "").lower()]

        def _sort_key(r: dict) -> tuple:
            try:
                sev_idx = _SEVERITY_ORDER.index(r.get("status", "Day-To-Day"))
            except ValueError:
                sev_idx = len(_SEVERITY_ORDER)
            return (sev_idx, r.get("team", ""), r.get("player_name", ""))

        records.sort(key=_sort_key)
        return records

    def is_fresh(self, sport: str, max_age_seconds: float = 14400) -> bool:
        """
        True if the cache has non-expired records for this sport fetched within
        max_age_seconds (default 4 hours). Used by the refresh job to decide
        whether to skip a redundant fetch.
        """
        now = time.time()
        row = self._conn.execute(
            """
            SELECT MAX(fetched_at) AS last_fetch
            FROM   injury_cache
            WHERE  sport = ? AND expires_at > ?
            """,
            (sport.lower(), now),
        ).fetchone()

        if not row or row["last_fetch"] is None:
            return False
        return (now - row["last_fetch"]) < max_age_seconds

    # ── Change detection alerts ───────────────────────────────────────────────

    def store_change_alerts(self, alerts: list[dict[str, Any]]) -> None:
        """
        Persist status-worsening alerts for proactive Telegram notification.
        Called by fetch_and_store() in injury_api.py when a player's status
        upgrades to a more severe category (e.g. Questionable → Out).
        """
        if not alerts:
            return
        now = time.time()
        with self._conn:
            for a in alerts:
                self._conn.execute(
                    """
                    INSERT INTO injury_change_alerts
                        (sport, player_name, team, position,
                         old_status, new_status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        a.get("sport", "").lower(),
                        a.get("player_name", ""),
                        a.get("team", ""),
                        a.get("position", ""),
                        a.get("old_status", ""),
                        a.get("new_status", ""),
                        now,
                    ),
                )
        log.info("[InjuryCache] Stored %d change alert(s)", len(alerts))

    def get_pending_change_alerts(self) -> list[dict[str, Any]]:
        """
        Return all unsent change alerts and mark them as sent in one transaction.
        Called by injury_refresh_job() to dispatch proactive Telegram messages.
        """
        rows = self._conn.execute(
            """
            SELECT id, sport, player_name, team, position,
                   old_status, new_status, created_at
            FROM   injury_change_alerts
            WHERE  sent = 0
            ORDER  BY created_at
            """
        ).fetchall()

        if not rows:
            return []

        ids = [row["id"] for row in rows]
        placeholders = ",".join("?" * len(ids))
        with self._conn:
            self._conn.execute(
                f"UPDATE injury_change_alerts SET sent = 1 WHERE id IN ({placeholders})",
                ids,
            )

        return [dict(row) for row in rows]

    # ── Maintenance ──────────────────────────────────────────────────────────

    def cleanup(self) -> int:
        """Delete expired rows from all sports. Returns count of deleted rows."""
        now = time.time()
        cur = self._conn.execute(
            "DELETE FROM injury_cache WHERE expires_at <= ?", (now,)
        )
        self._conn.commit()
        if cur.rowcount:
            log.info("[InjuryCache] Cleaned up %d expired rows", cur.rowcount)
        return cur.rowcount

    def stats(self) -> dict[str, Any]:
        """Summary of current cache state — useful for /status display."""
        rows = self._conn.execute(
            """
            SELECT sport,
                   COUNT(*) AS total,
                   MAX(fetched_at) AS last_fetch,
                   MIN(expires_at) AS earliest_expiry
            FROM   injury_cache
            WHERE  expires_at > ?
            GROUP  BY sport
            """,
            (time.time(),),
        ).fetchall()

        result: dict[str, Any] = {}
        for row in rows:
            last_fetch_dt = datetime.fromtimestamp(row["last_fetch"], tz=timezone.utc)
            expire_dt = datetime.fromtimestamp(row["earliest_expiry"], tz=timezone.utc)
            result[row["sport"].upper()] = {
                "count": row["total"],
                "last_fetch": last_fetch_dt.strftime("%H:%M UTC"),
                "expires": expire_dt.strftime("%H:%M UTC"),
            }
        return result
