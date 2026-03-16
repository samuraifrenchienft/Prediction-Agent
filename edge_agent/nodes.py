from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING

from .models import (
    Catalyst,
    MarketSnapshot,
    PortfolioState,
    QualificationState,
    Recommendation,
    RiskPolicy,
    Venue,
)

if TYPE_CHECKING:
    from .ml.confidence_calibrator import ConfidenceCalibrator

# Lazy-import econ scanner for Fed market probability override
_econ_scanner = None  # type: ignore[assignment]

def _get_econ_scanner():
    """Lazy-load econ scanner to avoid circular imports."""
    global _econ_scanner
    if _econ_scanner is None:
        try:
            from .scanners import econ_scanner as _es
            _econ_scanner = _es
        except Exception:
            _econ_scanner = False  # permanently disable if import fails
    return _econ_scanner if _econ_scanner else None

log = logging.getLogger(__name__)

# Module-level calibrator reference — injected at startup by run_edge_bot.py
# If None (pre-training), probability_node uses raw confidence (safe passthrough)
_calibrator: "ConfidenceCalibrator | None" = None


def set_calibrator(cal: "ConfidenceCalibrator | None") -> None:
    """Inject the active ConfidenceCalibrator into the nodes pipeline."""
    global _calibrator
    _calibrator = cal
    if cal is not None:
        log.info("[nodes] ConfidenceCalibrator injected (active=%s)", cal._active)


class SignalType(str, Enum):
    INJURY_MOMENTUM_REVERSAL = "INJURY_MOMENTUM_REVERSAL"
    PRE_GAME_INJURY_LAG      = "PRE_GAME_INJURY_LAG"
    NEWS_LAG                 = "NEWS_LAG"
    FAVORITE_LONGSHOT_BIAS   = "FAVORITE_LONGSHOT_BIAS"
    NONE                     = "NONE"


@dataclass
class ProbabilityOutput:
    p_true: float
    confidence: float
    uncertainty_band: tuple[float, float]
    thesis: list[str]
    disconfirming_evidence: list[str]
    signal: SignalType = SignalType.NONE       # set by probability_node
    raw_confidence: float = 0.0               # pre-calibration value (for ML logging)
    catalyst_strength: float = 0.0            # Σ(dir×conf×qual) used to shift p_true


@dataclass
class EvOutput:
    edge: float
    ev_gross: float
    fees: float
    slippage_cost: float
    impact_cost: float
    resolution_risk_haircut: float
    ev_net: float


def _base_fee_for_venue(venue: Venue) -> float:
    if venue == Venue.POLYMARKET:
        return 0.0045
    return 0.003  # Kalshi and others


# ---------------------------------------------------------------------------
# Signal keywords for PRE_GAME_INJURY_LAG detection
# ---------------------------------------------------------------------------

_INJURY_KEYWORDS = {
    "injur", "hurt", "ruled out", "scratch", "doubtful", "questionable",
    "dnp", "out:", "will not play", "illness", "suspension", "ankle",
    "knee", "hamstring", "concussion", "did not practice",
}


