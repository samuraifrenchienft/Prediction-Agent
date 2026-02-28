from __future__ import annotations

from .models import Catalyst, MarketSnapshot, PortfolioState, Recommendation, RiskPolicy
from .nodes import (
    edge_ev_node,
    probability_node,
    qualification_gate,
    recommendation_node,
    risk_policy_node,
)
from .repository import RecommendationRepository
from .watchlist import WatchlistStore


class EdgeEngine:
    """Reference implementation of EDGE proposal flow.

    This is intentionally proposal-first. Every returned recommendation has
    requires_approval=True and no live trading side effects.
    """

    def __init__(
        self,
        risk_policy: RiskPolicy | None = None,
        watchlist: WatchlistStore | None = None,
        repository: RecommendationRepository | None = None,
    ) -> None:
        self.risk_policy = risk_policy or RiskPolicy()
        self.watchlist = watchlist or WatchlistStore()
        self.repository = repository or RecommendationRepository()

    def evaluate_market(
        self,
        snapshot: MarketSnapshot,
        catalysts: list[Catalyst],
        portfolio: PortfolioState,
        theme: str,
    ) -> Recommendation:
        prob = probability_node(snapshot, catalysts)
        ev = edge_ev_node(snapshot, prob.p_true)
        qualification_state, gate_reasons = qualification_gate(snapshot, prob, ev, self.risk_policy)
        capped_size, policy_reasons = risk_policy_node(
            qualification_state=qualification_state,
            portfolio=portfolio,
            policy=self.risk_policy,
            theme=theme,
        )

        recommendation = recommendation_node(
            snapshot=snapshot,
            prob=prob,
            ev=ev,
            qualification_state=qualification_state,
            reject_reasons=gate_reasons,
            capped_size=capped_size,
            policy_reasons=policy_reasons,
        )
        self.watchlist.update(recommendation)
        self.repository.add(recommendation)
        return recommendation

    def evaluate_batch(
        self,
        inputs: list[tuple[MarketSnapshot, list[Catalyst], str]],
        portfolio: PortfolioState,
    ) -> list[Recommendation]:
        recommendations = [
            self.evaluate_market(snapshot=snapshot, catalysts=catalysts, portfolio=portfolio, theme=theme)
            for snapshot, catalysts, theme in inputs
        ]
        return sorted(
            recommendations,
            key=lambda rec: (rec.qualification_state.value != "qualified", -(rec.ev_net * rec.confidence)),
        )

    def top_opportunities(self, limit: int = 3) -> list[Recommendation]:
        return self.repository.top_opportunities(limit=limit)
