"""
Confidence Calibrator — Platt Scaling for EDGE signal confidence.
=================================================================

Problem: The raw `confidence` value from probability_node is an AI-generated
number (0.45–0.95) derived from catalyst quality/confidence scores. There is
zero guarantee it maps linearly to actual win rates.

Solution: Fit a logistic regression (Platt scaling) on resolved signals:
  X = [raw_confidence]
  y = 1 if outcome == WIN else 0

The trained intercept (β₀) and slope (β₁) are stored in ml_store.db so
they survive restarts. On each call:

  calibrated_win_prob = sigmoid(β₀ + β₁ × raw_confidence)

This calibrated probability is used:
  1. As the `confidence` field in ProbabilityOutput (replaces raw value)
  2. As an additional feature in the XGBoost shadow scorer

Minimum sample gate: 150 labeled signals required before calibration is
applied. Below this threshold the raw confidence passthrough is used.

Overfitting mitigation:
  - Single feature (can't overfit badly)
  - Temporal cross-validation (train on older data, test on recent)
  - Brier score threshold: calibration only activated if Brier < 0.25
  - Monthly re-training (not continuous) to prevent concept drift chasing
"""
from __future__ import annotations

import logging
import math
import time
from typing import Any

log = logging.getLogger(__name__)

_MIN_SAMPLES      = 150    # minimum labeled signals before calibration activates
_BRIER_THRESHOLD  = 0.25   # only apply calibration if Brier score < this
_TRAIN_SPLIT_FRAC = 0.75   # use first 75% for training, last 25% for validation


# ── Math helpers (no sklearn dependency needed for single-feature logistic) ──

def _sigmoid(x: float) -> float:
    """Numerically stable sigmoid."""
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    exp_x = math.exp(x)
    return exp_x / (1.0 + exp_x)


def _brier_score(probs: list[float], labels: list[int]) -> float:
    """Mean squared error between predicted probabilities and binary labels."""
    if not probs:
        return 1.0
    return sum((p - y) ** 2 for p, y in zip(probs, labels)) / len(probs)


def _fit_logistic(X: list[float], y: list[int], lr: float = 0.1, epochs: int = 500) -> tuple[float, float]:
    """
    Fit a single-feature logistic regression via gradient descent.
    Returns (intercept, slope).

    Using pure Python so we have no sklearn dependency for this hot-path module.
    sklearn IS used in signal_scorer.py for XGBoost; here we keep it lean.
    """
    b0, b1 = 0.0, 0.0  # intercept, slope
    n = len(X)
    if n == 0:
        return b0, b1

    for _ in range(epochs):
        grad_b0 = 0.0
        grad_b1 = 0.0
        for xi, yi in zip(X, y):
            pred = _sigmoid(b0 + b1 * xi)
            err  = pred - yi
            grad_b0 += err
            grad_b1 += err * xi

        # L2 regularization on slope to prevent extreme values
        grad_b1 += 0.01 * b1

        b0 -= lr * grad_b0 / n
        b1 -= lr * grad_b1 / n

    return b0, b1


# ── Main class ───────────────────────────────────────────────────────────────

