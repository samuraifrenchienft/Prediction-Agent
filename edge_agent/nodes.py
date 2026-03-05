from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from .models import (
    Catalyst,
    MarketSnapshot,
    PortfolioState,
    QualificationState,
    Recommendation,
    RiskPolicy,
    Venue,
)
from .ai_service import get_ai_response


# ---------------------------------------------------------------------------
# Signal types — the 5 focused edge signals EDGE looks for
# ---------------------------------------------------------------------------

class SignalType(str, Enum):
    INJURY_MOMENTUM_REVERSAL = "INJURY_MOMENTUM_REVERSAL"  # live game: full-roster team trailing in Q2
    PRE_GAME_INJURY_LAG      = "PRE_GAME_INJURY_LAG"       # market hasn't repriced key injury yet
    NEWS_LAG                 = "NEWS_LAG"                   # breaking news not yet in market price
    FAVORITE_LONGSHOT_BIAS   = "FAVORITE_LONGSHOT_BIAS"    # favorite systematically underpriced vs longshot
    CROSS_MARKET_CORRELATION = "CROSS_MARKET_CORRELATION"  # related prop market lags behind primary
    NONE                     = "NONE"


# Keywords that indicate a player is unavailable
_INJURY_KEYWORDS = [
    "out", "ruled out", "dnp", "did not play", "injured", "injury",
    "doubtful", "questionable", "scratch", "scratched", "day-to-day",
    "missed", "sidelined", "unavailable", "inactive", "concussion",
    "sprain", "fracture", "illness", "knee", "hamstring", "ankle",
    "shoulder", "suspension", "suspended",
]

# Keywords that indicate a team has a FULL roster / no major injuries
_FULL_ROSTER_KEYWORDS = [
    "full strength", "full roster", "healthy", "available", "active",
    "returned", "back in lineup", "cleared", "no injury report",
]


def _detect_injury_in_catalysts(catalysts: list[Catalyst]) -> bool:
    """Return True if any catalyst headline contains injury language."""
    for c in catalysts:
        src = c.source.lower()
        if any(kw in src for kw in _INJURY_KEYWORDS):
            return True
    return False


def _detect_injury_in_text(text: str) -> bool:
    t = text.lower()
    return any(kw in t for kw in _INJURY_KEYWORDS)