def classify_signal(
    snapshot: MarketSnapshot,
    catalysts: list[Catalyst],
    p_true: float,
) -> SignalType:
    """
    Rule-based signal classifier — deterministic, zero AI calls.

    Priority order:
    1. PRE_GAME_INJURY_LAG  — confirmed injury catalyst from InjuryAPIClient
                              (source starts with "INJURY:") with status-weighted TTR
    2. NEWS_LAG             — high-quality news catalyst pushing prob ≥5.5pp off market
    3. FAVORITE_LONGSHOT_BIAS — extreme market probability in a liquid market
    4. NONE

    Strategy notes:
    • PRE_GAME: TTR window varies by severity — "Out"/"Suspension" → 36h,
      "Questionable"/"Day-To-Day" → 20h.  This avoids stale injury flags
      that won't resolve before the market closes.
    • NEWS_LAG: threshold is 5.5pp edge, same as before, but now capped at
      catalysts with quality > 0.60 to reduce noise.
    • FAVORITE_LONGSHOT_BIAS: now requires depth_usd > 2000 to avoid false
      positives in illiquid micro-markets where extreme probs are normal.
    """
    ttr = snapshot.time_to_resolution_hours

    # 1. PRE_GAME_INJURY_LAG
    # InjuryAPIClient injects catalysts with source="INJURY:..." — more reliable
    # than scanning keywords in the market question.
    injury_catalysts = [c for c in catalysts if c.source.startswith("INJURY:")]
    if injury_catalysts:
        for c in injury_catalysts:
            if abs(c.direction) <= 0.45 or c.confidence <= 0.50:
                continue
            # Use shorter TTR window for less severe statuses to avoid stale signals
            # Out/Suspension (dir ≤ -0.80): allow up to 36h
            # Doubtful (dir ≤ -0.65): allow up to 28h
            # Questionable/Day-To-Day: allow up to 20h
            if c.direction <= -0.80:
                ttr_limit = 36
            elif c.direction <= -0.65:
                ttr_limit = 28
            else:
                ttr_limit = 20
            if ttr < ttr_limit:
                return SignalType.PRE_GAME_INJURY_LAG

    # Fallback keyword check for markets where injury client missed coverage
    question_lower = (snapshot.question or snapshot.market_id or "").lower()
    has_injury_keyword = any(kw in question_lower for kw in _INJURY_KEYWORDS)
    strong_news_dir = any(abs(c.direction) > 0.55 and c.confidence > 0.55 for c in catalysts)
    if has_injury_keyword and strong_news_dir and ttr < 48:
        return SignalType.PRE_GAME_INJURY_LAG

    # 2. NEWS_LAG — catalyst implies meaningful edge vs current market price
    # Raised quality bar to 0.60 (was 0.55) to reduce low-signal noise
    edge = abs(p_true - snapshot.market_prob)
    news_catalyst = any(
        not c.source.startswith("INJURY:")
        and c.quality > 0.60
        and abs(c.direction) > 0.35
        for c in catalysts
    )
    if news_catalyst and edge >= 0.055:
        return SignalType.NEWS_LAG

    # 3. FAVORITE_LONGSHOT_BIAS — only in liquid markets (depth_usd > 2000)
    # Illiquid micro-markets routinely sit at extreme probs — not a statistical edge
    if snapshot.depth_usd > 2000 and (
        snapshot.market_prob > 0.82 or snapshot.market_prob < 0.07
    ):
        return SignalType.FAVORITE_LONGSHOT_BIAS

    return SignalType.NONE


