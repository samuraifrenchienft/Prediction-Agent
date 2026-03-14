"""
Regime Detector — Feature Drift Monitor with Automatic ML Fallback.
====================================================================

Problem: ML models trained on historical signal data assume the feature
distribution is stationary. When market conditions change dramatically
(e.g., election season, crypto crash, sports offseason shift), the
current feature distribution can drift far from the training distribution,
making model predictions unreliable — potentially WORSE than the rule-based
fallback.

Solution: Monitor rolling feature means against training-set baselines.
If any key feature drifts by more than a configurable threshold, the ML
overlay is automatically disabled and the system falls back to pure
rule-based qualification. An alert is sent to Telegram.

Monitoring cadence: runs every 6 hours (via watchlist_vet_job or dedicated job).
Drift check: compares 14-day rolling means vs. the stored calibration baseline.

Recovery: drift flag is cleared automatically if distribution returns to normal
for 3 consecutive checks (42h with 6h cadence). No manual intervention needed.

Thresholds (conservative — designed to fire rarely, only on real regime shifts):
  confidence drift: |current_mean - baseline_mean| > 0.10
  ev_net drift:     |current_mean - baseline_mean| > 0.03
  market_prob drift:|current_mean - baseline_mean| > 0.10
"""
from __future__ import annotations

import logging
import time
from typing import Any

log = logging.getLogger(__name__)

# Drift thresholds — conservative values that only fire on genuine regime shifts
_DRIFT_THRESHOLDS = {
    "confidence":   0.10,   # 10pp shift in mean confidence
    "ev_net":       0.03,   # 3pp shift in mean EV
    "market_prob":  0.10,   # 10pp shift in mean market probability
}

_WINDOW_DAYS          = 14   # rolling window for current distribution stats
_RECOVERY_CHECKS      = 3    # consecutive non-drift checks before re-enabling ML
_MIN_WINDOW_SIGNALS   = 30   # minimum signals in window before drift check runs