def _classify_signal(snapshot: MarketSnapshot, catalysts: list[Catalyst]) -> SignalType:
    """
    Classify which (if any) primary EDGE signal applies to this market.

    Signal 1 — INJURY_MOMENTUM_REVERSAL
      Live sports market (TTR < 5h), injury news in catalysts,
      and current price has dropped ≥10pp below the opening probability
      (the full-roster team is behind; market has moved against them).

    Signal 2 — PRE_GAME_INJURY_LAG
      Pre-game sports market (5h < TTR < 48h), injury news present,
      but the market prob hasn't moved much from 50/50 (market is slow
      to reprice the injury — the edge window).

    Signal 3 — NEWS_LAG
      Non-sports or macro/politics market where catalysts show high
      directional confidence (>0.40 weighted average) but the market
      prob is still clustered near 0.5 (market hasn't priced the news).

    Signal 4 — FAVORITE_LONGSHOT_BIAS
      Well-documented statistical bias: favorites (55–70%) are
      systematically underpriced because bettors prefer longshot payouts.
      Applies to high-liquidity, low-spread markets where the bias is
      most reliably exploitable.

    Signal 5 — CROSS_MARKET_CORRELATION
      A synthetic catalyst was injected by the CrossMarketCorrelator
      indicating this prop/secondary market lags behind a correlated
      primary market. Signal is pre-set on the snapshot's catalysts.
    """
    q = snapshot.question.lower()
    is_sports = any(w in q for w in [
        "nfl", "nba", "mlb", "nhl", "soccer", "win", "championship",
        "playoff", "superbowl", "super bowl", "match", "game", "beat",
        "cover", "spread", "points", "score",
    ])
    injury_detected = _detect_injury_in_catalysts(catalysts) or _detect_injury_in_text(snapshot.question)
    ttr = snapshot.time_to_resolution_hours

    # Signal 1: in-game momentum reversal
    if is_sports and injury_detected and ttr < 5:
        opening = snapshot.opening_prob if snapshot.opening_prob > 0 else 0.5
        price_drop = opening - snapshot.market_prob
        if price_drop >= 0.10:  # team has fallen ≥10pp since open → they're losing
            return SignalType.INJURY_MOMENTUM_REVERSAL

    # Signal 2: pre-game injury lag
    if is_sports and injury_detected and 4 < ttr < 48:
        # Market still near 50/50 despite injury news → slow reprice
        if 0.35 < snapshot.market_prob < 0.65:
            return SignalType.PRE_GAME_INJURY_LAG

    # Signal 3: breaking news lag (politics / macro)
    if not is_sports and catalysts:
        weighted_dir = sum(abs(c.direction) * c.confidence * c.quality for c in catalysts)
        avg_dir = weighted_dir / len(catalysts)
        if avg_dir > 0.40 and 0.35 < snapshot.market_prob < 0.65:
            return SignalType.NEWS_LAG

    # Signal 4: favorite-longshot bias
    # Targets favorites in the 55–70% range in liquid, tight-spread markets.
    # No catalyst required — this is a structural statistical bias.
    if (
        0.55 < snapshot.market_prob < 0.70
        and snapshot.depth_usd >= 8_000
        and snapshot.spread_bps <= 150
    ):
        return SignalType.FAVORITE_LONGSHOT_BIAS

    # Signal 5: cross-market correlation lag
    # CrossMarketCorrelator injects a synthetic catalyst with source
    # starting with "CROSS_MARKET:" when it detects a correlated lag.
    if any(c.source.startswith("CROSS_MARKET:") for c in catalysts):
        return SignalType.CROSS_MARKET_CORRELATION

    return SignalType.NONE


# ---------------------------------------------------------------------------
# Output dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ProbabilityOutput:
    p_true: float
    confidence: float
    uncertainty_band: tuple[float, float]
    thesis: list[str]
    disconfirming_evidence: list[str]
    signal: SignalType = SignalType.NONE


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


# ---------------------------------------------------------------------------
# Probability node
# ---------------------------------------------------------------------------

