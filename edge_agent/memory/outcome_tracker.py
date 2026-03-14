"""
Outcome Tracker
===============
Tracks whether EDGE's scan signals resolved correctly, and stores
user paper-trade picks so we can compute EDGE accuracy and user P&L.

Tables (in outcome_tracker.db):
  signal_outcomes  — one row per logged signal; updated when market resolves
  user_picks       — user paper-trade choices (YES/NO per signal)

Resolution logic:
  Polymarket → Gamma API  (GET /markets?conditionIds=…)
  Kalshi     → REST API   (GET /markets/{ticker})

Outcome values: PENDING | WIN | LOSS | VOID
  WIN  = EDGE recommended side turned out to be the correct resolution
  LOSS = EDGE recommended side was wrong
  VOID = market voided / N/A

Paper P&L uses a fixed $10 default stake.
  WIN:  stake * (1 / entry_prob - 1)   ← profit if you bought at entry_prob
  LOSS: -stake
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger(__name__)

_DB_PATH      = Path(__file__).parent / "data" / "outcome_tracker.db"
_GAMMA_API    = "https://gamma-api.polymarket.com"
_KALSHI_API   = "https://api.elections.kalshi.com/trade-api/v2"
_DEFAULT_STAKE    = 10.0          # paper trade default stake $10
_MAX_CHECK_AGE    = 86400 * 60    # stop retrying after 60 days
_MAX_CHECK_COUNT  = 48            # mark UNRESOLVABLE after this many consecutive API failures
# Exponential back-off: minimum seconds to wait between resolution checks
# check_count → min gap:  0→0s  1→5m  2→10m  4→20m  8→40m  16→80m  24→3h  32→6h  48→12h
_BACKOFF_BASE_SECS = 300          # 5 minutes base
_SESS = requests.Session()
_SESS.headers.update({"User-Agent": "edge-agent/1.0"})


# ── DB setup ──────────────────────────────────────────────────────────────────

def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS signal_outcomes (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id    INTEGER NOT NULL UNIQUE,   -- scan_log scan_signals.id
            market_id    TEXT    NOT NULL,
            venue        TEXT    NOT NULL,          -- KALSHI | POLYMARKET
            target_side  TEXT    NOT NULL,          -- YES | NO (EDGE recommendation)
            entry_prob   REAL    NOT NULL,          -- market prob at signal time
            question     TEXT,                      -- human-readable market title
            outcome      TEXT    NOT NULL DEFAULT 'PENDING',
            resolved_at  REAL,
            check_count  INTEGER NOT NULL DEFAULT 0,
            created_at   REAL    NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_picks (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id    INTEGER NOT NULL,          -- FK → signal_outcomes.signal_id
            market_id    TEXT    NOT NULL,
            user_id      INTEGER NOT NULL,
            side         TEXT    NOT NULL,          -- YES | NO
            paper_stake  REAL    NOT NULL DEFAULT 10.0,
            outcome      TEXT    NOT NULL DEFAULT 'PENDING',
            paper_pnl    REAL,
            ts           REAL    NOT NULL,
            resolved_at  REAL
        )
    """)
    # Migrate: add question column if missing (safe on existing DBs)
    try:
        conn.execute("ALTER TABLE signal_outcomes ADD COLUMN question TEXT")
    except Exception:
        pass
    conn.execute("CREATE INDEX IF NOT EXISTS idx_so_pending   ON signal_outcomes(outcome) WHERE outcome = 'PENDING'")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_so_market    ON signal_outcomes(market_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_up_signal    ON user_picks(signal_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_up_user      ON user_picks(user_id)")
    conn.commit()


# ── Resolution helpers ─────────────────────────────────────────────────────────

def _resolve_polymarket(market_id: str) -> str | None:
    """
    Query Gamma API for market resolution.
    Returns 'YES', 'NO', 'VOID', or None (still open / API error).
    market_id is the conditionId (0x…) or a Gamma numeric id.
    """
    try:
        # Try conditionId lookup first
        r = _SESS.get(
            f"{_GAMMA_API}/markets",
            params={"conditionIds": market_id},
            timeout=8,
        )
        r.raise_for_status()
        data = r.json()
        market = data[0] if isinstance(data, list) and data else None

        # Fallback: numeric id lookup
        if not market:
            r2 = _SESS.get(f"{_GAMMA_API}/markets/{market_id}", timeout=8)
            r2.raise_for_status()
            market = r2.json()

        if not market:
            return None

        if not market.get("closed") or not market.get("resolved"):
            return None  # still open

        # outcomePrices is a JSON-encoded list like '[1, 0]' or '["1", "0"]'
        raw_prices = market.get("outcomePrices", "[]")
        prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
        prices = [float(p) for p in prices]

        # outcomes is a JSON-encoded list like '["Yes","No"]'
        raw_outcomes = market.get("outcomes", '["Yes","No"]')
        outcomes = json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else raw_outcomes

        if not prices or not outcomes:
            return "VOID"

        # Find which outcome paid $1
        for i, price in enumerate(prices):
            if price >= 0.99:
                label = str(outcomes[i]).upper()
                if "YES" in label or label == "Y":
                    return "YES"
                if "NO" in label or label == "N":
                    return "NO"

        return "VOID"
    except Exception as exc:
        log.debug("Polymarket resolution check failed for %s: %s", market_id, exc)
        return None


def _resolve_kalshi(market_id: str) -> str | None:
    """
    Query Kalshi REST API for market resolution.
    Returns 'YES', 'NO', 'VOID', or None.
    """
    try:
        r = _SESS.get(
            f"{_KALSHI_API}/markets/{market_id}",
            timeout=8,
        )
        r.raise_for_status()
        data = r.json()
        market = data.get("market", data)

        status = market.get("status", "").lower()
        if status not in ("finalized", "settled", "closed"):
            return None  # still open

        result = market.get("result", "").lower()
        if result in ("yes", "y"):
            return "YES"
        if result in ("no", "n"):
            return "NO"
        return "VOID"
    except Exception as exc:
        log.debug("Kalshi resolution check failed for %s: %s", market_id, exc)
        return None


def _compute_paper_pnl(side: str, target_side: str, entry_prob: float, stake: float) -> float:
    """
    Compute paper P&L for a user pick.
      side        = what the user picked (YES/NO)
      target_side = what actually resolved (YES/NO)
      entry_prob  = market prob at the time EDGE signalled
      stake       = paper stake in USD
    """
    won = (side.upper() == target_side.upper())
    if won:
        # Profit = stake * (1/prob - 1)  — buying at entry_prob, payout $1/share
        return round(stake * (1.0 / max(entry_prob, 0.01) - 1.0), 2)
    else:
        return round(-stake, 2)


# ── Public API ─────────────────────────────────────────────────────────────────

class OutcomeTracker:
    def __init__(self) -> None:
        self._conn = _connect()
        _init_db(self._conn)

    # ── Writes ────────────────────────────────────────────────────────────────

    def register_signal(
        self,
        signal_id: int,
        market_id: str,
        venue: str,
        target_side: str,
        entry_prob: float,
        question: str | None = None,
    ) -> None:
        """
        Register a scan signal for outcome tracking.
        Called immediately after a signal is logged to scan_log.
        """
        try:
            with self._conn:
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO signal_outcomes
                        (signal_id, market_id, venue, target_side, entry_prob, question, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (signal_id, market_id, venue.upper(), target_side.upper(),
                     entry_prob, question, time.time()),
                )
        except Exception as exc:
            log.warning("register_signal failed: %s", exc)

    def record_user_pick(
        self,
        signal_id: int,
        market_id: str,
        user_id: int,
        side: str,
        stake: float = _DEFAULT_STAKE,
    ) -> bool:
        """
        Store a user's paper-trade pick. Returns False if user already picked.
        """
        try:
            with self._conn:
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO user_picks
                        (signal_id, market_id, user_id, side, paper_stake, ts)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (signal_id, market_id, user_id, side.upper(), stake, time.time()),
                )
            return True
        except Exception as exc:
            log.warning("record_user_pick failed: %s", exc)
            return False

    # ── Resolution job ────────────────────────────────────────────────────────

    def resolve_pending(self, limit: int = 50) -> dict[str, int]:
        """
        Check PENDING signals against their respective APIs using exponential back-off.

        Back-off logic: each consecutive API failure doubles the minimum wait gap
        (capped at ~12h).  Only signals whose last check was older than the computed
        gap are re-queried this cycle.  After _MAX_CHECK_COUNT failures the signal
        is marked UNRESOLVABLE so it never blocks the queue again.

        Returns counts: {resolved, still_pending, skipped_backoff, unresolvable, errors}.
        """
        now    = time.time()
        cutoff = now - _MAX_CHECK_AGE

        rows = self._conn.execute(
            """
            SELECT * FROM signal_outcomes
            WHERE outcome = 'PENDING' AND created_at > ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (cutoff, limit),
        ).fetchall()

        resolved        = 0
        still_pending   = 0
        skipped_backoff = 0
        unresolvable    = 0
        errors          = 0

        for row in rows:
            row = dict(row)
            venue       = row["venue"]
            market_id   = row["market_id"]
            signal_id   = row["signal_id"]
            check_count = row.get("check_count", 0)
            resolved_at = row.get("resolved_at")  # used as "last_checked_at" for pending

            # ── Back-off gate ─────────────────────────────────────────────
            # Exponential gap: min_gap = base * 2^(check_count/4), capped at 12h
            min_gap_secs = min(
                _BACKOFF_BASE_SECS * (2 ** (check_count / 4)),
                43200,  # 12h cap
            )
            last_check = resolved_at or row["created_at"]
            if (now - last_check) < min_gap_secs:
                skipped_backoff += 1
                continue

            # ── Too many failures → mark UNRESOLVABLE ────────────────────
            if check_count >= _MAX_CHECK_COUNT:
                with self._conn:
                    self._conn.execute(
                        "UPDATE signal_outcomes SET outcome = 'VOID', resolved_at = ? "
                        "WHERE signal_id = ?",
                        (now, signal_id),
                    )
                log.warning(
                    "[OutcomeTracker] Signal %s marked VOID after %d failed resolution attempts",
                    signal_id,
                    check_count,
                )
                unresolvable += 1
                continue

            resolution = None
            if venue == "POLYMARKET":
                resolution = _resolve_polymarket(market_id)
            elif venue == "KALSHI":
                resolution = _resolve_kalshi(market_id)

            # Increment check counter and record last-check timestamp
            with self._conn:
                self._conn.execute(
                    "UPDATE signal_outcomes SET check_count = check_count + 1, "
                    "resolved_at = ? WHERE signal_id = ?",
                    (now, signal_id),
                )

            if resolution is None:
                still_pending += 1
                continue

            if resolution not in ("YES", "NO", "VOID"):
                errors += 1
                continue

            # Determine EDGE outcome
            target_side  = row["target_side"]
            edge_outcome = (
                "VOID" if resolution == "VOID"
                else ("WIN" if resolution == target_side else "LOSS")
            )

            now = time.time()
            with self._conn:
                self._conn.execute(
                    """
                    UPDATE signal_outcomes
                    SET outcome = ?, resolved_at = ?
                    WHERE signal_id = ?
                    """,
                    (edge_outcome, now, signal_id),
                )

                # Propagate resolution to user_picks
                picks = self._conn.execute(
                    "SELECT * FROM user_picks WHERE signal_id = ? AND outcome = 'PENDING'",
                    (signal_id,),
                ).fetchall()

                for pick in picks:
                    pick = dict(pick)
                    if resolution == "VOID":
                        user_outcome = "VOID"
                        pnl = 0.0
                    else:
                        user_won = (pick["side"].upper() == resolution.upper())
                        user_outcome = "WIN" if user_won else "LOSS"
                        pnl = _compute_paper_pnl(
                            pick["side"],
                            resolution,
                            row["entry_prob"],
                            pick["paper_stake"],
                        )

                    self._conn.execute(
                        """
                        UPDATE user_picks
                        SET outcome = ?, paper_pnl = ?, resolved_at = ?
                        WHERE id = ?
                        """,
                        (user_outcome, pnl, now, pick["id"]),
                    )

            resolved += 1
            log.info(
                "Signal %s resolved: %s → EDGE %s (market %s)",
                signal_id, resolution, edge_outcome, market_id[:20],
            )

        return {
            "resolved":        resolved,
            "still_pending":   still_pending,
            "skipped_backoff": skipped_backoff,
            "unresolvable":    unresolvable,
            "errors":          errors,
        }

    # ── Stats ─────────────────────────────────────────────────────────────────

    def edge_accuracy(self, days: int = 30) -> dict[str, Any]:
        """EDGE win/loss stats for the last N days."""
        cutoff = time.time() - (days * 86400)
        row = self._conn.execute(
            """
            SELECT
                COUNT(*)                                        AS total,
                SUM(CASE WHEN outcome = 'WIN'  THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN outcome = 'LOSS' THEN 1 ELSE 0 END) AS losses,
                SUM(CASE WHEN outcome = 'VOID' THEN 1 ELSE 0 END) AS voids,
                SUM(CASE WHEN outcome = 'PENDING' THEN 1 ELSE 0 END) AS pending
            FROM signal_outcomes
            WHERE created_at >= ?
            """,
            (cutoff,),
        ).fetchone()
        row = dict(row)
        settled = (row["wins"] or 0) + (row["losses"] or 0)
        row["win_rate"] = round(row["wins"] / settled, 4) if settled else None
        row["settled"]  = settled
        row["days"]     = days
        return row

    def user_pnl(self, user_id: int, days: int = 30) -> dict[str, Any]:
        """Paper P&L summary for a Telegram user."""
        cutoff = time.time() - (days * 86400)
        row = self._conn.execute(
            """
            SELECT
                COUNT(*)                                              AS total_picks,
                SUM(CASE WHEN outcome = 'WIN'  THEN 1 ELSE 0 END)    AS wins,
                SUM(CASE WHEN outcome = 'LOSS' THEN 1 ELSE 0 END)    AS losses,
                SUM(CASE WHEN outcome = 'VOID' THEN 1 ELSE 0 END)    AS voids,
                SUM(CASE WHEN outcome = 'PENDING' THEN 1 ELSE 0 END) AS pending,
                COALESCE(SUM(paper_pnl), 0)                          AS total_pnl,
                COALESCE(SUM(paper_stake), 0)                        AS total_staked
            FROM user_picks
            WHERE user_id = ? AND ts >= ?
            """,
            (user_id, cutoff),
        ).fetchone()
        row = dict(row)
        settled = (row["wins"] or 0) + (row["losses"] or 0)
        row["win_rate"] = round(row["wins"] / settled, 4) if settled else None
        row["settled"]  = settled
        row["days"]     = days
        row["roi"]      = (
            round(row["total_pnl"] / row["total_staked"], 4)
            if row["total_staked"] else None
        )
        return row

    def recent_resolved(self, days: int = 7, limit: int = 10) -> list[dict]:
        """Recently resolved signals with EDGE outcome."""
        cutoff = time.time() - (days * 86400)
        rows = self._conn.execute(
            """
            SELECT market_id, venue, target_side, entry_prob, outcome, resolved_at
            FROM signal_outcomes
            WHERE outcome != 'PENDING' AND resolved_at >= ?
            ORDER BY resolved_at DESC
            LIMIT ?
            """,
            (cutoff, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def cleanup(self, resolved_max_age_days: int = 180) -> dict[str, int]:
        """
        Purge old resolved signal_outcomes (and their user_picks) to prevent
        unbounded table growth.  PENDING signals are never deleted by cleanup.

        Default: keep resolved signals for 180 days (6 months).
        user_picks rows are cascade-deleted when their parent signal is deleted.
        """
        cutoff = time.time() - (resolved_max_age_days * 86400)

        with self._conn:
            # Find old resolved signal_ids first
            old_ids = [
                r[0]
                for r in self._conn.execute(
                    """
                    SELECT signal_id FROM signal_outcomes
                    WHERE outcome != 'PENDING' AND created_at < ?
                    """,
                    (cutoff,),
                ).fetchall()
            ]

            picks_deleted = 0
            sigs_deleted  = 0
            if old_ids:
                ph = ",".join("?" * len(old_ids))
                cur = self._conn.execute(
                    f"DELETE FROM user_picks WHERE signal_id IN ({ph})", old_ids
                )
                picks_deleted = cur.rowcount
                cur = self._conn.execute(
                    f"DELETE FROM signal_outcomes WHERE signal_id IN ({ph})", old_ids
                )
                sigs_deleted = cur.rowcount

        if sigs_deleted:
            log.info(
                "[OutcomeTracker] Cleanup: removed %d signals + %d picks older than %dd",
                sigs_deleted, picks_deleted, resolved_max_age_days,
            )

        return {"signals_deleted": sigs_deleted, "picks_deleted": picks_deleted}

    def get_user_picks(
        self,
        user_id: int,
        outcome_filter: str | None = None,   # 'PENDING' | 'WIN' | 'LOSS' | None = all
        limit: int = 50,
    ) -> list[dict]:
        """
        Return a user's paper picks joined with signal info (question, entry_prob, venue).
        Sorted: open picks first (newest), then settled (newest).
        outcome_filter=None returns all; 'PENDING' returns only open picks.
        """
        where = "up.user_id = ?"
        params: list = [user_id]

        if outcome_filter:
            where += " AND up.outcome = ?"
            params.append(outcome_filter)

        rows = self._conn.execute(
            f"""
            SELECT
                up.id           AS pick_id,
                up.signal_id,
                up.market_id,
                up.side,
                up.paper_stake,
                up.outcome      AS pick_outcome,
                up.paper_pnl,
                up.ts           AS picked_at,
                up.resolved_at  AS pick_resolved_at,
                so.venue,
                so.target_side  AS edge_side,
                so.entry_prob,
                so.question,
                so.outcome      AS signal_outcome
            FROM user_picks up
            LEFT JOIN signal_outcomes so ON up.signal_id = so.signal_id
            WHERE {where}
            ORDER BY
                CASE WHEN up.outcome = 'PENDING' THEN 0 ELSE 1 END ASC,
                up.ts DESC
            LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
        return [dict(r) for r in rows]
