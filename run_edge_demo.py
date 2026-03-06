from __future__ import annotations

import importlib
from pprint import pprint

from edge_agent import (
    EdgeEngine,
    EdgeReporter,
    EdgeScanner,
    EdgeService,
    PortfolioState,
)
from edge_agent.adapters import KalshiAdapter, PolymarketAdapter
from edge_agent.ai_service import get_ai_response

# Lazy import of kalshi_api for targeted market lookups in Q&A
_kalshi_api = importlib.import_module(".dat-ingestion.kalshi_api", "edge_agent")

# ── Keyword → Kalshi series mapping for on-demand Q&A lookups ────────────────
_SERIES_MAP: list[tuple[list[str], list[str]]] = [
    (["fed", "fomc", "interest rate", "rate cut", "rate hike"], ["KXFED"]),
    (["inflation", "cpi", "pce"], ["KXINFL"]),
    (["gdp", "recession", "economy", "growth"], ["KXGDP"]),
    (["bitcoin", "btc"], ["KXBTC"]),
    (["ethereum", "eth"], ["KXETH"]),
    (["nba", "basketball", "championship"], ["KXNBA"]),
    ([" nfl ", "football", "super bowl"], ["KXNFL"]),
    (["president", "election", "trump", "democrat", "republican", "vote"], ["KXPRES"]),
    (["highny", "weather", "new york"], ["KXHIGHNY"]),
]

# Cache: series → (markets, fetched_at)
_series_cache: dict[str, tuple[list[dict], float]] = {}
_CACHE_TTL = 300  # 5 minutes


def _fetch_relevant_markets(question: str) -> list[dict]:
    """
    Returns live Kalshi market data only for series relevant to the question.
    Returns empty list for non-prediction-market questions.
    """
    import time
    q = question.lower()
    series_to_fetch: list[str] = []
    for keywords, series in _SERIES_MAP:
        if any(kw in q for kw in keywords):
            series_to_fetch.extend(series)

    if not series_to_fetch:
        return []

    markets: list[dict] = []
    for series in series_to_fetch[:3]:  # cap at 3 series per query
        cached = _series_cache.get(series)
        if cached and (time.time() - cached[1]) < _CACHE_TTL:
            markets.extend(cached[0])
            continue
        try:
            result = _kalshi_api.get_markets(limit=5, series_ticker=series, min_volume=1)
            _series_cache[series] = (result, time.time())
            markets.extend(result)
        except Exception as e:
            print(f"[MarketLookup] {series}: {e}")

    return markets[:8]


def _format_market_context(markets: list[dict]) -> str:
    """Format live market data as a compact context string for the AI prompt."""
    if not markets:
        return ""
    lines = ["\n\nLive prediction market data (Kalshi):"]
    for m in markets:
        prob = _kalshi_api.parse_market_prob(m)
        vol = _kalshi_api.parse_volume(m)
        title = m.get("title") or m.get("ticker", "")
        lines.append(f"- {title}: {prob:.0%} yes | volume ${vol:,.0f}")
    return "\n".join(lines)


def answer_question(question: str) -> bool:
    """Answers a prediction market question. Returns True if user wants to exit."""
    if question.lower() in ("exit", "quit", "goodbye"):
        print("\nGoodbye!")
        return True

    # Fetch live market data only if question is market-topic-specific
    live_markets = _fetch_relevant_markets(question)
    market_context = _format_market_context(live_markets)

    if live_markets:
        print(f"  [Edge] Pulled {len(live_markets)} live market(s) for context.")

    system_prompt = (
        "You are Edge, an expert prediction market analyst. "
        "Answer concisely and connect every response to prediction markets. "
        "When live market data is provided, reference specific odds and volumes in your answer. "
        "For general prediction market concepts, give educational answers. "
        "For specific market questions, give direct analysis with trading implications. "
        "Assess confidence: Low / Medium / High. For Low or Medium, recommend HOLD. "
        "If the question is off-topic, politely redirect to prediction markets. "
        "Return JSON: {content, confidence_level, action_recommendation, entry_conditions}"
    )

    prompt = question + market_context
    ai_response = get_ai_response(prompt, task_type="creative", system_prompt=system_prompt)

    if ai_response and ai_response.get("content"):
        print(f"\nEdge: {ai_response['content']}")
        if ai_response.get("confidence_level"):
            print(f"Confidence: {ai_response['confidence_level']}")
        if ai_response.get("action_recommendation"):
            print(f"Recommendation: {ai_response['action_recommendation']}")
        if ai_response.get("entry_conditions"):
            print("Entry conditions:")
            for c in ai_response["entry_conditions"]:
                print(f"  - {c}")
    else:
        print("\nCouldn't generate a response. Check your AI API key.")

    return False


def main() -> None:
    engine = EdgeEngine()
    service = EdgeService(engine=engine)
    reporter = EdgeReporter(service=service)
    # Jupiter removed — no live API available
    scanner = EdgeScanner(adapters=[KalshiAdapter(), PolymarketAdapter()])
    portfolio = PortfolioState(
        bankroll_usd=10_000,
        daily_drawdown_pct=0.01,
        theme_exposure_pct={"sports": 0.08},
    )
    markets = []
    catalysts = []

    print("\n=== Edge — Prediction Market Agent ===")
    print("Live data: Kalshi + Polymarket | AI: OpenRouter free models\n")

    while True:
        print("1. Run market scan")
        print("2. Ask Edge a question")
        print("3. Fetch latest news catalysts")
        print("4. Fetch latest markets")
        print("5. Exit")
        choice = input("\nChoice: ").strip()

        if choice == "1":
            if not markets:
                print("Fetch markets first (option 4).")
                continue
            print(f"Scanning {len(markets)} markets...")
            recommendations, summary = service.run_scan(
                scanner.collect(markets, catalysts), portfolio=portfolio
            )
            print("\n=== Recommendations ===")
            for rec in recommendations:
                pprint(rec)
            print("\n=== Summary ===")
            pprint(summary)
            print("\n=== Dashboard ===")
            pprint(reporter.build_dashboard(top_n=3))

        elif choice == "2":
            question = input("Question: ").strip()
            if answer_question(question):
                break

        elif choice == "3":
            topic = input("News topic (or press Enter for general): ").strip() or "US politics markets economy"
            print(f"Fetching news for: {topic}")
            catalysts = scanner.catalyst_engine.detect_catalysts(topic)
            print(f"Found {len(catalysts)} catalysts.")

        elif choice == "4":
            print("Fetching live markets from Kalshi + Polymarket...")
            markets = scanner.fetch_markets()
            print(f"Loaded {len(markets)} live markets.")
            if not markets:
                print("  No markets returned. Check API keys in .env")

        elif choice == "5":
            print("Exiting.")
            break
        else:
            print("Invalid choice.")


if __name__ == "__main__":
    main()