def probability_node(snapshot: MarketSnapshot, catalysts: list[Catalyst]) -> ProbabilityOutput:
    """
    Estimates true probability using AI analysis of the market question + news catalysts.

    Applies signal-specific prompting for all five EDGE signals:
      1. INJURY_MOMENTUM_REVERSAL — live game, full-roster team trailing in Q2
      2. PRE_GAME_INJURY_LAG      — key player out, market slow to reprice
      3. NEWS_LAG                 — breaking news not yet in market price
      4. FAVORITE_LONGSHOT_BIAS   — favorite statistically underpriced vs longshot
      5. CROSS_MARKET_CORRELATION — prop market lags correlated primary market
    """
    signal = _classify_signal(snapshot, catalysts)

    # Build signal-aware context block
    signal_context = ""
    if signal == SignalType.INJURY_MOMENTUM_REVERSAL:
        opening = snapshot.opening_prob if snapshot.opening_prob > 0 else 0.5
        signal_context = (
            f"\n⚠️ SIGNAL: INJURY_MOMENTUM_REVERSAL\n"
            f"This appears to be a LIVE GAME. The team favored by the full roster opened at "
            f"{opening:.0%} probability and is now priced at {snapshot.market_prob:.0%} — "
            f"they are likely trailing in-game. Injury news is present for the opposing team. "
            f"Historical data shows full-roster teams trailing in Q2 win at a significantly higher "
            f"rate than their live in-game price implies. Weight this heavily.\n"
        )
    elif signal == SignalType.PRE_GAME_INJURY_LAG:
        signal_context = (
            f"\n⚠️ SIGNAL: PRE_GAME_INJURY_LAG\n"
            f"A key player injury has been reported but the market is still priced near "
            f"{snapshot.market_prob:.0%}. Markets typically take 30-90 minutes to fully reprice "
            f"significant injuries. If the injured player is a starter or key contributor, the "
            f"true probability should be materially different from the current market price. "
            f"Assess the magnitude of the injury impact and whether the market has already repriced.\n"
        )
    elif signal == SignalType.NEWS_LAG:
        signal_context = (
            f"\n⚠️ SIGNAL: NEWS_LAG\n"
            f"News catalysts show strong directional signal but the market is still priced near "
            f"{snapshot.market_prob:.0%}. This may represent a repricing lag opportunity. "
            f"Assess whether the catalyst is genuinely material and likely to move the market.\n"
        )
    elif signal == SignalType.FAVORITE_LONGSHOT_BIAS:
        signal_context = (
            f"\n⚠️ SIGNAL: FAVORITE_LONGSHOT_BIAS\n"
            f"This market is priced at {snapshot.market_prob:.0%} — in the classic 'favorite' range "
            f"(55–70%). Academic research across sports betting, prediction markets, and horse racing "
            f"consistently documents that favorites in this range are systematically underpriced. "
            f"Bettors overweight longshot outcomes (higher payouts) causing the market to "
            f"underestimate the favorite's true win probability by 3–8pp on average. "
            f"Market depth ${snapshot.depth_usd:,.0f} and spread {snapshot.spread_bps:.0f}bps "
            f"confirm liquidity is sufficient for this bias to be exploitable. "
            f"Estimate whether the true probability is materially higher than the current market price.\n"
        )
    elif signal == SignalType.CROSS_MARKET_CORRELATION:
        # Extract the cross-market context injected as a synthetic catalyst
        cross_catalyst = next(
            (c for c in catalysts if c.source.startswith("CROSS_MARKET:")), None
        )
        cross_detail = cross_catalyst.source[len("CROSS_MARKET:"):] if cross_catalyst else "Related primary market has moved."
        signal_context = (
            f"\n⚠️ SIGNAL: CROSS_MARKET_CORRELATION\n"
            f"A correlated primary market has moved significantly but this secondary/prop market "
            f"is still priced at {snapshot.market_prob:.0%}. The lag exists because secondary markets "
            f"have lower volume and fewer active liquidity providers updating their limit orders. "
            f"Correlation detail: {cross_detail}. "
            f"Assess whether the primary market move should logically shift this market's probability "
            f"and by how much.\n"
        )

    system_prompt = (
        "You are EDGE, a disciplined prediction market analyst specializing in finding mispricings. "
        "Analyze the market question, current price, and news catalysts to estimate the true probability. "
        "Be calibrated — if the market is efficient, say so. Only diverge from market_prob when you have "
        "clear evidence. Return a JSON object with exactly these fields:\n"
        '{"p_true": float (0-1), "confidence": float (0-1), '
        '"bull_thesis": list[str], "disconfirming_evidence": list[str], '
        '"signal_strength": str ("strong"|"moderate"|"weak"|"none")}'
    )

    catalyst_lines = "\n".join(
        f"  - [{c.source}] direction={c.direction:+.2f}, confidence={c.confidence:.2f}, quality={c.quality:.2f}"
        for c in catalysts
    ) or "  (no catalysts found)"

    prompt = (
        f"Market Question: {snapshot.question or snapshot.market_id}\n"
        f"Venue: {snapshot.venue.value}\n"
        f"Current Market Probability (YES): {snapshot.market_prob:.3f}\n"
        f"Time to Resolution: {snapshot.time_to_resolution_hours:.1f} hours\n"
        f"Spread: {snapshot.spread_bps:.0f} bps | Depth: ${snapshot.depth_usd:,.0f}\n"
        f"{signal_context}\n"
        f"News Catalysts:\n{catalyst_lines}\n\n"
        f"Given this data, what is the true probability this market resolves YES? "
        f"Return your JSON analysis."
    )

    ai_analysis = get_ai_response(prompt, task_type="complex", system_prompt=system_prompt)

    if ai_analysis:
        p_true = float(ai_analysis.get("p_true", snapshot.market_prob))
        thesis = ai_analysis.get("bull_thesis", [])
        disconfirming = ai_analysis.get("disconfirming_evidence", [])
        ai_confidence = float(ai_analysis.get("confidence", 0.5))
        signal_strength = ai_analysis.get("signal_strength", "none")

        # Boost confidence when a named signal is active and AI agrees it's strong
        if signal != SignalType.NONE and signal_strength in ("strong", "moderate"):
            ai_confidence = min(0.95, ai_confidence + 0.15)

        # Prepend signal label to thesis
        if signal != SignalType.NONE:
            thesis = [f"[{signal.value}] {thesis[0]}"] + thesis[1:] if thesis else [f"[{signal.value}] Signal detected."]
    else:
        # Fallback
        weighted_signal = sum(c.direction * c.confidence * c.quality for c in catalysts)
        catalyst_strength_val = min(0.12, max(-0.12, weighted_signal))
        p_true = min(0.99, max(0.01, snapshot.market_prob + catalyst_strength_val))
        thesis = [
            "Probability shifted from venue implied odds after weighted catalyst scoring (FALLBACK).",
            f"Catalyst-adjusted estimate moved by {p_true - snapshot.market_prob:+.3f}.",
        ]
        disconfirming = [
            "AI analysis failed. Using simplified fallback logic.",
            "Low-liquidity periods can distort short-term market probability.",
        ]
        ai_confidence = 0.5

    # Compute final confidence from AI output + catalyst quality
    source_confidence = 0.5
    if catalysts:
        source_confidence = sum(c.confidence * c.quality for c in catalysts) / len(catalysts)
    confidence = max(0.45, min(0.95, (ai_confidence * 0.7) + (source_confidence * 0.3)))

    uncertainty = max(0.02, 0.18 - (confidence * 0.12))
    band = (max(0.01, p_true - uncertainty), min(0.99, p_true + uncertainty))

    return ProbabilityOutput(
        p_true=p_true,
        confidence=confidence,
        uncertainty_band=band,
        thesis=thesis,
        disconfirming_evidence=disconfirming,
        signal=signal,
    )


