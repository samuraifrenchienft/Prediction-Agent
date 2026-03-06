from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .models import (
    AIAnalysis,
    Catalyst,
    MarketSnapshot,
    PortfolioState,
    QualificationState,
    Recommendation,
    RiskPolicy,
    Venue,
)


@dataclass
class ProbabilityOutput:
    p_true: float
    confidence: float
    uncertainty_band: tuple[float, float]
    thesis: list[str]
    disconfirming_evidence: list[str]


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
    if venue == Venue.JUPITER_PREDICTION:
        return 0.005
    if venue == Venue.POLYMARKET:
        return 0.0045
    return 0.003


from .ai_service import get_ai_response

def probability_node(snapshot: MarketSnapshot, catalysts: list[Catalyst]) -> ProbabilityOutput:
    """Analyzes market data and catalysts to predict the probability of a market resolving to 'yes'.

    This function now uses an AI model to get a more sophisticated probability assessment.
    """

    system_prompt = (
        "You are a world-class prediction market analyst. Your task is to analyze market data and catalysts "
        "and return your analysis as a structured JSON object. The JSON object should conform to the following schema: "
        '{"p_true": float, "bull_thesis": list[str], "key_catalysts": list[str], "disconfirming_evidence": list[str], "market_positioning": str}'
    )

    prompt = (
        f"Analyze the following market data and catalysts. "
        f"Market Data:\n"
        f"- Market ID: {snapshot.market_id}\n"
        f"- Venue: {snapshot.venue}\n"
        f"- Current Market Probability: {snapshot.market_prob}\n"
        f"- Time to Resolution (hours): {snapshot.time_to_resolution_hours}\n\n"
        f"Catalysts:\n"
    )
    for c in catalysts:
        prompt += f"- Source: {c.source}, Quality: {c.quality}, Direction: {c.direction}, Confidence: {c.confidence}\n"

    ai_analysis = get_ai_response(prompt, task_type="complex", system_prompt=system_prompt)

    if ai_analysis:
        p_true = float(ai_analysis.get("p_true", snapshot.market_prob))
        thesis = ai_analysis.get("bull_thesis") or ai_analysis.get("key_catalysts") or []
        disconfirming = ai_analysis.get("disconfirming_evidence", [])
        if isinstance(thesis, str):
            thesis = [thesis]
        if isinstance(disconfirming, str):
            disconfirming = [disconfirming]
    else:
        # Fallback to original logic if AI fails
        weighted_signal = sum(c.direction * c.confidence * c.quality for c in catalysts)
        catalyst_strength = min(0.12, max(-0.12, weighted_signal))
        p_true = min(0.99, max(0.01, snapshot.market_prob + catalyst_strength))
        thesis = [
            "Probability shifted from venue implied odds after weighted catalyst scoring (FALLBACK).",
            f"Catalyst-adjusted estimate moved by {p_true - snapshot.market_prob:+.3f}.",
        ]
        disconfirming = [
            "AI analysis failed. Using simplified logic.",
            "Low-liquidity periods can distort short-term market probability.",
        ]

    source_confidence = 0.5
    if catalysts:
        source_confidence = sum(c.confidence * c.quality for c in catalysts) / len(catalysts)
    confidence = max(0.45, min(0.95, source_confidence))

    uncertainty = max(0.02, 0.18 - (confidence * 0.12))
    band = (max(0.01, p_true - uncertainty), min(0.99, p_true + uncertainty))

    return ProbabilityOutput(
        p_true=p_true,
        confidence=confidence,
        uncertainty_band=band,
        thesis=thesis,
        disconfirming_evidence=disconfirming,
    )


def edge_ev_node(snapshot: MarketSnapshot, p_true: float) -> EvOutput:
    edge = p_true - snapshot.market_prob
    ev_gross = edge

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
            "time_to_resolution_hours": snapshot.time_to_resolution_hours,
            "ambiguity_score": snapshot.ambiguity_score,
            "volatility_entropy_score": snapshot.volatility_entropy_score,
        },
    )
