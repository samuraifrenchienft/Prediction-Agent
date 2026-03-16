from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from .models import Catalyst, MarketSnapshot, Venue
from .nodes import SignalType


# ---------------------------------------------------------------------------
# Game phase estimation
# ---------------------------------------------------------------------------

class GamePhase(str, Enum):
    PRE_GAME = "PRE_GAME"    # TTR > 5h — game hasn't started
    LIVE_Q1  = "LIVE_Q1"    # TTR 3–5h  (approx. first quarter / first period)
    LIVE_Q2  = "LIVE_Q2"    # TTR 1.5–3h (second quarter — main trigger window)
    LIVE_Q3  = "LIVE_Q3"    # TTR 0.75–1.5h (aggressive escalation)
    LIVE_Q4  = "LIVE_Q4"    # TTR < 0.75h (final phase)
    COMPLETE = "COMPLETE"    # Resolved / TTR ≤ 0


def _estimate_phase(ttr_hours: float) -> GamePhase:
    """Estimate game quarter from time-to-resolution.

    Uses approximate TTR ranges that work across major sports:
      NBA (~2.5h), NFL (~3h), NHL (~3h), MLB (~3h).
    These are best-effort — live price movement is the real signal.
    """
    if ttr_hours > 5.0:
        return GamePhase.PRE_GAME
    if ttr_hours > 3.0:
        return GamePhase.LIVE_Q1
    if ttr_hours > 1.5:
        return GamePhase.LIVE_Q2
    if ttr_hours > 0.75:
        return GamePhase.LIVE_Q3
    if ttr_hours > 0.0:
        return GamePhase.LIVE_Q4
    return GamePhase.COMPLETE


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class TrackedGame:
    market_id: str
    venue: Venue
    question: str
    theme: str

    # Reference probability set at pre-game registration
    pre_game_market_prob: float
    # snapshot.opening_prob at registration (0 = unknown, will fall back to pre_game_market_prob)
    pre_game_opening_prob: float

    # Catalyst sources that contained injury keywords
    injury_catalysts: list[str]
    registered_at: datetime

    phase: GamePhase = GamePhase.PRE_GAME
    last_market_prob: float = 0.0
    last_updated: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # How this game was added to the tracker:
    #   "pre_game_lag"     — PRE_GAME_INJURY_LAG signal fired (market underpriced injury)
    #   "proactive_injury" — star player Out/Doubtful detected in injury cache
    registration_type: str = "pre_game_lag"

    # Trigger state — set once when INJURY_MOMENTUM_REVERSAL condition is met
    triggered: bool = False
    trigger_prob: float = 0.0          # market_prob at the moment trigger fired
    trigger_phase: GamePhase | None = None

    @property
    def reference_prob(self) -> float:
        """Best available opening probability for this game."""
        return self.pre_game_opening_prob if self.pre_game_opening_prob > 0 else self.pre_game_market_prob

    @property
    def current_drop(self) -> float:
        """Current price drop from pre-game reference (positive = team is losing)."""
        return self.reference_prob - self.last_market_prob

    @property
    def current_surge(self) -> float:
        """Current price surge from pre-game reference (positive = team pulling ahead).

        Opposite of current_drop. Useful for detecting when the healthy/favored team
        is running away (injured team collapsing) — a "confirm & hold" or "too late" signal.
        """
        return self.last_market_prob - self.reference_prob


# ---------------------------------------------------------------------------
# GameTracker
# ---------------------------------------------------------------------------

# Injury keywords used to extract injury-specific catalyst sources
_INJURY_KW = [
    "out", "injured", "injury", "dnp", "doubtful", "ruled out",
    "scratch", "scratched", "sidelined", "unavailable", "concussion",
    "sprain", "fracture", "hamstring", "ankle", "suspension",
]

# Price-drop thresholds from pre-game probability (in probability points)
# These are intentionally lower than the default 10pp in _classify_signal
# because we already have pre-game injury confirmation.
_Q1_TRIGGER_DROP = 0.12   # Q1: 12pp drop — must be a clear blowout to fire this early
_Q2_TRIGGER_DROP = 0.08   # Q2: 8pp drop fires the alert
_Q3_TRIGGER_DROP = 0.06   # Q3/Q4: 6pp drop (more aggressive — less time to act)