class RegimeDetector:
    """
    Feature drift monitor for the ML overlay.

    Usage:
        detector = RegimeDetector(ml_store)
        detector.set_baseline(training_data)         # call after each training run
        is_drifted = detector.check(recent_signals)  # returns True if ML should disable
        detector.status()                            # dict for /mlstatus display
    """

    def __init__(self, ml_store: Any) -> None:
        self._store              = ml_store
        self._baseline: dict[str, float] | None = None
        self._consecutive_ok: int = 0
        self._ml_disabled: bool   = False
        self._drift_reasons: list[str] = []
        self._last_checked: float | None = None

        # Try loading latest regime state from DB
        self._load_from_db()

    def _load_from_db(self) -> None:
        """Restore regime state from the last stored snapshot."""
        row = self._store.get_latest_regime()
        if not row:
            return
        self._ml_disabled    = bool(row.get("ml_disabled", 0))
        self._last_checked   = row.get("computed_at")

        # Rebuild baseline from last regime snapshot means
        self._baseline = {
            "confidence":  row["mean_confidence"],
            "ev_net":      row["mean_ev_net"],
            "market_prob": row["mean_market_prob"],
        }

        if self._ml_disabled:
            log.warning(
                "[RegimeDetector] ML overlay was disabled at last shutdown (drift detected). "
                "Re-checking on next check() call."
            )

    # ── Baseline management ──────────────────────────────────────────────────

    def set_baseline(self, training_data: list[dict[str, Any]]) -> None:
        """
        Compute and store baseline feature means from the training dataset.
        Called after each successful model training run.

        training_data: list of dicts with keys: raw_confidence, ev_net, market_prob
        """
        if not training_data:
            return

        n = len(training_data)
        self._baseline = {
            "confidence":  sum(d.get("raw_confidence", 0.5) for d in training_data) / n,
            "ev_net":      sum(d.get("ev_net", 0.0) for d in training_data) / n,
            "market_prob": sum(d.get("market_prob", 0.5) for d in training_data) / n,
        }
        log.info(
            "[RegimeDetector] Baseline set: conf=%.4f ev=%.4f prob=%.4f (n=%d)",
            self._baseline["confidence"],
            self._baseline["ev_net"],
            self._baseline["market_prob"],
            n,
        )

    # ── Drift check ──────────────────────────────────────────────────────────

    def check(self, recent_signals: list[dict[str, Any]]) -> bool:
        """
        Check if the recent signal distribution has drifted from the baseline.

        recent_signals: list of dicts from ml_store.get_labeled_features() or
                        raw signal feature dicts (last _WINDOW_DAYS days).

        Returns True if ML overlay should be DISABLED (drift detected).
        Returns False if ML overlay is safe to use.
        """
        self._last_checked = time.time()

        if self._baseline is None:
            # No baseline yet (pre-training) — no drift to detect
            self._ml_disabled = False
            return False

        if len(recent_signals) < _MIN_WINDOW_SIGNALS:
            # Not enough recent data to assess drift — maintain current state
            log.debug(
                "[RegimeDetector] Only %d signals in window (need %d) — no drift check.",
                len(recent_signals), _MIN_WINDOW_SIGNALS,
            )
            return self._ml_disabled

        n = len(recent_signals)
        current = {
            "confidence":  sum(d.get("raw_confidence", 0.5) for d in recent_signals) / n,
            "ev_net":      sum(d.get("ev_net", 0.0) for d in recent_signals) / n,
            "market_prob": sum(d.get("market_prob", 0.5) for d in recent_signals) / n,
        }

        drift_reasons: list[str] = []
        for key, threshold in _DRIFT_THRESHOLDS.items():
            delta = abs(current[key] - self._baseline[key])
            if delta > threshold:
                drift_reasons.append(
                    f"{key}: Δ={delta:.4f} (threshold={threshold:.4f}, "
                    f"baseline={self._baseline[key]:.4f} current={current[key]:.4f})"
                )

        drifted = len(drift_reasons) > 0

        if drifted:
            self._consecutive_ok = 0
            self._drift_reasons  = drift_reasons
            if not self._ml_disabled:
                log.warning(
                    "[RegimeDetector] Feature drift detected — ML overlay DISABLED.\n  %s",
                    "\n  ".join(drift_reasons),
                )
            self._ml_disabled = True
        else:
            self._drift_reasons = []
            self._consecutive_ok += 1
            if self._ml_disabled and self._consecutive_ok >= _RECOVERY_CHECKS:
                log.info(
                    "[RegimeDetector] %d consecutive clean checks — ML overlay RE-ENABLED.",
                    _RECOVERY_CHECKS,
                )
                self._ml_disabled    = False
                self._consecutive_ok = 0

        # Persist regime snapshot
        mean_smart = sum(d.get("smart_money_score", 0.0) for d in recent_signals) / n
        self._store.save_regime_snapshot(
            n_signals=n,
            mean_confidence=current["confidence"],
            mean_ev_net=current["ev_net"],
            mean_market_prob=current["market_prob"],
            mean_smart_money=mean_smart,
            conf_drift="confidence" in " ".join(drift_reasons),
            ev_drift="ev_net" in " ".join(drift_reasons),
            prob_drift="market_prob" in " ".join(drift_reasons),
            ml_disabled=self._ml_disabled,
            window_days=_WINDOW_DAYS,
        )

        return self._ml_disabled

    @property
    def is_ml_safe(self) -> bool:
        """True if ML overlay is currently safe to use (no drift detected)."""
        return not self._ml_disabled

    # ── Status ───────────────────────────────────────────────────────────────

    def status(self) -> dict[str, Any]:
        """Return status dict for /mlstatus display."""
        return {
            "ml_safe":          self.is_ml_safe,
            "drift_detected":   self._ml_disabled,
            "drift_reasons":    self._drift_reasons,
            "consecutive_ok":   self._consecutive_ok,
            "recovery_needed":  f"{max(0, _RECOVERY_CHECKS - self._consecutive_ok)} more clean checks",
            "baseline":         {k: round(v, 4) for k, v in (self._baseline or {}).items()},
            "last_checked":     (
                __import__("datetime").datetime.fromtimestamp(
                    self._last_checked, tz=__import__("datetime").timezone.utc
                ).strftime("%Y-%m-%d %H:%M UTC")
                if self._last_checked else "never"
            ),
            "thresholds":       _DRIFT_THRESHOLDS,
            "window_days":      _WINDOW_DAYS,
        }