def probability_node(snapshot: MarketSnapshot, catalysts: list[Catalyst]) -> ProbabilityOutput:
    """
    Catalyst-weighted probability estimation.
    Pure math — zero AI calls. Fast, deterministic, free-tier safe.

    Uses AI-scored catalyst data (from CatalystDetectionEngine) to adjust
    market probability. The catalysts already carry AI signal quality scores;
    combining them mathematically is more reliable than a second AI re-assessment.

    Special handling for Fed rate / economic markets:
      These are mutually-exclusive outcome groups (cut 25bps, cut 50bps, hold,
      hike). The generic catalyst pipeline would give them all the same
      adjustment — producing identical probabilities, which is nonsensical.
      Instead, delegate to the econ scanner's specialized model (yield curve +
      EFFR + BPS magnitude scaling).
    """
    # ── Econ scanner override for Fed rate / economic markets ──────────────
    # Detects Fed/rate markets by question text and uses the specialized
    # probability model that differentiates cut/hike/hold/bps magnitude.
    econ_override = None
    econ_notes = ""
    question = (snapshot.question or snapshot.market_id or "").lower()
    es = _get_econ_scanner()
    if es is not None:
        category = es._detect_econ_category(question)
        if category is not None:
            try:
                ctx = es._get_econ_context()
                prob, notes = es._estimate_econ_prob(
                    snapshot.question or snapshot.market_id, category, ctx
                )
                if prob is not None:
                    econ_override = prob
                    econ_notes = notes
                    log.debug(
                        "[nodes] Econ override for '%s': %.1f%% (%s)",
                        question[:50], prob * 100, notes,
                    )
            except Exception as exc:
                log.warning("[nodes] Econ scanner override failed: %s", exc)

    if catalysts:
        weighted_signal = sum(c.direction * c.confidence * c.quality for c in catalysts)
        catalyst_strength = min(0.12, max(-0.12, weighted_signal))
        source_confidence = sum(c.confidence * c.quality for c in catalysts) / len(catalysts)
    else:
        catalyst_strength = 0.0
        source_confidence = 0.5

    if econ_override is not None:
        # Use econ scanner probability directly — it already accounts for
        # cut/hike/hold direction and BPS magnitude.
        p_true = econ_override
        catalyst_strength = p_true - snapshot.market_prob
    else:
        p_true = min(0.99, max(0.01, snapshot.market_prob + catalyst_strength))

    # Apply Platt-scaling calibration if a trained calibrator is available.
    # Falls back to raw source_confidence if calibrator is inactive or not loaded.
    raw_confidence = max(0.45, min(0.95, source_confidence))
    if _calibrator is not None:
        confidence = _calibrator.calibrate(raw_confidence)
    else:
        confidence = raw_confidence

    uncertainty = max(0.02, 0.18 - (confidence * 0.12))
    band = (max(0.01, p_true - uncertainty), min(0.99, p_true + uncertainty))

    signal = classify_signal(snapshot, catalysts, p_true)

    adj = p_true - snapshot.market_prob
    thesis: list[str] = [
        f"Catalyst-adjusted probability: {p_true:.1%} "
        f"(market: {snapshot.market_prob:.1%}, Δ{adj:+.1%}).",
        f"Signal: {signal.value}.",
    ]
    if econ_override is not None and econ_notes:
        thesis.append(f"Econ model: {econ_notes}")
    if catalysts:
        top_cat = max(catalysts, key=lambda c: c.quality * abs(c.direction))
        thesis.append(
            f"Strongest catalyst: [{top_cat.source}] "
            f"dir={top_cat.direction:+.2f} conf={top_cat.confidence:.2f} "
            f"qual={top_cat.quality:.2f}"
        )

    disconfirming: list[str] = [
        "Market consensus may reflect information not yet captured in news feeds.",
        "Low-liquidity periods can distort short-term market probability.",
    ]

    return ProbabilityOutput(
        p_true=p_true,
        confidence=confidence,
        uncertainty_band=band,
        thesis=thesis,
        disconfirming_evidence=disconfirming,
        signal=signal,
        raw_confidence=raw_confidence,
        catalyst_strength=catalyst_strength,
    )


def edge_ev_node(snapshot: MarketSnapshot, p_true: float) -> EvOutput:
    edge = p_true - snapshot.market_prob
    # Use abs(edge) for ev_gross so that NO bets (edge < 0) are evaluated correctly.
    # A negative edge means we believe the market is over-priced → we BUY_NO.
    # The gross EV of the bet is the magnitude of our disagreement with the market.
    ev_gross = abs(edge)

    fees = _base_fee_for_venue(snapshot.venue)
    slippage_cost = min(0.025, snapshot.spread_bps / 10000)
    impact_cost = 0.003 if snapshot.depth_usd < 5000 else 0.001
    resolution_risk_haircut = 0.002 if snapshot.time_to_resolution_hours < 24 else 0.001

    ev_net = ev_gross - (fees + slippage_cost + impact_cost + resolution_risk_haircut)

    return EvOutput(
        edge=edge,
        ev_gross=ev_gross,
        fees=fees,
        slippage_cost=slippage_cost,
        impact_cost=impact_cost,
        resolution_risk_haircut=resolution_risk_haircut,
        ev_net=ev_net,
    )


