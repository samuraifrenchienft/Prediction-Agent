from unittest.mock import patch

from edge_agent import Catalyst, EdgeScanner, JupiterAdapter, KalshiAdapter, PolymarketAdapter, Venue
from edge_agent.presets import PREDICTION_MARKET_DNA


def _mock_catalysts() -> list[Catalyst]:
    return [Catalyst(source="mock_feed", quality=0.85, direction=0.1, confidence=0.9)]


class TestEdgeScanner:
    def test_scanner_collects_from_all_adapters(self) -> None:
        with patch("edge_agent.scanner.CatalystDetectionEngine") as MockEngine:
            MockEngine.return_value.detect_catalysts.return_value = _mock_catalysts()
            scanner = EdgeScanner(adapters=[JupiterAdapter(), KalshiAdapter(), PolymarketAdapter()])
            collected = scanner.collect()

        assert len(collected) == 3
        venues = {snapshot.venue for snapshot, _, _ in collected}
        assert venues == {Venue.JUPITER_PREDICTION, Venue.KALSHI, Venue.POLYMARKET}

    def test_adapter_payload_shape(self) -> None:
        with patch("edge_agent.scanner.CatalystDetectionEngine") as MockEngine:
            MockEngine.return_value.detect_catalysts.return_value = _mock_catalysts()
            scanner = EdgeScanner(adapters=[JupiterAdapter()])
            (snapshot, catalysts, theme) = scanner.collect()[0]

        assert snapshot.market_id
        assert isinstance(catalysts, list)
        assert theme in {"sports", "macro", "politics"}

    def test_scanner_passes_strategy_dna_to_catalyst_engine(self) -> None:
        with patch("edge_agent.scanner.CatalystDetectionEngine") as MockEngine:
            MockEngine.return_value.detect_catalysts.return_value = []
            EdgeScanner(
                adapters=[JupiterAdapter()],
                brand_dna=PREDICTION_MARKET_DNA,
            )
        MockEngine.assert_called_once_with(strategy_dna=PREDICTION_MARKET_DNA.strategy)

    def test_scanner_without_brand_dna_passes_none(self) -> None:
        with patch("edge_agent.scanner.CatalystDetectionEngine") as MockEngine:
            MockEngine.return_value.detect_catalysts.return_value = []
            EdgeScanner(adapters=[JupiterAdapter()])
        MockEngine.assert_called_once_with(strategy_dna=None)

    def test_catalysts_shared_across_all_markets(self) -> None:
        catalysts = _mock_catalysts()
        with patch("edge_agent.scanner.CatalystDetectionEngine") as MockEngine:
            MockEngine.return_value.detect_catalysts.return_value = catalysts
            scanner = EdgeScanner(adapters=[JupiterAdapter(), KalshiAdapter()])
            collected = scanner.collect()

        for _, market_catalysts, _ in collected:
            assert market_catalysts is catalysts
