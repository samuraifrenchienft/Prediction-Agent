from datetime import datetime, timezone

from edge_agent import Catalyst, EdgeReporter, EdgeService, MarketSnapshot, PortfolioState, QualificationState, Venue


def _input(market_id: str, venue: Venue, prob: float, depth: float, spread: float, direction: float):
    return (
        MarketSnapshot(
            market_id=market_id,
            venue=venue,
            market_prob=prob,
            spread_bps=spread,
            depth_usd=depth,
            volume_24h_usd=50000,
            time_to_resolution_hours=48,
            updated_at=datetime.now(timezone.utc),
        ),
        [Catalyst(source="official", quality=0.9, direction=direction, confidence=0.9)],
        "sports",
    )


def test_service_run_scan_returns_summary_and_watchlist() -> None:
    service = EdgeService()
    portfolio = PortfolioState(bankroll_usd=10000)

    inputs = [
        _input("good", Venue.KALSHI, 0.40, 12000, 80, 0.06),
        _input("thin", Venue.POLYMARKET, 0.40, 200, 150, 0.08),
    ]

    recs, summary = service.run_scan(inputs=inputs, portfolio=portfolio)

    assert len(recs) == 2
    assert summary.total_markets == 2
    assert summary.qualified >= 1
    assert summary.watchlist >= 1
    assert summary.venue_counts.get(Venue.KALSHI.value, 0) >= 1

    watch = service.list_watchlist()
    assert len(watch) >= 1
    assert watch[0]["state"] == QualificationState.WATCHLIST.value


def test_recommendation_to_dict_has_serialized_fields() -> None:
    service = EdgeService()
    portfolio = PortfolioState(bankroll_usd=10000)
    recs, _ = service.run_scan(inputs=[_input("good", Venue.KALSHI, 0.41, 10000, 90, 0.05)], portfolio=portfolio)

    payload = recs[0].to_dict()
    assert payload["venue"] == Venue.KALSHI.value
    assert payload["qualification_state"] in {"qualified", "watchlist", "rejected"}
    assert isinstance(payload["timestamp"], str)


def test_reporter_builds_dashboard_payload() -> None:
    service = EdgeService()
    reporter = EdgeReporter(service=service)
    portfolio = PortfolioState(bankroll_usd=10000)
    service.run_scan(inputs=[_input("good", Venue.KALSHI, 0.40, 12000, 80, 0.06)], portfolio=portfolio)

    dashboard = reporter.build_dashboard(top_n=1)

    assert dashboard.summary["total_markets"] == 1
    assert isinstance(dashboard.top_opportunities, list)
    assert isinstance(dashboard.watchlist, list)