def qualification_gate(
    snapshot: MarketSnapshot,
    prob: ProbabilityOutput,
    ev: EvOutput,
    policy: RiskPolicy,
) -> tuple[QualificationState, list[str]]:
    reasons: list[str] = []

    if snapshot.depth_usd < policy.min_depth_usd:
        reasons.append("LOW_DEPTH")
    if snapshot.spread_bps > policy.max_spread_bps:
        reasons.append("EXECUTION_TRAP")
    if ev.ev_net <= 0:
        reasons.append("EV_NON_POSITIVE")
    if prob.confidence < policy.min_confidence:
        reasons.append("LOW_CONFIDENCE")
    if snapshot.time_to_resolution_hours < policy.min_time_to_resolution_hours:
        reasons.append("TIME_DECAY")
    if snapshot.ambiguity_score > policy.max_ambiguity_score:
        reasons.append("AMBIGUITY_RISK")
    if snapshot.volatility_entropy_score > policy.max_volatility_entropy_score:
        reasons.append("ENTROPY_HIGH")

    if not reasons:
        return QualificationState.QUALIFIED, reasons

    watchlist_reasons = {"LOW_DEPTH", "LOW_CONFIDENCE", "EXECUTION_TRAP", "ENTROPY_HIGH"}
    if set(reasons).issubset(watchlist_reasons):
        return QualificationState.WATCHLIST, reasons
    return QualificationState.REJECTED, reasons


def risk_policy_node(
    qualification_state: QualificationState,
    portfolio: PortfolioState,
    policy: RiskPolicy,
    theme: str,
) -> tuple[float, list[str]]:
    reasons: list[str] = []
    capped_size = policy.max_position_pct_bankroll

    if portfolio.daily_drawdown_pct >= policy.max_daily_drawdown_pct:
        reasons.append("DAILY_DRAWDOWN_LIMIT")
        capped_size = 0.0

    current_theme = portfolio.theme_exposure_pct.get(theme, 0.0)
    remaining_theme_capacity = max(0.0, policy.max_theme_exposure_pct - current_theme)
    capped_size = min(capped_size, remaining_theme_capacity)

    if qualification_state != QualificationState.QUALIFIED:
        capped_size = 0.0

    return capped_size, reasons


def recommendation_node(
    snapshot: MarketSnapshot,
    prob: ProbabilityOutput,
    ev: EvOutput,
    qualification_state: QualificationState,
    reject_reasons: list[str],
    capped_size: float,
    policy_reasons: list[str],
) -> Recommendation:
    action = "HOLD"
    if qualification_state == QualificationState.QUALIFIED and capped_size > 0:
        action = "BUY_YES" if ev.edge > 0 else "BUY_NO"

    invalidation = [
        "Confidence falls below threshold for two consecutive evaluation windows.",
        "Execution costs rise enough to make ev_net <= 0.",
    ]

    return Recommendation(
        market_id=snapshot.market_id,
        venue=snapshot.venue,
        timestamp=datetime.now(timezone.utc),
        market_prob=snapshot.market_prob,
        agent_prob=prob.p_true,
        uncertainty_band=prob.uncertainty_band,
        edge=ev.edge,
        ev_gross=ev.ev_gross,
        fees=ev.fees,
        slippage_cost=ev.slippage_cost,
        impact_cost=ev.impact_cost,
        resolution_risk_haircut=ev.resolution_risk_haircut,
        ev_net=ev.ev_net,
        confidence=prob.confidence,
        action=action,
        entry_range=(max(0.01, snapshot.market_prob - 0.02), min(0.99, snapshot.market_prob + 0.02)),
        max_position_pct_bankroll=capped_size,
        thesis=prob.thesis,
        disconfirming_evidence=prob.disconfirming_evidence,
        invalidation=invalidation,
        qualification_state=qualification_state,
        reject_reason_codes=reject_reasons + policy_reasons,
        requires_approval=True,
        metadata={
            "signal":   prob.signal.value,                              # was missing — fixed
            "question": snapshot.question or snapshot.market_id,        # was missing — fixed
            "time_to_resolution_hours":   snapshot.time_to_resolution_hours,
            "ambiguity_score":            snapshot.ambiguity_score,
            "volatility_entropy_score":   snapshot.volatility_entropy_score,
        },
    )
