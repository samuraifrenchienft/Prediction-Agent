"""
Signal Scorer — XGBoost Shadow-Mode Win Probability Estimator.
==============================================================

Phase 1 (now, < 400 labeled signals): SHADOW ONLY.
  Computes P(WIN|features) for every QUALIFIED/WATCHLIST signal.
  Logs predictions to ml_store.ml_predictions.
  Output NEVER affects recommendation or qualification state.

Phase 2 (≥ 400 labeled signals): SOFT GATE.
  Can promote WATCHLIST → QUALIFIED if P(WIN) > _PROMOTE_THRESHOLD.
  Can flag QUALIFIED for human review if P(WIN) < _DEMOTE_THRESHOLD.
  Hard safety rules (neg EV, depth < min, TTR < min) are NEVER overridden.

Phase 3 (≥ 800 signals): Full integration.
  XGBoost score replaces binary qualification gate with soft score.
  Still constrained by hard safety floors.

Overfitting safeguards:
  - max_depth=3 (shallow trees only)
  - min_child_weight=10 (no splits on fewer than 10 samples)
  - subsample=0.8, colsample_bytree=0.8 (randomisation)
  - Temporal cross-validation only (no random splits)
  - Model version pinned per training run; old predictions not retroactively updated

Dependencies: xgboost, scikit-learn (both in requirements.txt)
"""
from __future__ import annotations

import json
import logging
import pickle
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Minimum labeled samples before model trains/deploys
_MIN_TRAIN_SAMPLES  = 400
_PROMOTE_THRESHOLD  = 0.58   # P(WIN) > this → promote WATCHLIST → QUALIFIED
_DEMOTE_THRESHOLD   = 0.42   # P(WIN) < this → flag QUALIFIED for review
_MODEL_PATH         = Path(__file__).parent.parent / "memory" / "data" / "xgb_signal_model.pkl"

# XGBoost hyperparameters (conservative to prevent overfitting on small datasets)
_XGB_PARAMS = {
    "n_estimators":      100,
    "max_depth":         3,        # shallow — prevents overfitting
    "min_child_weight":  10,       # no splits on < 10 samples
    "subsample":         0.8,      # row sampling per tree
    "colsample_bytree":  0.8,      # feature sampling per tree
    "learning_rate":     0.05,     # slow learning rate → less overfit
    "objective":         "binary:logistic",
    "eval_metric":       "logloss",
    "use_label_encoder": False,
    "random_state":      42,
}

# Feature names (must match exactly what log_prediction() stores)
_FEATURES = [
    "raw_confidence",
    "ev_net",
    "market_prob",
    "depth_usd",
    "spread_bps",
    "ttr_hours",
    "catalyst_strength",
    "smart_money_score",
    "n_hot_longs",
    "n_hot_shorts",
    "signal_type_encoded",  # label-encoded from signal_type string
]

_SIGNAL_TYPE_MAP = {
    "PRE_GAME_INJURY_LAG":      0,
    "INJURY_MOMENTUM_REVERSAL": 1,
    "NEWS_LAG":                 2,
    "FAVORITE_LONGSHOT_BIAS":   3,
    "NONE":                     4,
    "UNKNOWN":                  4,
}


def _encode_signal_type(signal_type: str) -> int:
    return _SIGNAL_TYPE_MAP.get(signal_type.upper(), 4)


def _to_feature_vector(row: dict[str, Any]) -> list[float]:
    """
    Convert a feature dict to a numeric vector for XGBoost.

    Missing probability features use float('nan') so XGBoost's native
    missing-value handling kicks in (treats NaN as "missing" and learns
    optimal split directions automatically). This is better than a sentinel
    like -1.0 which the tree would treat as a real value, or 0.5 which
    biases toward mid-probability.
    """
    import math
    _nan = float("nan")
    return [
        float(row["raw_confidence"]) if row.get("raw_confidence") is not None else _nan,
        float(row.get("ev_net", 0.0)),
        float(row["market_prob"]) if row.get("market_prob") is not None else _nan,
        float(row.get("depth_usd", 0.0)),
        float(row.get("spread_bps", 0.0)),
        float(row.get("ttr_hours", 0.0)),
        float(row.get("catalyst_strength", 0.0)),
        float(row.get("smart_money_score", 0.0)),
        float(row.get("n_hot_longs", 0)),
        float(row.get("n_hot_shorts", 0)),
        float(_encode_signal_type(row.get("signal_type", "UNKNOWN"))),
    ]


