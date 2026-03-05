from __future__ import annotations

from .cross_market import CrossMarketCorrelator
from .game_tracker import GameTracker
from .models import Catalyst, MarketSnapshot, PortfolioState, Recommendation, RiskPolicy
from .nodes import (
    SignalType,
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
        game_tracker: GameTracker | None = None,
        cross_market_correlator: CrossMarketCorrelator | None = None,
    ) -> None:
        self.risk_policy = risk_policy or RiskPolicy()
        self.watchlist = watchlist or WatchlistStore()
        self.repository = repository or RecommendationRepository()
        self.game_tracker = game_tracker or GameTracker()
        self.cross_market_correlator = cross_market_correlator or CrossMarketCorrelator()

    def evaluate_market(
        self,
        snapshot: MarketSnapshot,
        catalysts: list[Catalyst],
        portfolio: PortfolioState,
        theme: str,
    ) -> Recommendation:
        # If this market is already being tracked (pre-game injury registered),
        # patch opening_prob from tracker data so _classify_signal has a valid reference.
        snapshot = self.game_tracker.enrich_snapshot(snapshot)

        # Check if this live game should fire a tracker-based trigger (Q2 condition).
        # This runs before probability_node so the signal propagates correctly.
        tracker_signal = self.game_tracker.update(snapshot)
        if tracker_signal == SignalType.INJURY_MOMENTUM_REVERSAL and snapshot.opening_prob == 0.0:
            # Ensure opening_prob is set so _classify_signal confirms the signal
            snapshot.opening_prob = self.game_tracker.get_game(
                snapshot.venue, snapshot.market_id
            ).pre_game_market_prob if self.game_tracker.get_game(snapshot.venue, snapshot.market_id) else 0.5

        prob = probability_node(snapshot, catalysts)

        # If the AI confirmed a PRE_GAME_INJURY_LAG signal, register the game
        # in the tracker so we actively monitor it once it goes live.
        if prob.signal == SignalType.PRE_GAME_INJURY_LAG:
            self.game_tracker.register(snapshot, catalysts, theme)

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
        # Pre-process: inject cross-market correlation catalysts where applicable
        inputs = self.cross_market_correlator.enrich_batch(inputs)

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
