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


@dataclass
class Catalyst:
    source: str
    quality: float
    direction: float
    confidence: float


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
    min_confidence: float = 0.70
    max_spread_bps: float = 220
    min_depth_usd: float = 1500
    min_time_to_resolution_hours: float = 1.0
    max_ambiguity_score: float = 0.55
    max_volatility_entropy_score: float = 0.80


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
