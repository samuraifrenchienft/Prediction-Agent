from unittest.mock import patch
from datetime import datetime, timezone

from edge_agent import Catalyst, EdgeScanner, KalshiAdapter, PolymarketAdapter, Venue
from edge_agent.adapters import AdapterMarket
from edge_agent.models import MarketSnapshot


def _mock_catalysts() -> list[Catalyst]:
    return [Catalyst(source="mock_feed", quality=0.85, direction=0.1, confidence=0.9)]


def _make_market(market_id: str, venue: Venue, theme: str) -> AdapterMarket:
    snap = MarketSnapshot(
        market_id=market_id,
        venue=venue,
        market_prob=0.50,
        spread_bps=80,
        depth_usd=10000,
        volume_24h_usd=50000,
        time_to_resolution_hours=24,
        updated_at=datetime.now(timezone.utc),
    )
    return AdapterMarket(snapshot=snap, catalysts=[], theme=theme)


class TestEdgeScanner:
    def test_collect_with_injected_markets_and_catalysts(self) -> None:
        """When both markets and catalysts are injected, scanner passes them through."""
        cats = _mock_catalysts()
        market = _make_market("m1", Venue.KALSHI, "macro")

        with patch("edge_agent.scanner.CatalystDetectionEngine"), \
             patch("edge_agent.scanner.InjuryAPIClient"):
            scanner = EdgeScanner(adapters=[])
            collected = scanner.collect(markets=[market], catalysts=cats)

        assert len(collected) == 1
        snapshot, result_cats, theme = collected[0]
        assert snapshot.market_id == "m1"
        assert isinstance(result_cats, list)
        assert theme == "macro"

    def test_collect_result_is_three_tuple(self) -> None:
        """Each collected item must be (MarketSnapshot, list[Catalyst], str)."""
        market = _make_market("shape-test", Venue.POLYMARKET, "politics")

        with patch("edge_agent.scanner.CatalystDetectionEngine") as MockEngine, \
             patch("edge_agent.scanner.InjuryAPIClient"):
            MockEngine.return_value.detect_catalysts.return_value = _mock_catalysts()
            scanner = EdgeScanner(adapters=[])
            collected = scanner.collect(markets=[market])

        assert len(collected) == 1
        snapshot, catalysts, theme = collected[0]
        assert snapshot.market_id == "shape-test"
        assert isinstance(catalysts, list)
        assert theme == "politics"

    def test_sports_theme_gets_injury_catalysts_attempted(self) -> None:
        """Sports markets should trigger injury catalyst injection."""
        market = _make_market("nba-game", Venue.KALSHI, "sports")
        market.snapshot.question = "Will the Lakers win tonight?"

        with patch("edge_agent.scanner.CatalystDetectionEngine") as MockEngine, \
             patch("edge_agent.scanner.InjuryAPIClient") as MockInjury:
            MockEngine.return_value.detect_catalysts.return_value = []
            MockInjury.return_value.build_injury_catalysts.return_value = []
            scanner = EdgeScanner(adapters=[])
            collected = scanner.collect(markets=[market])

        assert len(collected) == 1
        # Injury client should have been called for a sports market
        MockInjury.return_value.build_injury_catalysts.assert_called_once()

    def test_non_sports_theme_skips_injury_client(self) -> None:
        """Non-sports markets must not call the injury API."""
        market = _make_market("fed-rate", Venue.KALSHI, "macro")

        with patch("edge_agent.scanner.CatalystDetectionEngine") as MockEngine, \
             patch("edge_agent.scanner.InjuryAPIClient") as MockInjury:
            MockEngine.return_value.detect_catalysts.return_value = []
            scanner = EdgeScanner(adapters=[])
            scanner.collect(markets=[market])

        MockInjury.return_value.build_injury_catalysts.assert_not_called()
