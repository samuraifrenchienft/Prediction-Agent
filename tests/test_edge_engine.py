from datetime import datetime, timezone

from edge_agent import (
    Catalyst,
    EdgeEngine,
    MarketSnapshot,
    PortfolioState,
    QualificationState,
    Venue,
)


def test_qualified_market_emits_buy_yes() -> None:
    engine = EdgeEngine()
    snapshot = MarketSnapshot(
        market_id="m1",
        venue=Venue.JUPITER_PREDICTION,
        market_prob=0.40,
        spread_bps=100,
        depth_usd=10000,
        volume_24h_usd=50000,
        time_to_resolution_hours=48,
        updated_at=datetime.now(timezone.utc),
    )
    catalysts = [Catalyst(source="official", quality=0.9, direction=0.05, confidence=0.9)]
    portfolio = PortfolioState(bankroll_usd=10000)

    rec = engine.evaluate_market(snapshot, catalysts, portfolio, theme="sports")

    assert rec.qualification_state == QualificationState.QUALIFIED
    assert rec.action == "BUY_YES"
    assert rec.max_position_pct_bankroll > 0


def test_low_depth_moves_to_watchlist() -> None:
    engine = EdgeEngine()
    snapshot = MarketSnapshot(
        market_id="m2",
        venue=Venue.POLYMARKET,
        market_prob=0.40,
        spread_bps=180,
        depth_usd=200,
        volume_24h_usd=2000,
        time_to_resolution_hours=24,
        updated_at=datetime.now(timezone.utc),
    )
    catalysts = [Catalyst(source="official", quality=0.8, direction=0.08, confidence=0.8)]
    portfolio = PortfolioState(bankroll_usd=10000)

    rec = engine.evaluate_market(snapshot, catalysts, portfolio, theme="politics")

    assert rec.qualification_state == QualificationState.WATCHLIST
    assert rec.action == "HOLD"
    assert "LOW_DEPTH" in rec.reject_reason_codes
    assert len(engine.watchlist.list_entries()) == 1


def test_ambiguity_rejects_market() -> None:
    engine = EdgeEngine()
    snapshot = MarketSnapshot(
        market_id="m3",
        venue=Venue.KALSHI,
        market_prob=0.52,
        spread_bps=100,
        depth_usd=10000,
        volume_24h_usd=50000,
        time_to_resolution_hours=48,
        updated_at=datetime.now(timezone.utc),
        ambiguity_score=0.9,
    )
    catalysts = [Catalyst(source="official", quality=0.9, direction=0.03, confidence=0.9)]
    portfolio = PortfolioState(bankroll_usd=10000)

    rec = engine.evaluate_market(snapshot, catalysts, portfolio, theme="macro")

    assert rec.qualification_state == QualificationState.REJECTED
    assert "AMBIGUITY_RISK" in rec.reject_reason_codes
    assert not engine.watchlist.list_entries()


def test_jupiter_fee_is_higher_than_kalshi_fee() -> None:
    engine = EdgeEngine()
    base_kwargs = dict(
        market_prob=0.40,
        spread_bps=80,
        depth_usd=12000,
        volume_24h_usd=50000,
        time_to_resolution_hours=48,
        updated_at=datetime.now(timezone.utc),
    )
    catalysts = [Catalyst(source="official", quality=0.9, direction=0.05, confidence=0.9)]
    portfolio = PortfolioState(bankroll_usd=10000)

    jupiter = engine.evaluate_market(
        MarketSnapshot(market_id="j", venue=Venue.JUPITER_PREDICTION, **base_kwargs),
        catalysts,
        portfolio,
        theme="sports",
    )
    kalshi = engine.evaluate_market(
        MarketSnapshot(market_id="k", venue=Venue.KALSHI, **base_kwargs),
        catalysts,
        portfolio,
        theme="sports",
    )

    assert jupiter.fees > kalshi.fees


def test_batch_and_repository_top_opportunities() -> None:
    engine = EdgeEngine()
    portfolio = PortfolioState(bankroll_usd=10000)

    batch = [
        (
            MarketSnapshot(
                market_id="a",
                venue=Venue.JUPITER_PREDICTION,
                market_prob=0.40,
                spread_bps=100,
                depth_usd=10000,
                volume_24h_usd=50000,
                time_to_resolution_hours=48,
                updated_at=datetime.now(timezone.utc),
            ),
            [Catalyst(source="official", quality=0.9, direction=0.06, confidence=0.9)],
            "sports",
        ),
        (
            MarketSnapshot(
                market_id="b",
                venue=Venue.KALSHI,
                market_prob=0.50,
                spread_bps=90,
                depth_usd=12000,
                volume_24h_usd=90000,
                time_to_resolution_hours=48,
                updated_at=datetime.now(timezone.utc),
            ),
            [Catalyst(source="official", quality=0.9, direction=0.04, confidence=0.9)],
            "macro",
        ),
    ]

    recommendations = engine.evaluate_batch(batch, portfolio=portfolio)
    assert len(recommendations) == 2
    assert recommendations[0].qualification_state == QualificationState.QUALIFIED

    top = engine.top_opportunities(limit=1)
    assert len(top) == 1
    assert top[0].qualification_state == QualificationState.QUALIFIED

    reason_counts = engine.repository.reject_reason_counts()
    assert isinstance(reason_counts, dict)