class ConfidenceCalibrator:
    """
    Platt-scaling calibrator for signal confidence.

    Usage:
        cal = ConfidenceCalibrator(ml_store)
        cal.load()                              # load latest params from DB (call once at startup)
        new_conf = cal.calibrate(raw_conf)      # returns calibrated value or raw passthrough
        cal.train(labeled_data)                 # retrain on latest outcome_tracker data
        cal.status()                            # dict for /mlstatus display
    """

    def __init__(self, ml_store: Any) -> None:
        self._store    = ml_store
        self._intercept: float | None = None
        self._slope:     float | None = None
        self._n_samples: int          = 0
        self._brier:     float | None = None
        self._trained_at: float | None = None
        self._active    = False

    def load(self) -> bool:
        """
        Load the most recent active calibration from ml_store.db.
        Returns True if a usable calibration was found, False otherwise.
        """
        row = self._store.get_active_calibration()
        if not row:
            log.info("[ConfidenceCalibrator] No calibration found — using raw passthrough.")
            return False

        n       = row["n_samples"]
        brier   = row.get("brier_score") or 1.0

        if n < _MIN_SAMPLES:
            log.info(
                "[ConfidenceCalibrator] Calibration has only %d samples (need %d) — passthrough.",
                n, _MIN_SAMPLES,
            )
            return False

        if brier > _BRIER_THRESHOLD:
            log.warning(
                "[ConfidenceCalibrator] Brier score %.4f > threshold %.4f — passthrough.",
                brier, _BRIER_THRESHOLD,
            )
            return False

        self._intercept  = row["intercept"]
        self._slope      = row["slope"]
        self._n_samples  = n
        self._brier      = brier
        self._trained_at = row["trained_at"]
        self._active     = True
        log.info(
            "[ConfidenceCalibrator] Loaded: n=%d β₀=%.4f β₁=%.4f Brier=%.4f",
            n, self._intercept, self._slope, brier,
        )
        return True

    def calibrate(self, raw_confidence: float) -> float:
        """
        Apply Platt scaling to convert raw confidence → calibrated win probability.
        Returns raw_confidence unchanged if calibrator is not yet active.

        Output range: always clamped to [0.45, 0.95] to stay compatible with the
        rest of the pipeline (qualification_gate uses min_confidence = 0.50).
        """
        if not self._active or self._intercept is None or self._slope is None:
            return raw_confidence  # safe passthrough

        calibrated = _sigmoid(self._intercept + self._slope * raw_confidence)
        return max(0.45, min(0.95, calibrated))

    def train(self, labeled_data: list[dict[str, Any]]) -> bool:
        """
        Retrain the calibrator on the latest labeled feature data.

        labeled_data: list of dicts with keys:
            raw_confidence (float), actual_outcome ("WIN" | "LOSS")

        Returns True if training succeeded and model was saved, False otherwise.
        """
        # Filter to WIN/LOSS only
        samples = [
            d for d in labeled_data
            if d.get("actual_outcome") in ("WIN", "LOSS")
        ]
        n = len(samples)

        if n < _MIN_SAMPLES:
            log.info(
                "[ConfidenceCalibrator] Only %d labeled samples (need %d) — skipping train.",
                n, _MIN_SAMPLES,
            )
            return False

        # Temporal split: train on first 75%, validate on last 25%
        split_idx   = int(n * _TRAIN_SPLIT_FRAC)
        train_set   = samples[:split_idx]
        val_set     = samples[split_idx:]

        X_train = [s["raw_confidence"] for s in train_set]
        y_train = [1 if s["actual_outcome"] == "WIN" else 0 for s in train_set]

        b0, b1 = _fit_logistic(X_train, y_train)

        # Validate on held-out set
        if val_set:
            X_val = [s["raw_confidence"] for s in val_set]
            y_val = [1 if s["actual_outcome"] == "WIN" else 0 for s in val_set]
            val_probs = [_sigmoid(b0 + b1 * x) for x in X_val]
            brier = _brier_score(val_probs, y_val)
        else:
            # Not enough data for val split — compute on training set (pessimistic)
            train_probs = [_sigmoid(b0 + b1 * x) for x in X_train]
            brier = _brier_score(train_probs, y_train)

        log.info(
            "[ConfidenceCalibrator] Training complete: n=%d (train=%d val=%d) "
            "β₀=%.4f β₁=%.4f Brier=%.4f",
            n, len(train_set), len(val_set), b0, b1, brier,
        )

        if brier > _BRIER_THRESHOLD:
            log.warning(
                "[ConfidenceCalibrator] Brier=%.4f > threshold=%.4f — model NOT activated "
                "(raw passthrough maintained). Check data quality.",
                brier, _BRIER_THRESHOLD,
            )
            run_id = self._store.save_calibration(
                n_samples=n, intercept=b0, slope=b1, brier_score=brier,
                notes=f"NOT ACTIVATED — Brier {brier:.4f} > threshold {_BRIER_THRESHOLD}",
            )
            # Deactivate — brier too high
            with self._store._conn:
                self._store._conn.execute(
                    "UPDATE calibration_state SET is_active = 0 WHERE id = ?", (run_id,)
                )
            self._active = False
            return False

        # Save and activate
        run_id = self._store.save_calibration(
            n_samples=n, intercept=b0, slope=b1, brier_score=brier,
            notes=f"train={len(train_set)} val={len(val_set)}",
        )
        self._store.save_training_snapshot(run_id, samples)

        self._intercept  = b0
        self._slope      = b1
        self._n_samples  = n
        self._brier      = brier
        self._trained_at = time.time()
        self._active     = True

        return True

    def status(self) -> dict[str, Any]:
        """Return a status dict for /mlstatus display."""
        return {
            "active":       self._active,
            "n_samples":    self._n_samples,
            "intercept":    round(self._intercept or 0.0, 4),
            "slope":        round(self._slope or 0.0, 4),
            "brier_score":  round(self._brier or 1.0, 4),
            "trained_at":   (
                __import__("datetime").datetime.fromtimestamp(
                    self._trained_at, tz=__import__("datetime").timezone.utc
                ).strftime("%Y-%m-%d %H:%M UTC")
                if self._trained_at else "never"
            ),
            "min_samples_needed": _MIN_SAMPLES,
        }
