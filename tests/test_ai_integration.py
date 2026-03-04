from unittest.mock import patch
from edge_agent.models import MarketSnapshot, Catalyst, Venue
from edge_agent.nodes import probability_node
from datetime import datetime

def test_probability_node_with_ai():
    """Tests that the probability_node uses the AI's response."""
    # Create a mock market snapshot and catalysts
    snapshot = MarketSnapshot(
        market_id="test_market",
        venue=Venue.KALSHI,
        market_prob=0.5,
        spread_bps=100,
        depth_usd=10000,
        volume_24h_usd=100000,
        time_to_resolution_hours=24,
        updated_at=datetime.now(),
    )
    catalysts = [
        Catalyst(source="test_source", quality=0.8, direction=1, confidence=0.9)
    ]

    # Mock the AI's response — get_ai_response now returns a dict
    with patch("edge_agent.nodes.get_ai_response") as mock_get_ai_response:
        mock_get_ai_response.return_value = {
            "p_true": 0.75,
            "bull_thesis": ["Strong momentum"],
            "disconfirming_evidence": [],
        }

        # Call the probability_node
        result = probability_node(snapshot, catalysts)

        # Assert that the agent's probability is the same as the AI's response
        assert result.p_true == 0.75
        print("test_probability_node_with_ai passed!")

if __name__ == "__main__":
    test_probability_node_with_ai()