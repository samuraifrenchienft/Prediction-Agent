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
from edge_agent.memory import KnowledgeBase, SessionMemory

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
            print(f"  [MarketLookup] {series}: {e}")

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


def answer_question(question: str, kb: KnowledgeBase, mem: SessionMemory) -> bool:
    """Answers a prediction market question. Returns True if user wants to exit."""
    if question.lower() in ("exit", "quit", "goodbye"):
        print("\nGoodbye! Session saved.")
        return True

    # 1. Fetch live market data only if the question is market-topic-specific
    live_markets = _fetch_relevant_markets(question)
    market_context = _format_market_context(live_markets)
    if live_markets:
        tickers = [m.get("ticker", "") for m in live_markets]
        print(f"  [Edge] Pulled {len(live_markets)} live market(s) for context.")

    # 2. Search knowledge base for relevant docs
    kb_context = kb.get_context_for_question(question)
    if kb_context:
        print(f"  [Edge] Found relevant knowledge base context.")

    # 3. Get today's session context (recent Q&A history + preferences)
    session_context = mem.get_session_context(max_exchanges=4)

    system_prompt = (
        "You are Edge, an expert prediction market analyst and guide. "
        "You have deep knowledge of Polymarket and Kalshi — their UIs, account setup, trading mechanics, and strategy. "
        "Answer concisely but completely. "
        "When live market data is provided, reference specific odds and volumes. "
        "When knowledge base context is provided, use it to give accurate platform-specific guidance. "
        "When session context is provided, use it to give continuity — reference earlier parts of the conversation when relevant. "
        "Assess confidence: Low / Medium / High. Recommend HOLD for Low/Medium confidence trades. "
        "If the question is off-topic, politely redirect to prediction markets. "
        "Return JSON: {content, confidence_level, action_recommendation, entry_conditions}"
    )

    # Build the full prompt: question + all context layers
    prompt = question + kb_context + session_context + market_context
    print("\nThinking... (may take 10-90s for complex questions)")
    ai_response = get_ai_response(prompt, task_type="creative", system_prompt=system_prompt)

    if ai_response is None:
        print("\nAPI error - check that OPEN_ROUTER_API_KEY or GROQ_API_KEY is set and valid.")
        return False

    raw = (
        ai_response.get("content")
        or ai_response.get("answer")
        or ai_response.get("response")
        or ai_response.get("message")
        or ai_response.get("text")
    )
    answer_text = (raw or "").strip() if raw else ""

    if answer_text:
        print(f"\nEdge: {answer_text}")
        if ai_response.get("confidence_level"):
            print(f"Confidence: {ai_response['confidence_level']}")
        if ai_response.get("action_recommendation"):
            print(f"Recommendation: {ai_response['action_recommendation']}")
        entry_conditions = ai_response.get("entry_conditions")
        if entry_conditions:
            conditions = entry_conditions if isinstance(entry_conditions, list) else [entry_conditions]
            print("Entry conditions:")
            for cond in conditions:
                print(f"  - {cond}")
    else:
        print("\nCouldn't generate a response in the expected format.")
        pprint(ai_response)

    # 4. Save this exchange to session memory
    if answer_text:
        topics = [s[1][0] for s in _SERIES_MAP if any(kw in question.lower() for kw in s[0])]
        tickers_discussed = [m.get("ticker", "") for m in live_markets] if live_markets else []
        mem.add_exchange(question, answer_text, markets_discussed=tickers_discussed, topics=topics)

    return False


def main() -> None:
    # ── Initialize systems (no API calls made here) ──────────────────────────
    engine = EdgeEngine()
    service = EdgeService(engine=engine)
    reporter = EdgeReporter(service=service)
    scanner = EdgeScanner(adapters=[KalshiAdapter(), PolymarketAdapter()])
    portfolio = PortfolioState(
        bankroll_usd=10_000,
        daily_drawdown_pct=0.01,
        theme_exposure_pct={"sports": 0.08},
    )

    # ── Load memory systems ──────────────────────────────────────────────────
    kb = KnowledgeBase()
    mem = SessionMemory()

    markets = []
    catalysts = []

    # ── Startup greeting ─────────────────────────────────────────────────────
    kb_stats = kb.stats()
    mem_stats = mem.stats()

    print("\n" + "═" * 52)
    print("  Edge — Prediction Market Intelligence Agent")
    print("═" * 52)
    print("  Live data: Kalshi + Polymarket")
    print("  AI: OpenRouter (free tier)")
    print(f"  Knowledge base: {kb_stats['total_docs']} docs loaded")
    print(f"  Session: {mem_stats['today_exchanges']} exchanges today")
    print("─" * 52)
    print("  Hey — I'm Edge. I track live prediction markets")
    print("  across Kalshi and Polymarket, analyze odds, and")
    print("  help you find edges before the market catches on.")
    print("")
    print("  Ask me anything: account setup, market odds,")
    print("  strategy, or just what's moving right now.")
    print("═" * 52 + "\n")

    if mem_stats["today_exchanges"] > 0:
        print(f"  Welcome back — continuing today's session "
              f"({mem_stats['today_exchanges']} questions so far).\n")

    # ── Main loop ─────────────────────────────────────────────────────────────
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
            if not question:
                continue
            if answer_question(question, kb, mem):
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
            print("Exiting. Session saved.")
            break
        else:
            print("Invalid choice.")

    kb.close()
    mem.close()


if __name__ == "__main__":
    main()
