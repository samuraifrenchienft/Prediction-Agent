from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

import requests
from dotenv import load_dotenv

load_dotenv()

from .models import Catalyst, MarketSnapshot, Venue


@dataclass
class AdapterMarket:
    snapshot: MarketSnapshot
    catalysts: list[Catalyst]
    theme: str
    title: str = field(default="")  # human-readable market question for news queries


class MarketAdapter(Protocol):
    venue: Venue

    def fetch_markets(self) -> list[AdapterMarket]:
        """Return normalized market candidates for EDGE evaluation."""


def _infer_theme(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ["election", "president", "congress", "senate", "vote", "poll", "democrat", "republican"]):
        return "politics"
    if any(w in t for w in ["fed", "rate", "inflation", "gdp", "cpi", "recession", "economy", "bitcoin", "crypto", "eth"]):
        return "macro"
    if any(w in t for w in ["nfl", "nba", "mlb", "nhl", "soccer", "championship", "super bowl", "playoff", "sport"]):
        return "sports"
    return "other"


class KalshiAdapter:
    """Fetches open markets from the Kalshi public REST API."""

    venue = Venue.KALSHI
    BASE_URL = "https://api.kalshi.com/trade-api/v2"

    def fetch_markets(self, limit: int = 25) -> list[AdapterMarket]:
        try:
            resp = requests.get(
                f"{self.BASE_URL}/markets",
                params={"status": "open", "limit": limit},
                timeout=10,
            )
            resp.raise_for_status()
            result = []
            for m in resp.json().get("markets", []):
                yes_bid = (m.get("yes_bid") or 50) / 100
                yes_ask = (m.get("yes_ask") or 50) / 100
                mid = (yes_bid + yes_ask) / 2
                spread_bps = round((yes_ask - yes_bid) * 10_000, 1)

                close_time_str = m.get("close_time", "")
                try:
                    close_dt = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
                    resolution_hours = max(0.0, (close_dt - datetime.now(timezone.utc)).total_seconds() / 3600)
                except Exception:
                    resolution_hours = 24.0

                title = m.get("title", m["ticker"])
                result.append(AdapterMarket(
                    snapshot=MarketSnapshot(
                        market_id=m["ticker"],
                        venue=self.venue,
                        market_prob=mid,
                        spread_bps=spread_bps,
                        depth_usd=(m.get("open_interest") or 0) / 100,
                        volume_24h_usd=(m.get("volume") or 0) / 100,
                        time_to_resolution_hours=resolution_hours,
                        updated_at=datetime.now(timezone.utc),
                        question=title,
                    ),
                    catalysts=[],
                    theme=_infer_theme(title + " " + m.get("category", "")),
                    title=title,
                ))
            print(f"[KalshiAdapter] fetched {len(result)} markets")
            return result
        except Exception as e:
            print(f"[KalshiAdapter] error: {e}")
            return []


class PolymarketAdapter:
    """Fetches active markets from the Polymarket Gamma REST API, sorted by volume."""

    venue = Venue.POLYMARKET
    BASE_URL = "https://gamma-api.polymarket.com"

    def fetch_markets(self, limit: int = 25) -> list[AdapterMarket]:
        try:
            resp = requests.get(
                f"{self.BASE_URL}/markets",
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": limit,
                    "sort_by": "volume24hr",
                    "ascending": "false",
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            markets = data if isinstance(data, list) else data.get("markets", [])

            result = []
            for m in markets:
                prices_raw = m.get("outcomePrices", '["0.5","0.5"]')
                prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
                yes_price = float(prices[0]) if prices else 0.5

                end_date_str = m.get("endDate") or m.get("end_date_iso", "")
                try:
                    end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                    resolution_hours = max(0.0, (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600)
                except Exception:
                    resolution_hours = 48.0

                volume = float(m.get("volume24hr") or m.get("volume") or 0)
                liquidity = float(m.get("liquidity") or 0)
                spread_bps = float(m.get("spread") or 0) * 10_000 if m.get("spread") else 50.0
                title = m.get("question") or m.get("title", "")

                result.append(AdapterMarket(
                    snapshot=MarketSnapshot(
                        market_id=m.get("conditionId") or m.get("id", "unknown"),
                        venue=self.venue,
                        market_prob=yes_price,
                        spread_bps=spread_bps,
                        depth_usd=liquidity,
                        volume_24h_usd=volume,
                        time_to_resolution_hours=resolution_hours,
                        updated_at=datetime.now(timezone.utc),
                        question=title,
                    ),
                    catalysts=[],
                    theme=_infer_theme(title + " " + m.get("category", "")),
                    title=title,
                ))
            print(f"[PolymarketAdapter] fetched {len(result)} markets")
            return result
        except Exception as e:
            print(f"[PolymarketAdapter] error: {e}")
            return []


class JupiterAdapter:
    """Fetches live prediction markets from Jupiter Prediction API.

    Uses the Jupiter Prediction Market API (https://api.jup.ag/prediction/v1).
    Requires JUPITER_API_KEY in .env.
    """

    venue = Venue.JUPITER_PREDICTION
    BASE_URL = "https://api.jup.ag/prediction/v1"

    def fetch_markets(self, limit: int = 25) -> list[AdapterMarket]:
        api_key = os.environ.get("JUPITER_API_KEY", "")
        if not api_key:
            print("[JupiterAdapter] JUPITER_API_KEY not set in .env")
            return []

        try:
            resp = requests.get(
                f"{self.BASE_URL}/events",
                headers={"x-api-key": api_key},
                params={
                    "includeMarkets": "true",
                    "filter": "live",
                    "sortBy": "volume",
                    "sortDirection": "desc",
                },
                timeout=10,
            )
            resp.raise_for_status()
            events = resp.json().get("data", [])

            result = []
            for event in events:
                if len(result) >= limit:
                    break
                category = event.get("category", "other")
                event_title = (event.get("metadata") or {}).get("title", "")

                for m in event.get("markets", []):
                    if m.get("status") != "open":
                        continue

                    pricing = m.get("pricing") or {}
                    buy_yes = pricing.get("buyYesPriceUsd")   # ask for YES
                    sell_yes = pricing.get("sellYesPriceUsd")  # bid for YES

                    if buy_yes is None or sell_yes is None:
                        continue

                    mid = (buy_yes + sell_yes) / 2
                    spread_bps = max(0.0, (buy_yes - sell_yes) * 10_000)
                    volume = float(pricing.get("volume") or 0)

                    close_unix = m.get("closeTime")
                    if close_unix:
                        close_dt = datetime.fromtimestamp(close_unix, tz=timezone.utc)
                        resolution_hours = max(0.0, (close_dt - datetime.now(timezone.utc)).total_seconds() / 3600)
                    else:
                        resolution_hours = 48.0

                    market_title = (m.get("metadata") or {}).get("title", event_title)

                    result.append(AdapterMarket(
                        snapshot=MarketSnapshot(
                            market_id=m["marketId"],
                            venue=self.venue,
                            market_prob=mid,
                            spread_bps=spread_bps,
                            depth_usd=volume,
                            volume_24h_usd=volume,
                            time_to_resolution_hours=resolution_hours,
                            updated_at=datetime.now(timezone.utc),
                            question=market_title,
                        ),
                        catalysts=[],
                        theme=_infer_theme(market_title + " " + category),
                        title=market_title,
                    ))

                    if len(result) >= limit:
                        break

            print(f"[JupiterAdapter] fetched {len(result)} markets")
            return result
        except Exception as e:
            print(f"[JupiterAdapter] error: {e}")
            return []
