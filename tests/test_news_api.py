"""Unit tests for NewsAPIClient."""
from unittest.mock import MagicMock, patch

import pytest
import requests


def _make_client(api_key="test-key"):
    """Helper that creates a NewsAPIClient with the given key set."""
    with patch.dict("os.environ", {"NEWS_API_KEY": api_key}), patch("dotenv.load_dotenv"):
        import importlib
        news_api = importlib.import_module("edge_agent.dat-ingestion.news_api")
        importlib.reload(news_api)
        return news_api.NewsAPIClient()


class TestNewsAPIClient:
    def test_get_top_headlines_success(self):
        mock_articles = [
            {"title": "Market rallies on Fed news", "source": {"name": "Reuters"}},
            {"title": "Tech stocks surge", "source": {"name": "Bloomberg"}},
        ]
        mock_response = MagicMock()
        mock_response.json.return_value = {"articles": mock_articles}
        mock_response.raise_for_status.return_value = None

        client = _make_client()
        with patch("requests.get", return_value=mock_response) as mock_get:
            articles = client.get_top_headlines("technology", page_size=2)

        assert articles == mock_articles
        mock_get.assert_called_once()
        call_params = mock_get.call_args[1]["params"]
        assert call_params["q"] == "technology"
        assert call_params["pageSize"] == 2

    def test_get_top_headlines_empty_response(self):
        mock_response = MagicMock()
        mock_response.json.return_value = {}
        mock_response.raise_for_status.return_value = None

        client = _make_client()
        with patch("requests.get", return_value=mock_response):
            articles = client.get_top_headlines("xyz_no_results")

        assert articles == []

    def test_get_top_headlines_http_error(self):
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = requests.HTTPError("401 Unauthorized")

        client = _make_client()
        with patch("requests.get", return_value=mock_response):
            with pytest.raises(requests.HTTPError):
                client.get_top_headlines("test")

    def test_missing_api_key_raises(self):
        with patch.dict("os.environ", {}, clear=True), patch("dotenv.load_dotenv"):
            import importlib
            news_api = importlib.import_module("edge_agent.dat-ingestion.news_api")
            importlib.reload(news_api)
            with pytest.raises(ValueError, match="NEWS_API_KEY"):
                news_api.NewsAPIClient()
