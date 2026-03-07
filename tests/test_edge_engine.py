from datetime import datetime, timezone

from edge_agent import (
    Catalyst,
    EdgeEngine,
    MarketSnapshot,
    PortfolioState,
    QualificationState,
    Venue,
)


def _cat(direction: float = 0.05, quality: float = 0.9, confidence: float = 0.9) -> list[Catalyst]:
    return [Catalyst(source="official", quality=quality, direction=direction, confidence=confidence)]


def _snap(
    market_id: str = "m1",
    venue: Venue = Venue.KALSHI,
    prob: float = 0.40,
    spread: float = 100,
    depth: float = 10000,
    ttr: float = 48,
    ambiguity: float = 0.0,
) -> MarketSnapshot:
    return MarketSnapshot(
        market_id=market_id,
        venue=venue,
        market_prob=prob,
        spread_bps=spread,
        depth_usd=depth,
        volume_24h_usd=50000,
        time_to_resolution_hours=ttr,
        updated_at=datetime.now(timezone.utc),
        ambiguity_score=ambiguity,
    )


def test_qualified_market_emits_buy_yes() -> None:
    engine = EdgeEngine()
    rec = engine.evaluate_market(_snap(), _cat(), PortfolioState(bankroll_usd=10000), theme="sports")

    assert rec.qualification_state == QualificationState.QUALIFIED
    assert rec.action == "BUY_YES"
    assert rec.max_position_pct_bankroll > 0


def test_low_depth_moves_to_watchlist() -> None:
    engine = EdgeEngine()
    rec = engine.evaluate_market(
        _snap(market_id="m2", venue=Venue.POLYMARKET, depth=200, spread=180, ttr=24),
        _cat(direction=0.08),
        PortfolioState(bankroll_usd=10000),
        theme="politics",
    )

    assert rec.qualification_state == QualificationState.WATCHLIST
    assert rec.action == "HOLD"
    assert "LOW_DEPTH" in rec.reject_reason_codes
    assert len(engine.watchlist.list_entries()) == 1


def test_ambiguity_rejects_market() -> None:
    engine = EdgeEngine()
    rec = engine.evaluate_market(
        _snap(market_id="m3", ambiguity=0.9),
        _cat(direction=0.03),
        PortfolioState(bankroll_usd=10000),
        theme="macro",
    )

    assert rec.qualification_state == QualificationState.REJECTED
    assert "AMBIGUITY_RISK" in rec.reject_reason_codes
    assert not engine.watchlist.list_entries()


def test_polymarket_fee_is_higher_than_kalshi_fee() -> None:
    """Polymarket charges 0.45% vs Kalshi's 0.30%."""
    engine = EdgeEngine()
    portfolio = PortfolioState(bankroll_usd=10000)
    base = dict(prob=0.40, spread=80, depth=12000, ttr=48)

    poly = engine.evaluate_market(_snap(market_id="p", venue=Venue.POLYMARKET, **base), _cat(), portfolio, theme="sports")
    kalshi = engine.evaluate_market(_snap(market_id="k", venue=Venue.KALSHI, **base), _cat(), portfolio, theme="sports")

    assert poly.fees > kalshi.fees


def test_batch_and_repository_top_opportunities() -> None:
    engine = EdgeEngine()
    portfolio = PortfolioState(bankroll_usd=10000)

    batch = [
        (_snap(market_id="a", venue=Venue.KALSHI), _cat(direction=0.06), "sports"),
        (_snap(market_id="b", venue=Venue.POLYMARKET), _cat(direction=0.04), "macro"),
    ]

    recommendations = engine.evaluate_batch(batch, portfolio=portfolio)
    assert len(recommendations) == 2
    assert recommendations[0].qualification_state == QualificationState.QUALIFIED

    top = engine.top_opportunities(limit=1)
    assert len(top) == 1
    assert top[0].qualification_state == QualificationState.QUALIFIED

    reason_counts = engine.repository.reject_reason_counts()
    assert isinstance(reason_counts, dict)
