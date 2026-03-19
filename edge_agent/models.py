from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class Venue(str, Enum):
    JUPITER_PREDICTION = "jupiter_prediction"
    KALSHI = "kalshi"
    POLYMARKET = "polymarket"


class QualificationState(str, Enum):
    QUALIFIED = "qualified"
    WATCHLIST = "watchlist"
    REJECTED = "rejected"


@dataclass
class Catalyst:
    source: str
    quality: float
    direction: float
    confidence: float


# Backward-compat alias — adapters previously used AIAnalysis as the catalyst type
AIAnalysis = Catalyst


@dataclass
class AIAnalysis:
    """Extended AI response model used by the Q&A chatbot."""

    source: str | None = None
    quality: float | None = None
    direction: float | None = None
    confidence: float | None = None
    content: str | None = None
    category: str | None = None
    confidence_level: str | None = None
    action_recommendation: str | None = None
    entry_conditions: list[str] | None = None


@dataclass
class MarketSnapshot:
    market_id: str
    venue: Venue
    market_prob: float
    spread_bps: float
    depth_usd: float
    volume_24h_usd: float
    time_to_resolution_hours: float
    updated_at: datetime
    ambiguity_score: float = 0.0
    volatility_entropy_score: float = 0.0
    question: str | None = None  # human-readable market title; set by adapters


@dataclass
class PortfolioState:
    bankroll_usd: float
    daily_drawdown_pct: float = 0.0
    theme_exposure_pct: dict[str, float] = field(default_factory=dict)


@dataclass
class RiskPolicy:
    max_position_pct_bankroll: float = 0.03
    max_theme_exposure_pct: float = 0.20
    max_daily_drawdown_pct: float = 0.05
    min_confidence: float = 0.40  # lowered from 0.50 to qualify more markets
    max_spread_bps: float = 350  # widened from 260 to allow thinner markets
    min_depth_usd: float = 250  # lowered from 500 to include smaller markets
    min_time_to_resolution_hours: float = 1.0
    max_ambiguity_score: float = (
        0.70  # increased from 0.60 to accept more ambiguous markets
    )
    max_volatility_entropy_score: float = (
        0.90  # increased from 0.85 to accept more volatile markets
    )


@dataclass
class Recommendation:
    market_id: str
    venue: Venue
    timestamp: datetime
    market_prob: float
    agent_prob: float
    uncertainty_band: tuple[float, float]
    edge: float
    ev_gross: float
    fees: float
    slippage_cost: float
    impact_cost: float
    resolution_risk_haircut: float
    ev_net: float
    confidence: float
    action: str
    entry_range: tuple[float, float]
    max_position_pct_bankroll: float
    thesis: list[str]
    disconfirming_evidence: list[str]
    invalidation: list[str]
    qualification_state: QualificationState
    reject_reason_codes: list[str] = field(default_factory=list)
    requires_approval: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["venue"] = self.venue.value
        payload["timestamp"] = self.timestamp.isoformat()
        payload["qualification_state"] = self.qualification_state.value
        return payload
