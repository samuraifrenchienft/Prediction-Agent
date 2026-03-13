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
_DEFAULT_STAKE = 10.0          # paper trade default stake $10
_MAX_CHECK_AGE = 86400 * 60    # stop retrying after 60 days
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
                        (signal_id, market_id, venue, target_side, entry_prob, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (signal_id, market_id, venue.upper(), target_side.upper(),
                     entry_prob, time.time()),
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
        Check all PENDING signals against their respective APIs.
        Updates outcomes and propagates to user_picks.
        Returns counts: {resolved, still_pending, errors}.
        """
        cutoff = time.time() - _MAX_CHECK_AGE
        rows = self._conn.execute(
            """
            SELECT * FROM signal_outcomes
            WHERE outcome = 'PENDING' AND created_at > ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (cutoff, limit),
        ).fetchall()

        resolved = 0
        still_pending = 0
        errors = 0

        for row in rows:
            row = dict(row)
            venue     = row["venue"]
            market_id = row["market_id"]
            signal_id = row["signal_id"]

            resolution = None
            if venue == "POLYMARKET":
                resolution = _resolve_polymarket(market_id)
            elif venue == "KALSHI":
                resolution = _resolve_kalshi(market_id)

            # Increment check counter regardless
            with self._conn:
                self._conn.execute(
                    "UPDATE signal_outcomes SET check_count = check_count + 1 WHERE signal_id = ?",
                    (signal_id,),
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

        return {"resolved": resolved, "still_pending": still_pending, "errors": errors}

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