class SignalScorer:
    """
    XGBoost-based shadow win-probability scorer.

    Usage:
        scorer = SignalScorer()
        scorer.load()                               # load model from disk if trained
        prob = scorer.predict(features_dict)        # returns float or None
        did_train = scorer.train(labeled_data)      # retrain on new labeled data
        can_promote = scorer.should_promote(prob)   # Phase 2 gate
        status = scorer.status()                    # /mlstatus display
    """

    def __init__(self) -> None:
        self._model = None
        self._version: str = "untrained"
        self._n_samples: int = 0
        self._val_logloss: float | None = None
        self._val_accuracy: float | None = None
        self._trained_at: float | None = None
        self._phase: int = 1  # 1=shadow 2=soft-gate 3=full

    def load(self) -> bool:
        """Load a previously-trained model from disk. Returns True if successful."""
        if not _MODEL_PATH.exists():
            log.info("[SignalScorer] No saved model found — shadow mode (no predictions).")
            return False
        try:
            with open(_MODEL_PATH, "rb") as f:
                data = pickle.load(f)
            self._model       = data["model"]
            self._version     = data.get("version", "unknown")
            self._n_samples   = data.get("n_samples", 0)
            self._val_logloss = data.get("val_logloss")
            self._val_accuracy = data.get("val_accuracy")
            self._trained_at  = data.get("trained_at")
            self._phase       = self._compute_phase(self._n_samples)
            log.info(
                "[SignalScorer] Loaded model v%s (n=%d, val_acc=%.3f, phase=%d)",
                self._version, self._n_samples,
                self._val_accuracy or 0, self._phase,
            )
            return True
        except Exception as exc:
            log.warning("[SignalScorer] Failed to load model: %s", exc)
            return False

    def _compute_phase(self, n: int) -> int:
        if n >= 800:
            return 3
        if n >= _MIN_TRAIN_SAMPLES:
            return 2
        return 1

    def predict(self, features: dict[str, Any]) -> float | None:
        """
        Predict P(WIN) for a signal given its feature dict.
        Returns None if model is not yet trained (Phase 1 shadow, no prediction).
        """
        if self._model is None:
            return None
        try:
            vec = [_to_feature_vector(features)]
            prob = float(self._model.predict_proba(vec)[0][1])
            return round(prob, 4)
        except Exception as exc:
            log.debug("[SignalScorer] predict() failed: %s", exc)
            return None

    def should_promote(self, xgb_prob: float | None) -> bool:
        """
        Phase 2: can this WATCHLIST signal be promoted to QUALIFIED?
        Only active in Phase 2+ and only if hard safety gates are already satisfied.
        """
        if self._phase < 2 or xgb_prob is None:
            return False
        return xgb_prob >= _PROMOTE_THRESHOLD

    def should_flag_review(self, xgb_prob: float | None) -> bool:
        """
        Phase 2: should this QUALIFIED signal be flagged for review?
        Does NOT demote — only adds metadata for human inspection.
        """
        if self._phase < 2 or xgb_prob is None:
            return False
        return xgb_prob <= _DEMOTE_THRESHOLD

    def train(self, labeled_data: list[dict[str, Any]]) -> bool:
        """
        Train (or retrain) the XGBoost model on labeled signal data.

        labeled_data: list of dicts from ml_store.get_labeled_features()
          Required keys: raw_confidence, ev_net, market_prob, depth_usd,
                         spread_bps, ttr_hours, catalyst_strength, smart_money_score,
                         n_hot_longs, n_hot_shorts, signal_type, actual_outcome

        Uses temporal split (not random) to prevent data leakage.
        Returns True if model trained and saved successfully.
        """
        try:
            import xgboost as xgb
            from sklearn.model_selection import TimeSeriesSplit
            from sklearn.metrics import log_loss, accuracy_score
        except ImportError as exc:
            log.error("[SignalScorer] Missing dependency: %s — run: pip install xgboost scikit-learn", exc)
            return False

        # Filter to WIN/LOSS only
        samples = [d for d in labeled_data if d.get("actual_outcome") in ("WIN", "LOSS")]
        n = len(samples)

        if n < _MIN_TRAIN_SAMPLES:
            log.info(
                "[SignalScorer] Only %d labeled samples (need %d) — skipping train.",
                n, _MIN_TRAIN_SAMPLES,
            )
            return False

        # Build feature matrix and label vector (temporally ordered — data already sorted)
        X = [_to_feature_vector(s) for s in samples]
        y = [1 if s["actual_outcome"] == "WIN" else 0 for s in samples]

        # Temporal train/val split (last 20% = validation)
        split_idx = int(n * 0.80)
        X_train, X_val = X[:split_idx], X[split_idx:]
        y_train, y_val = y[:split_idx], y[split_idx:]

        if len(set(y_train)) < 2:
            log.warning("[SignalScorer] Training set is single-class — skipping.")
            return False

        # Train with early stopping on validation logloss
        model = xgb.XGBClassifier(**{k: v for k, v in _XGB_PARAMS.items() if k != "eval_metric"})
        try:
            model.fit(
                X_train, y_train,
                eval_set=[(X_val, y_val)],
                verbose=False,
            )
        except Exception as exc:
            log.error("[SignalScorer] XGBoost fit() failed: %s", exc)
            return False

        # Validate
        val_probs = model.predict_proba(X_val)[:, 1]
        val_preds = (val_probs >= 0.5).astype(int)
        val_logloss  = log_loss(y_val, val_probs)
        val_accuracy = accuracy_score(y_val, val_preds)

        log.info(
            "[SignalScorer] Training complete: n=%d train=%d val=%d "
            "logloss=%.4f accuracy=%.3f",
            n, len(X_train), len(X_val), val_logloss, val_accuracy,
        )

        # Save to disk
        version = str(int(time.time()))
        data = {
            "model":        model,
            "version":      version,
            "n_samples":    n,
            "val_logloss":  val_logloss,
            "val_accuracy": val_accuracy,
            "trained_at":   time.time(),
            "features":     _FEATURES,
        }
        _MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_MODEL_PATH, "wb") as f:
            pickle.dump(data, f)

        self._model        = model
        self._version      = version
        self._n_samples    = n
        self._val_logloss  = val_logloss
        self._val_accuracy = val_accuracy
        self._trained_at   = time.time()
        self._phase        = self._compute_phase(n)

        return True

    def feature_importance(self) -> dict[str, float] | None:
        """Return feature importances dict (feature_name → importance score)."""
        if self._model is None:
            return None
        try:
            imp = self._model.feature_importances_
            return {name: round(float(score), 4) for name, score in zip(_FEATURES, imp)}
        except Exception:
            return None

    def status(self) -> dict[str, Any]:
        """Return status dict for /mlstatus display."""
        return {
            "phase":         self._phase,
            "phase_label":   {1: "shadow", 2: "soft-gate", 3: "full-integration"}.get(self._phase, "shadow"),
            "model_version": self._version,
            "n_samples":     self._n_samples,
            "val_logloss":   round(self._val_logloss or 0.0, 4),
            "val_accuracy":  round(self._val_accuracy or 0.0, 3),
            "trained_at":    (
                __import__("datetime").datetime.fromtimestamp(
                    self._trained_at, tz=__import__("datetime").timezone.utc
                ).strftime("%Y-%m-%d %H:%M UTC")
                if self._trained_at else "never"
            ),
            "promote_threshold": _PROMOTE_THRESHOLD,
            "demote_threshold":  _DEMOTE_THRESHOLD,
            "min_train_samples": _MIN_TRAIN_SAMPLES,
            "feature_importance": self.feature_importance(),
        }
