"""
Scan Log — SQLite persistence for scan run history and qualified signals.
=========================================================================

Written by _run_scan() after every scan. Read by /performance command.

Tables:
  scan_runs    — one row per scan (summary stats)
  scan_signals — one row per qualified recommendation per scan

DB lives at edge_agent/memory/data/scan_log.db
"""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_DB_PATH = Path(__file__).parent / "data" / "scan_log.db"


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scan_runs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          REAL    NOT NULL,
            total       INTEGER NOT NULL DEFAULT 0,
            qualified   INTEGER NOT NULL DEFAULT 0,
            watchlist   INTEGER NOT NULL DEFAULT 0,
            rejected    INTEGER NOT NULL DEFAULT 0,
            new_alerts  INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scan_signals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_run_id INTEGER NOT NULL,
            ts          REAL    NOT NULL,
            market_id   TEXT    NOT NULL,
            venue       TEXT    NOT NULL,
            signal_type TEXT,
            ev_net      REAL    NOT NULL DEFAULT 0,
            confidence  REAL    NOT NULL DEFAULT 0,
            action      TEXT,
            market_prob REAL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_scan_ts ON scan_runs(ts)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_signal_run ON scan_signals(scan_run_id)"
    )
    conn.commit()


class ScanLog:
    def __init__(self) -> None:
        self._conn = _connect()
        _init_db(self._conn)

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def log_scan(
        self,
        total: int,
        qualified: int,
        watchlist: int,
        rejected: int,
        new_alerts: int,
    ) -> int:
        """Insert a scan_runs row. Returns the new run_id."""
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO scan_runs (ts, total, qualified, watchlist, rejected, new_alerts)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (time.time(), total, qualified, watchlist, rejected, new_alerts),
            )
        return cur.lastrowid

    def log_signal(
        self,
        scan_run_id: int,
        market_id: str,
        venue: str,
        signal_type: str | None,
        ev_net: float,
        confidence: float,
        action: str | None,
        market_prob: float | None,
    ) -> None:
        """Insert a scan_signals row for a qualified recommendation."""
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO scan_signals
                    (scan_run_id, ts, market_id, venue, signal_type,
                     ev_net, confidence, action, market_prob)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scan_run_id,
                    time.time(),
                    market_id,
                    venue,
                    signal_type or "UNKNOWN",
                    ev_net,
                    confidence,
                    action or "",
                    market_prob or 0.0,
                ),
            )

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_summary(self, days: int = 30) -> dict[str, Any]:
        """Return performance summary for the last N days."""
        cutoff = time.time() - (days * 86400)

        run_row = self._conn.execute(
            """
            SELECT COUNT(*) AS scans,
                   SUM(total)     AS total_markets,
                   SUM(qualified) AS total_qualified,
                   SUM(watchlist) AS total_watchlist,
                   SUM(new_alerts) AS total_alerts
            FROM scan_runs
            WHERE ts >= ?
            """,
            (cutoff,),
        ).fetchone()

        sig_rows = self._conn.execute(
            """
            SELECT signal_type,
                   COUNT(*)        AS count,
                   AVG(ev_net)     AS avg_ev,
                   AVG(confidence) AS avg_conf,
                   MAX(ev_net)     AS best_ev
            FROM scan_signals
            WHERE ts >= ?
            GROUP BY signal_type
            ORDER BY count DESC
            """,
            (cutoff,),
        ).fetchall()

        best_sig_row = self._conn.execute(
            """
            SELECT market_id, venue, signal_type, ev_net, confidence, ts
            FROM scan_signals
            WHERE ts >= ?
            ORDER BY ev_net DESC
            LIMIT 1
            """,
            (cutoff,),
        ).fetchone()

        scans        = run_row["scans"] or 0
        total_qual   = run_row["total_qualified"] or 0
        total_alerts = run_row["total_alerts"] or 0

        signal_breakdown = [
            {
                "signal":   dict(r)["signal_type"],
                "count":    dict(r)["count"],
                "avg_ev":   dict(r)["avg_ev"] or 0.0,
                "avg_conf": dict(r)["avg_conf"] or 0.0,
                "best_ev":  dict(r)["best_ev"] or 0.0,
            }
            for r in sig_rows
        ]

        best = dict(best_sig_row) if best_sig_row else None
        if best:
            best["ts_str"] = datetime.fromtimestamp(
                best["ts"], tz=timezone.utc
            ).strftime("%b %d %H:%M UTC")

        return {
            "days":             days,
            "scans":            scans,
            "total_markets":    run_row["total_markets"] or 0,
            "total_qualified":  total_qual,
            "total_watchlist":  run_row["total_watchlist"] or 0,
            "total_alerts":     total_alerts,
            "avg_qual_per_scan": round(total_qual / scans, 2) if scans else 0.0,
            "signal_breakdown": signal_breakdown,
            "best_signal":      best,
        }

    def recent_scans(self, limit: int = 5) -> list[dict[str, Any]]:
        """Return the most recent N scan_runs rows."""
        rows = self._conn.execute(
            "SELECT * FROM scan_runs ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
