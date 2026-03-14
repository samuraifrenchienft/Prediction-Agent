"""
edge_agent.ml — Machine Learning overlay for EDGE signal pipeline.
==================================================================

Architecture: ML modules operate as a non-blocking overlay on top of the
deterministic rule-based engine. They NEVER replace hard safety gates
(negative EV, min depth, min TTR). They augment confidence scoring and
provide shadow-mode predictions that improve over time as outcome data
accumulates.

Modules:
  ml_store.py          — SQLite persistence for ML state
  confidence_calibrator.py — Logistic regression: raw confidence → calibrated win prob
  signal_scorer.py     — XGBoost shadow-mode scorer: features → P(WIN)
  trader_features.py   — Smart money feature extraction from TraderCache
  regime_detector.py   — Feature drift monitor with automatic fallback

Deployment phases:
  Phase 1 (now):    Shadow mode — log predictions, never affect output
  Phase 2 (150+ resolved signals): Calibrate confidence → real win rates
  Phase 3 (400+ resolved signals): Soft gate — XGBoost can promote WATCHLIST→QUALIFIED
  Never:            Override hard rejections (neg EV, depth, TTR)
"""
from __future__ import annotations

from .ml_store import MLStore
from .confidence_calibrator import ConfidenceCalibrator
from .signal_scorer import SignalScorer
from .trader_features import TraderFeatureExtractor
from .regime_detector import RegimeDetector

__all__ = [
    "MLStore",
    "ConfidenceCalibrator",
    "SignalScorer",
    "TraderFeatureExtractor",
    "RegimeDetector",
]