class GameTracker:
    """
    Tracks sports games with pre-identified injuries across the full game lifecycle.

    Flow:
      1. PRE_GAME (TTR 5–48h): PRE_GAME_INJURY_LAG signal fires → game is registered
         with the current market prob as the opening reference.

      2. LIVE_Q1 (TTR 3–5h): Game goes live. Tracker monitors the price.

      3. LIVE_Q2 (TTR 1.5–3h): Tracking becomes aggressive.
         TRIGGER FIRES if the healthy/full-roster team's price drops ≥ 8pp
         from the pre-game reference (team is losing in Q2).

      4. LIVE_Q3/Q4 (TTR < 1.5h): Even more aggressive threshold (6pp).
         Trigger can still fire if not yet triggered in Q2.

    The trigger fires SignalType.INJURY_MOMENTUM_REVERSAL so downstream nodes
    can generate a high-priority recommendation.
    """

    def __init__(self) -> None:
        self._games: dict[str, TrackedGame] = {}

    def _key(self, venue: Venue, market_id: str) -> str:
        return f"{venue.value}:{market_id}"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register(
        self,
        snapshot: MarketSnapshot,
        catalysts: list,          # list[Catalyst] OR list[str] for proactive registrations
        theme: str,
        registration_type: str = "pre_game_lag",
    ) -> None:
        """Register a game for injury monitoring.

        Two registration paths:
          "pre_game_lag"     — PRE_GAME_INJURY_LAG signal fired; catalysts are Catalyst objects.
          "proactive_injury" — star player Out/Doubtful in injury cache; catalysts are strings.

        Does nothing if the game is already being tracked.
        """
        key = self._key(snapshot.venue, snapshot.market_id)
        if key in self._games:
            return

        # Extract injury source strings regardless of whether catalysts are
        # Catalyst objects (news pipeline) or plain strings (injury-cache path).
        injury_sources: list[str] = []
        for c in catalysts:
            src = c.source if hasattr(c, "source") else str(c)
            if any(kw in src.lower() for kw in _INJURY_KW):
                injury_sources.append(src)
        if not injury_sources:
            injury_sources = [f"(injury detected — {registration_type})"]

        game = TrackedGame(
            market_id=snapshot.market_id,
            venue=snapshot.venue,
            question=snapshot.question,
            theme=theme,
            pre_game_market_prob=snapshot.market_prob,
            pre_game_opening_prob=snapshot.opening_prob,
            injury_catalysts=injury_sources,
            registered_at=datetime.now(timezone.utc),
            last_market_prob=snapshot.market_prob,
            registration_type=registration_type,
        )
        self._games[key] = game

        tag = "📊 PRE-GAME LAG" if registration_type == "pre_game_lag" else "👁 PROACTIVE INJURY"
        print(
            f"[GameTracker] +{tag} '{snapshot.question[:65]}' | "
            f"pre-game={snapshot.market_prob:.1%} | "
            f"sources: {len(injury_sources)}"
        )

    def update(self, snapshot: MarketSnapshot) -> SignalType | None:
        """Update a tracked game; returns INJURY_MOMENTUM_REVERSAL if trigger fires.

        Call this each scan cycle for every tracked game, even if the market
        wasn't returned by the scanner this cycle (re-use last known snapshot
        or re-fetch from adapter). Returns None if no trigger condition is met.
        """
        key = self._key(snapshot.venue, snapshot.market_id)
        game = self._games.get(key)
        if not game:
            return None

        phase = _estimate_phase(snapshot.time_to_resolution_hours)
        game.phase = phase
        game.last_market_prob = snapshot.market_prob
        game.last_updated = datetime.now(timezone.utc)

        # Clean up completed / resolved games
        if phase == GamePhase.COMPLETE:
            self._games.pop(key, None)
            return None

        # Don't re-trigger an already-triggered game
        if game.triggered:
            return None

        # Check trigger during live phases Q1 and later
        if phase not in (GamePhase.LIVE_Q1, GamePhase.LIVE_Q2, GamePhase.LIVE_Q3, GamePhase.LIVE_Q4):
            return None

        if phase == GamePhase.LIVE_Q1:
            threshold = _Q1_TRIGGER_DROP   # 12pp — clear blowout only
        elif phase == GamePhase.LIVE_Q2:
            threshold = _Q2_TRIGGER_DROP   # 8pp — main trigger window
        else:
            threshold = _Q3_TRIGGER_DROP   # 6pp — aggressive late-game
        drop = game.current_drop

        if drop >= threshold:
            game.triggered = True
            game.trigger_prob = snapshot.market_prob
            game.trigger_phase = phase
            print(
                f"[GameTracker] *** TRIGGER *** '{snapshot.question[:60]}' | "
                f"phase={phase.value} | "
                f"pre-game={game.reference_prob:.1%} → now={snapshot.market_prob:.1%} | "
                f"drop={drop:.1%} ≥ threshold={threshold:.1%}"
            )
            return SignalType.INJURY_MOMENTUM_REVERSAL

        return None

    def enrich_snapshot(self, snapshot: MarketSnapshot) -> MarketSnapshot:
        """Patch snapshot.opening_prob from tracker data if not already set.

        This ensures _classify_signal() in nodes.py has a valid opening reference
        even when the market API doesn't provide historical price data.
        """
        key = self._key(snapshot.venue, snapshot.market_id)
        game = self._games.get(key)
        if game and snapshot.opening_prob == 0.0:
            snapshot.opening_prob = game.pre_game_market_prob
        return snapshot

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def active_games(self) -> list[TrackedGame]:
        return list(self._games.values())

    def triggered_games(self) -> list[TrackedGame]:
        return [g for g in self._games.values() if g.triggered]

    def get_game(self, venue: Venue, market_id: str) -> TrackedGame | None:
        return self._games.get(self._key(venue, market_id))

    def summary(self) -> str:
        games = self.active_games()
        triggered = self.triggered_games()
        lines = [
            f"[GameTracker] {len(games)} tracked | {len(triggered)} triggered"
        ]
        for g in games:
            drop  = g.current_drop
            surge = g.current_surge
            if g.triggered:
                flag = "*** TRIGGERED ***"
            elif drop > 0:
                flag = f"drop={drop:+.1%}"      # team losing — buy window forming
            elif surge > 0.02:
                flag = f"surge={surge:+.1%}"     # team pulling ahead — confirm/hold
            else:
                flag = f"Δ={-drop:+.1%}"         # near-flat
            type_badge = "📌" if g.registration_type == "pre_game_lag" else "👁"
            lines.append(
                f"  {'🔥' if g.triggered else type_badge} [{g.phase.value:10}] "
                f"{g.question[:55]:<55} | ref={g.reference_prob:.1%} → {g.last_market_prob:.1%} | {flag}"
            )
        return "\n".join(lines)