# ---------------------------------------------------------------------------
# EV node
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Qualification gate
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Risk policy node
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Recommendation node
# ---------------------------------------------------------------------------

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

    # Signal-specific invalidation conditions
    invalidation = [
        "Confidence falls below threshold for two consecutive evaluation windows.",
        "Execution costs rise enough to make ev_net <= 0.",
    ]
    if prob.signal == SignalType.INJURY_MOMENTUM_REVERSAL:
        invalidation = [
            "Injured team's backup outperforms — gap closes without full-roster team pulling ahead.",
            "Full-roster team falls further behind in Q3 — momentum has not reversed.",
            "Market price converges upward without thesis playing out (fake-out).",
        ]
    elif prob.signal == SignalType.PRE_GAME_INJURY_LAG:
        invalidation = [
            "Market fully reprices before entry — edge disappears.",
            "Injured player returns to play (medical clearance before game).",
            "Backup player confirmed as strong replacement (reduces true edge).",
        ]
    elif prob.signal == SignalType.NEWS_LAG:
        invalidation = [
            "News story is retracted or contradicted by follow-up reporting.",
            "Market reprices fully before entry — no lag remains.",
            "Subsequent catalysts move in the opposing direction.",
        ]
    elif prob.signal == SignalType.FAVORITE_LONGSHOT_BIAS:
        invalidation = [
            "Market is already efficiently priced — AI model agrees with market prob.",
            "Low-liquidity environment distorts the bias (depth falls below threshold).",
            "Unusual event or upset risk causes genuine longshot outcome probability to rise.",
        ]
    elif prob.signal == SignalType.CROSS_MARKET_CORRELATION:
        invalidation = [
            "Primary market move was noise or reverts before secondary market reprices.",
            "Secondary market reprices fully before entry — correlation lag closed.",
            "New information affects secondary market independently of primary.",
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
            "signal": prob.signal.value,
            "time_to_resolution_hours": snapshot.time_to_resolution_hours,
            "ambiguity_score": snapshot.ambiguity_score,
            "volatility_entropy_score": snapshot.volatility_entropy_score,
            "question": snapshot.question,
        },
    )
