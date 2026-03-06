from pprint import pprint

from edge_agent import (
    EdgeEngine,
    EdgeReporter,
    EdgeScanner,
    EdgeService,
    PortfolioState,
)
from edge_agent.adapters import JupiterAdapter, KalshiAdapter, PolymarketAdapter
from edge_agent.ai_service import get_ai_response

def answer_question(question: str) -> bool:
    """Answers a question using the Q&A agent, and returns whether to exit."""
    # Check for exit condition
    if question.lower() in ["exit", "quit", "goodbye"]:
        print("\nGoodbye!")
        return True

    system_prompt = (
        "You are a sophisticated and insightful expert on prediction markets named Edge. Your primary goal is to provide helpful and educational answers to users, ranging from beginners to experts. "
        "When you greet a user, introduce yourself by name. "
        "When a user asks a question, you must first understand the intent and then provide a nuanced response that is tailored to the user. "
        
        "## Your Capabilities: ##"
        "1.  **Broad Category Knowledge**: You are an expert in all major prediction market categories, including politics, sports, weather, science, finance, and more. When asked about a topic, always connect it to the world of prediction markets. "
        "2.  **Context-Aware Response Length**: You must adjust your response length based on the topic. "
        "    - For core prediction market concepts (e.g., 'what is a prediction market?', 'how does liquidity work?', 'what are the risks?'), provide **longer, more detailed, and educational answers**. Assume the user is a beginner. "
        "    - For questions about specific market categories (e.g., 'who will win the election?', 'will it rain tomorrow?', 'will this stock go up?'), provide **shorter, more direct answers** that reference how one might trade on this in a prediction market. "
        "3.  **Data-Driven Insights**: You can use both current and historical data to enrich your answers. If the question is about a current event, you might reference real-time market odds. If it's a more general question, you can use historical examples to provide a learning lesson. "
        "4.  **Confidence and Uncertainty**: You must assess your own confidence level (Low, Medium, or High) for each answer. When confidence is not High, explicitly state it. For low or medium confidence answers, you should recommend cautious actions like 'HOLD' or 'Do Not Trade.' You can also suggest specific conditions or entry points that would make a trade more attractive. "
        "5.  **Graceful Handling of Unrelated Questions**: If a question is truly off-topic and cannot be connected to prediction markets, politely state that your focus is on prediction markets. "
        "6.  **Handle Greetings and Goodbyes**: Respond to simple greetings and goodbyes in a friendly and natural way. "
    )
    
    ai_response = get_ai_response(question, task_type="creative", system_prompt=system_prompt)
    
    if ai_response and ai_response.get("content"):
        print(f"\nAnswer: {ai_response['content']}")
        if ai_response.get("confidence_level"):
            print(f"Confidence: {ai_response['confidence_level']}")
        if ai_response.get("action_recommendation"):
            print(f"Recommendation: {ai_response['action_recommendation']}")
        if ai_response.get("entry_conditions"):
            print("Entry Conditions:")
            for condition in ai_response["entry_conditions"]:
                print(f"- {condition}")
    else:
        print("\nSorry, I couldn't generate a response in the expected format.")
        pprint(ai_response)
        
    return False


def main() -> None:
    """Runs the edge agent demo."""
    engine = EdgeEngine()
    service = EdgeService(engine=engine)
    reporter = EdgeReporter(service=service)
    scanner = EdgeScanner(adapters=[JupiterAdapter(), KalshiAdapter(), PolymarketAdapter()])
    portfolio = PortfolioState(bankroll_usd=10_000, daily_drawdown_pct=0.01, theme_exposure_pct={"sports": 0.08})
    markets = []  # Start with no markets
    catalysts = []  # Start with no catalysts

    while True:
        print("\nSelect an action:")
        print("1. Run market scan")
        print("2. Ask a question about prediction markets")
        print("3. Fetch Latest News")
        print("4. Fetch Latest Markets")
        print("5. Exit")
        choice = input("Enter your choice (1, 2, 3, 4, or 5): ")

        if choice == "1":
            if not markets:
                print("\nPlease fetch markets first (option 4).")
                continue
            print("Running market scan...")
            recommendations, summary = service.run_scan(scanner.collect(markets, catalysts), portfolio=portfolio)

            print("=== Ranked recommendations ===")
            for rec in recommendations:
                pprint(rec)

            print("\n=== Scan summary ===")
            pprint(summary)

            print("\n=== Dashboard payload ===")
            pprint(reporter.build_dashboard(top_n=3))
        elif choice == "2":
            question = input("What is your question? ")
            if answer_question(question):
                break
        elif choice == "3":
            print("Fetching latest news...")
            catalysts = scanner.catalyst_engine.detect_catalysts("US politics")
            print(f"Found {len(catalysts)} new catalysts.")
        elif choice == "4":
            print("Fetching latest markets...")
            markets = scanner.fetch_markets()
            print(f"Found {len(markets)} markets.")
        elif choice == "5":
            print("Exiting...")
            break
        else:
            print("Invalid choice. Please try again.")


if __name__ == "__main__":
    main()
