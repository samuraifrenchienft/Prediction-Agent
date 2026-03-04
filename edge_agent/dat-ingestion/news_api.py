import os
import requests
from dotenv import load_dotenv

load_dotenv()

class NewsAPIClient:
    """A client for fetching news from the NewsAPI."""

    def __init__(self):
        self.api_key = os.environ.get("NEWS_API_KEY")
        if not self.api_key:
            raise ValueError("NEWS_API_KEY not found in .env file")
        self.base_url = "https://newsapi.org/v2/everything"

    def get_top_headlines(self, query: str, page_size: int = 10) -> list[dict]:
        """Gets the top headlines for a given query."""
        params = {
            "q": query,
            "pageSize": page_size,
            "apiKey": self.api_key,
        }
        response = requests.get(self.base_url, params=params)
        response.raise_for_status()
        return response.json().get("articles", [])