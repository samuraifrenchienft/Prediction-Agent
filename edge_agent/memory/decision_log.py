"""
Decision Log — SQLite Audit Trail for Every AI Decision.
=========================================================

Why this exists:
  When the bot gives a wrong answer, you need to know:
    1. Which model actually answered (OpenRouter? Groq? Which one?)
    2. Which version of the prompt was used
    3. Which context blocks were active (market data? injuries? scan?)
    4. How long it took (latency spike = API degradation)
    5. Was correction mode on? Was it a structured or chat call?

  Without this log, all of that is lost. With it, debugging a bad response
  means: fetch the row for that timestamp, read model + prompt_version + context_blocks,
  and you immediately know why the AI said what it said.

Tables:
  ai_decisions   — one row per AI call (chat or structured)
  prompt_changes — optional log of prompt template changes over time

DB lives at edge_agent/memory/data/decision_log.db
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_DB_PATH = Path(__file__).parent / "data" / "decision_log.db"
_MAX_TEXT_STORE = 2000   # truncate stored prompt/response text after this many chars
_RETAIN_DAYS    = 30     # auto-purge decisions older than 30 days


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ai_decisions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              REAL    NOT NULL,
            call_type       TEXT    NOT NULL,   -- 'chat' | 'structured'
            model_used      TEXT    NOT NULL,   -- e.g. 'deepseek/deepseek-chat-v3-0324:free'
            prompt_version  TEXT    NOT NULL,   -- e.g. 'chat_system@2.3' | 'unknown'
            context_blocks  TEXT    NOT NULL DEFAULT '[]',  -- JSON list of active blocks
            -- What went in / came out (truncated for storage)
            prompt_snippet  TEXT    NOT NULL DEFAULT '',    -- first 500 chars of system prompt
            response_snippet TEXT   NOT NULL DEFAULT '',   -- first 500 chars of response
            -- Hashes for exact comparison without storing full text
            prompt_hash     TEXT    NOT NULL DEFAULT '',
            response_hash   TEXT    NOT NULL DEFAULT '',
            -- Performance and mode flags
            latency_ms      INTEGER NOT NULL DEFAULT 0,
            tokens_estimate INTEGER NOT NULL DEFAULT 0,
            correction_mode INTEGER NOT NULL DEFAULT 0,    -- 1 = user correction was active
            regime_safe     INTEGER NOT NULL DEFAULT 1,    -- 0 = ML drift was detected
            user_id         TEXT    NOT NULL DEFAULT '',   -- Telegram user_id if applicable
            -- Outcome (filled in later if we can match to an outcome)
            outcome         TEXT    -- 'correct' | 'corrected_by_user' | 'unknown'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS prompt_changes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          REAL    NOT NULL,
            prompt_name TEXT    NOT NULL,
            old_version TEXT    NOT NULL,
            new_version TEXT    NOT NULL,
            notes       TEXT    NOT NULL DEFAULT '',
            changed_by  TEXT    NOT NULL DEFAULT 'code'
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dec_ts   ON ai_decisions(ts)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dec_user ON ai_decisions(user_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dec_model ON ai_decisions(model_used)"
    )
    conn.commit()


class DecisionLog:
    """
    Write-and-forget audit log for every AI call.

    Usage:
        dlog = DecisionLog()
        entry_id = dlog.log(
            call_type      = "chat",
            model_used     = "deepseek/deepseek-chat-v3-0324:free",
            prompt_version = "chat_system@2.3",
            context_blocks = ["market_data", "injuries", "session"],
            system_prompt  = full_system_prompt,
            response       = ai_response,
            latency_ms     = 840,
            correction_mode= True,
            user_id        = str(telegram_user_id),
        )
    """

    def __init__(self) -> None:
        self._conn = _connect()
        _init_db(self._conn)

    def log(
        self,
        call_type:       str,
        model_used:      str,
        prompt_version:  str = "unknown",
        context_blocks:  list[str] | None = None,
        system_prompt:   str = "",
        response:        str = "",
        latency_ms:      int = 0,
        tokens_estimate: int = 0,
        correction_mode: bool = False,
        regime_safe:     bool = True,
        user_id:         str = "",
    ) -> int:
        """
        Log one AI decision. Returns the new row id.
        Never raises — any error is swallowed silently (audit log must never break the bot).
        """
        try:
            now = time.time()

            # Snippets (truncated — not full text for storage efficiency)
            prompt_snip   = system_prompt[:500] if system_prompt else ""
            response_snip = response[:500]       if response      else ""

            # Hashes (for exact comparison without full-text retrieval)
            p_hash = hashlib.sha256(system_prompt.encode()).hexdigest()[:16] if system_prompt else ""
            r_hash = hashlib.sha256(response.encode()).hexdigest()[:16]      if response      else ""

            with self._conn:
                cur = self._conn.execute(
                    """
                    INSERT INTO ai_decisions
                        (ts, call_type, model_used, prompt_version, context_blocks,
                         prompt_snippet, response_snippet, prompt_hash, response_hash,
                         latency_ms, tokens_estimate, correction_mode, regime_safe, user_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        now,
                        call_type,
                        model_used,
                        prompt_version,
                        json.dumps(context_blocks or []),
                        prompt_snip,
                        response_snip,
                        p_hash,
                        r_hash,
                        latency_ms,
                        tokens_estimate,
                        int(correction_mode),
                        int(regime_safe),
                        str(user_id),
                    ),
                )
            return cur.lastrowid
        except Exception as exc:
            log.debug("[DecisionLog] log() failed silently: %s", exc)
            return -1

    def mark_outcome(self, decision_id: int, outcome: str) -> None:
        """
        Mark a decision as 'correct', 'corrected_by_user', or 'unknown'.
        Called when the user sends a correction trigger (e.g. 'your data is wrong').
        """
        try:
            with self._conn:
                self._conn.execute(
                    "UPDATE ai_decisions SET outcome = ? WHERE id = ?",
                    (outcome, decision_id),
                )
        except Exception:
            pass

    def get_recent(
        self,
        limit: int = 10,
        user_id: str | None = None,
        call_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Return the most recent N decisions, optionally filtered by user or call_type.

        Returns:
            list of dicts with all column values plus a human-readable 'ts_str' field.
        """
        conditions: list[str] = []
        params: list[Any]     = []

        if user_id:
            conditions.append("user_id = ?")
            params.append(str(user_id))
        if call_type:
            conditions.append("call_type = ?")
            params.append(call_type)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.append(limit)

        rows = self._conn.execute(
            f"""
            SELECT * FROM ai_decisions
            {where}
            ORDER BY ts DESC
            LIMIT ?
            """,
            params,
        ).fetchall()

        result = []
        for row in rows:
            d = dict(row)
            d["ts_str"] = datetime.fromtimestamp(
                d["ts"], tz=timezone.utc
            ).strftime("%b %d %H:%M UTC")
            try:
                d["context_blocks"] = json.loads(d.get("context_blocks", "[]"))
            except Exception:
                d["context_blocks"] = []
            result.append(d)

        return result

    def model_stats(self, days: int = 7) -> list[dict[str, Any]]:
        """
        Return per-model call counts and average latency for the last N days.
        Used by /decisions to show which models are being used most.
        """
        cutoff = time.time() - (days * 86400)
        rows = self._conn.execute(
            """
            SELECT model_used,
                   COUNT(*)         AS calls,
                   AVG(latency_ms)  AS avg_latency_ms,
                   SUM(CASE WHEN correction_mode = 1 THEN 1 ELSE 0 END) AS corrections,
                   SUM(CASE WHEN outcome = 'corrected_by_user' THEN 1 ELSE 0 END) AS user_corrections
            FROM ai_decisions
            WHERE ts >= ?
            GROUP BY model_used
            ORDER BY calls DESC
            """,
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]

    def prompt_version_stats(self, days: int = 7) -> list[dict[str, Any]]:
        """Count calls per prompt version for the last N days."""
        cutoff = time.time() - (days * 86400)
        rows = self._conn.execute(
            """
            SELECT prompt_version,
                   COUNT(*)        AS calls,
                   AVG(latency_ms) AS avg_latency_ms
            FROM ai_decisions
            WHERE ts >= ?
            GROUP BY prompt_version
            ORDER BY calls DESC
            """,
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]

    def summary(self, days: int = 7) -> dict[str, Any]:
        """
        High-level summary for /decisions command display.
        Returns total calls, top models, correction rate, avg latency.
        """
        cutoff = time.time() - (days * 86400)
        row = self._conn.execute(
            """
            SELECT
                COUNT(*)                                                      AS total_calls,
                AVG(latency_ms)                                               AS avg_latency_ms,
                SUM(CASE WHEN correction_mode = 1 THEN 1 ELSE 0 END)         AS correction_calls,
                SUM(CASE WHEN outcome = 'corrected_by_user' THEN 1 ELSE 0 END) AS user_corrections,
                SUM(CASE WHEN regime_safe = 0     THEN 1 ELSE 0 END)         AS drift_calls
            FROM ai_decisions
            WHERE ts >= ?
            """,
            (cutoff,),
        ).fetchone()

        total = row["total_calls"] or 0
        return {
            "days":              days,
            "total_calls":       total,
            "avg_latency_ms":    round(row["avg_latency_ms"] or 0),
            "correction_calls":  row["correction_calls"] or 0,
            "correction_rate":   f"{(row['correction_calls'] or 0) / max(1, total):.1%}",
            "user_corrections":  row["user_corrections"] or 0,
            "drift_calls":       row["drift_calls"] or 0,
        }

    def log_prompt_change(
        self,
        prompt_name: str,
        old_version: str,
        new_version: str,
        notes: str = "",
        changed_by: str = "code",
    ) -> None:
        """Record a prompt version change for audit trail."""
        try:
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO prompt_changes
                        (ts, prompt_name, old_version, new_version, notes, changed_by)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (time.time(), prompt_name, old_version, new_version, notes, changed_by),
                )
        except Exception:
            pass

    def cleanup(self, retain_days: int = _RETAIN_DAYS) -> int:
        """Purge decisions older than retain_days. Returns rows deleted."""
        cutoff = time.time() - (retain_days * 86400)
        try:
            with self._conn:
                cur = self._conn.execute(
                    "DELETE FROM ai_decisions WHERE ts < ?", (cutoff,)
                )
            deleted = cur.rowcount
            if deleted:
                log.info("[DecisionLog] Purged %d old decisions (>%d days)", deleted, retain_days)
            return deleted
        except Exception:
            return 0

    def row_count(self) -> int:
        """Current row count in ai_decisions."""
        row = self._conn.execute("SELECT COUNT(*) FROM ai_decisions").fetchone()
        return row[0] if row else 0
