from __future__ import annotations

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

    Proposal-first — every returned recommendation has requires_approval=True
    and no live trading side effects.
    """

    def __init__(
        self,
        risk_policy: RiskPolicy | None = None,
        watchlist: WatchlistStore | None = None,
        repository: RecommendationRepository | None = None,
        game_tracker: GameTracker | None = None,
    ) -> None:
        self.risk_policy = risk_policy or RiskPolicy()
        self.watchlist = watchlist or WatchlistStore()
        self.repository = repository or RecommendationRepository()
        self.game_tracker = game_tracker or GameTracker()

    def evaluate_market(
        self,
        snapshot: MarketSnapshot,
        catalysts: list[Catalyst],
        portfolio: PortfolioState,
        theme: str,
    ) -> Recommendation:
        # Enrich snapshot with tracker data (opening_prob, etc.) if game is tracked.
        snapshot = self.game_tracker.enrich_snapshot(snapshot)

        # Check if a live tracked game fires INJURY_MOMENTUM_REVERSAL.
        # Runs before probability_node so signal propagates into the recommendation.
        tracker_signal = self.game_tracker.update(snapshot)
        if tracker_signal == SignalType.INJURY_MOMENTUM_REVERSAL and snapshot.opening_prob == 0.0:
            tracked = self.game_tracker.get_game(snapshot.venue, snapshot.market_id)
            snapshot.opening_prob = tracked.pre_game_market_prob if tracked else 0.5

        # Probability estimation — pure catalyst math, zero AI calls
        prob = probability_node(snapshot, catalysts)

        # Live game tracker overrides the catalyst-based signal when confirmed
        if tracker_signal == SignalType.INJURY_MOMENTUM_REVERSAL:
            prob.signal = SignalType.INJURY_MOMENTUM_REVERSAL

        # Register pre-game injury markets for live monitoring once they go live
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

    def game_tracker_summary(self) -> str:
        active = self.game_tracker.active_games()
        triggered = self.game_tracker.triggered_games()
        if not active:
            return (
                "🏈 Injury tracker idle — will activate when a game market goes live "
                "with a star player Out/Doubtful on one side."
            )
        lines = [f"Injury tracker: {len(active)} active, {len(triggered)} triggered"]
        for g in active[:5]:
            drop = g.current_drop
            lines.append(
                f"  {'🔥' if g.triggered else '👁'} {g.question[:50]} "
                f"({g.last_market_prob:.0%}, drop {drop:+.1%})"
            )
        return "\n".join(lines)
