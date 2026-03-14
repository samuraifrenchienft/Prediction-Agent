"""
ML Store — SQLite persistence for all machine-learning state.
=============================================================

Tables:
  ml_predictions     — shadow-mode XGBoost predictions per signal (never affects output)
  calibration_state  — trained logistic-regression intercept/slope snapshots
  training_snapshots — feature vectors + labels used for each training run
  regime_state       — rolling feature distribution stats for drift detection

DB lives at edge_agent/memory/data/ml_store.db
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_DB_PATH = Path(__file__).parent.parent / "memory" / "data" / "ml_store.db"


# ── DB bootstrap ──────────────────────────────────────────────────────────────

def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init_db(conn: sqlite3.Connection) -> None:
    # Shadow-mode XGBoost predictions per signal
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ml_predictions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id       INTEGER NOT NULL,          -- FK → scan_signals.id
            market_id       TEXT    NOT NULL,
            venue           TEXT    NOT NULL,
            signal_type     TEXT    NOT NULL,
            -- Raw input features (logged for training)
            raw_confidence  REAL    NOT NULL,
            ev_net          REAL    NOT NULL,
            market_prob     REAL    NOT NULL,
            depth_usd       REAL    NOT NULL DEFAULT 0,
            spread_bps      REAL    NOT NULL DEFAULT 0,
            ttr_hours       REAL    NOT NULL DEFAULT 0,
            catalyst_strength REAL  NOT NULL DEFAULT 0,
            smart_money_score REAL  NOT NULL DEFAULT 0,  -- from TraderFeatureExtractor
            n_hot_longs     INTEGER NOT NULL DEFAULT 0,
            n_hot_shorts    INTEGER NOT NULL DEFAULT 0,
            -- Shadow prediction (doesn't affect live output)
            xgb_win_prob    REAL,                      -- NULL if model not yet trained
            calibrated_conf REAL,                      -- NULL if calibrator not ready
            model_version   TEXT    NOT NULL DEFAULT 'shadow_v0',
            -- Outcome (filled in later by resolution job)
            actual_outcome  TEXT,                      -- WIN | LOSS | VOID | NULL(pending)
            ts              REAL    NOT NULL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_mlp_signal ON ml_predictions(signal_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_mlp_pending ON ml_predictions(actual_outcome) "
        "WHERE actual_outcome IS NULL"
    )

    # Logistic regression calibration snapshots
    conn.execute("""
        CREATE TABLE IF NOT EXISTS calibration_state (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            trained_at      REAL    NOT NULL,
            n_samples       INTEGER NOT NULL,
            intercept       REAL    NOT NULL,          -- β₀
            slope           REAL    NOT NULL,           -- β₁ (confidence feature)
            brier_score     REAL,                      -- calibration quality metric
            is_active       INTEGER NOT NULL DEFAULT 1, -- only newest row has is_active=1
            notes           TEXT    NOT NULL DEFAULT ''
        )
    """)

    # Training snapshots — feature vectors with labels for auditability
    conn.execute("""
        CREATE TABLE IF NOT EXISTS training_snapshots (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id          INTEGER NOT NULL,          -- calibration_state.id
            signal_id       INTEGER NOT NULL,
            confidence      REAL    NOT NULL,
            ev_net          REAL    NOT NULL,
            signal_type     TEXT    NOT NULL,
            market_prob     REAL    NOT NULL,
            smart_money_score REAL  NOT NULL DEFAULT 0,
            label           INTEGER NOT NULL,          -- 1=WIN 0=LOSS
            ts              REAL    NOT NULL
        )
    """)

    # Regime / feature-drift state
    conn.execute("""
        CREATE TABLE IF NOT EXISTS regime_state (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            computed_at     REAL    NOT NULL,
            window_days     INTEGER NOT NULL DEFAULT 14,
            n_signals       INTEGER NOT NULL,
            -- Rolling feature means (baseline from training set)
            mean_confidence REAL    NOT NULL,
            mean_ev_net     REAL    NOT NULL,
            mean_market_prob REAL   NOT NULL,
            mean_smart_money REAL   NOT NULL DEFAULT 0,
            -- Drift flags (1 = drift detected)
            conf_drift      INTEGER NOT NULL DEFAULT 0,
            ev_drift        INTEGER NOT NULL DEFAULT 0,
            prob_drift      INTEGER NOT NULL DEFAULT 0,
            ml_disabled     INTEGER NOT NULL DEFAULT 0  -- 1 = fell back to rule-based
        )
    """)

    conn.commit()


# ── Public class ──────────────────────────────────────────────────────────────

class MLStore:
    """Thread-safe SQLite store for all ML pipeline state."""

    def __init__(self) -> None:
        self._conn = _connect()
        _init_db(self._conn)

    # ── Predictions ──────────────────────────────────────────────────────────

    def log_prediction(
        self,
        signal_id: int,
        market_id: str,
        venue: str,
        signal_type: str,
        raw_confidence: float,
        ev_net: float,
        market_prob: float,
        depth_usd: float = 0.0,
        spread_bps: float = 0.0,
        ttr_hours: float = 0.0,
        catalyst_strength: float = 0.0,
        smart_money_score: float = 0.0,
        n_hot_longs: int = 0,
        n_hot_shorts: int = 0,
        xgb_win_prob: float | None = None,
        calibrated_conf: float | None = None,
        model_version: str = "shadow_v0",
    ) -> int:
        """Insert a shadow-mode prediction row. Returns the new row id."""
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO ml_predictions
                    (signal_id, market_id, venue, signal_type,
                     raw_confidence, ev_net, market_prob, depth_usd, spread_bps,
                     ttr_hours, catalyst_strength, smart_money_score,
                     n_hot_longs, n_hot_shorts,
                     xgb_win_prob, calibrated_conf, model_version, ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal_id, market_id, venue, signal_type,
                    raw_confidence, ev_net, market_prob, depth_usd, spread_bps,
                    ttr_hours, catalyst_strength, smart_money_score,
                    n_hot_longs, n_hot_shorts,
                    xgb_win_prob, calibrated_conf, model_version, time.time(),
                ),
            )
        return cur.lastrowid

    def update_prediction_outcome(self, signal_id: int, outcome: str) -> None:
        """Propagate resolved outcome (WIN/LOSS/VOID) to shadow predictions."""
        with self._conn:
            self._conn.execute(
                "UPDATE ml_predictions SET actual_outcome = ? WHERE signal_id = ?",
                (outcome.upper(), signal_id),
            )

    def get_labeled_features(
        self, min_samples: int = 0, days: int = 180
    ) -> list[dict[str, Any]]:
        """
        Return feature vectors with WIN/LOSS labels for model training.
        Only returns rows where actual_outcome is WIN or LOSS (not VOID/pending).
        """
        cutoff = time.time() - (days * 86400)
        rows = self._conn.execute(
            """
            SELECT raw_confidence, ev_net, market_prob, depth_usd, spread_bps,
                   ttr_hours, catalyst_strength, smart_money_score,
                   n_hot_longs, n_hot_shorts, signal_type, actual_outcome, ts
            FROM   ml_predictions
            WHERE  actual_outcome IN ('WIN', 'LOSS')
              AND  ts >= ?
            ORDER  BY ts ASC
            """,
            (cutoff,),
        ).fetchall()
        result = [dict(r) for r in rows]
        if len(result) < min_samples:
            return []
        return result

    def prediction_counts(self) -> dict[str, int]:
        """Quick summary for /mlstatus."""
        row = self._conn.execute(
            """
            SELECT
                COUNT(*)                                                 AS total,
                SUM(CASE WHEN actual_outcome = 'WIN'  THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN actual_outcome = 'LOSS' THEN 1 ELSE 0 END) AS losses,
                SUM(CASE WHEN actual_outcome IS NULL   THEN 1 ELSE 0 END) AS pending
            FROM ml_predictions
            """
        ).fetchone()
        return dict(row) if row else {"total": 0, "wins": 0, "losses": 0, "pending": 0}

    # ── Calibration ──────────────────────────────────────────────────────────

    def save_calibration(
        self,
        n_samples: int,
        intercept: float,
        slope: float,
        brier_score: float | None = None,
        notes: str = "",
    ) -> int:
        """Save a new calibration snapshot and deactivate old ones."""
        now = time.time()
        with self._conn:
            self._conn.execute(
                "UPDATE calibration_state SET is_active = 0"
            )
            cur = self._conn.execute(
                """
                INSERT INTO calibration_state
                    (trained_at, n_samples, intercept, slope, brier_score, is_active, notes)
                VALUES (?, ?, ?, ?, ?, 1, ?)
                """,
                (now, n_samples, intercept, slope, brier_score, notes),
            )
        log.info(
            "[MLStore] Calibration saved: n=%d intercept=%.4f slope=%.4f brier=%.4f",
            n_samples, intercept, slope, brier_score or 0,
        )
        return cur.lastrowid

    def get_active_calibration(self) -> dict[str, Any] | None:
        """Return the most recent active calibration parameters."""
        row = self._conn.execute(
            "SELECT * FROM calibration_state WHERE is_active = 1 ORDER BY trained_at DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def save_training_snapshot(self, run_id: int, samples: list[dict]) -> None:
        """Persist the training feature vectors used in a calibration run."""
        now = time.time()
        with self._conn:
            for s in samples:
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO training_snapshots
                        (run_id, signal_id, confidence, ev_net, signal_type,
                         market_prob, smart_money_score, label, ts)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        s.get("signal_id", 0),
                        s.get("raw_confidence", 0),
                        s.get("ev_net", 0),
                        s.get("signal_type", "UNKNOWN"),
                        s.get("market_prob", 0),
                        s.get("smart_money_score", 0),
                        1 if s.get("actual_outcome") == "WIN" else 0,
                        now,
                    ),
                )

    # ── Regime ───────────────────────────────────────────────────────────────

    def save_regime_snapshot(
        self,
        n_signals: int,
        mean_confidence: float,
        mean_ev_net: float,
        mean_market_prob: float,
        mean_smart_money: float = 0.0,
        conf_drift: bool = False,
        ev_drift: bool = False,
        prob_drift: bool = False,
        ml_disabled: bool = False,
        window_days: int = 14,
    ) -> None:
        """Persist a regime snapshot after each drift check."""
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO regime_state
                    (computed_at, window_days, n_signals,
                     mean_confidence, mean_ev_net, mean_market_prob, mean_smart_money,
                     conf_drift, ev_drift, prob_drift, ml_disabled)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    time.time(), window_days, n_signals,
                    mean_confidence, mean_ev_net, mean_market_prob, mean_smart_money,
                    int(conf_drift), int(ev_drift), int(prob_drift), int(ml_disabled),
                ),
            )

    def get_latest_regime(self) -> dict[str, Any] | None:
        """Return the most recent regime snapshot."""
        row = self._conn.execute(
            "SELECT * FROM regime_state ORDER BY computed_at DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def cleanup(self, max_age_days: int = 180) -> int:
        """Purge old ml_predictions and training_snapshots rows."""
        cutoff = time.time() - (max_age_days * 86400)
        with self._conn:
            r1 = self._conn.execute(
                "DELETE FROM ml_predictions WHERE ts < ? AND actual_outcome IS NOT NULL",
                (cutoff,),
            )
            r2 = self._conn.execute(
                "DELETE FROM training_snapshots WHERE ts < ?", (cutoff,)
            )
        total = r1.rowcount + r2.rowcount
        if total:
            log.info("[MLStore] Cleanup: removed %d old prediction + snapshot rows", total)
        return total
