from pprint import pprint

from edge_agent import EdgeEngine, EdgeReporter, EdgeScanner, EdgeService, PortfolioState
from edge_agent.adapters import JupiterAdapter, KalshiAdapter, PolymarketAdapter


def main() -> None:
    engine = EdgeEngine()
    service = EdgeService(engine=engine)
    reporter = EdgeReporter(service=service)
    scanner = EdgeScanner(adapters=[JupiterAdapter(), KalshiAdapter(), PolymarketAdapter()])
    portfolio = PortfolioState(bankroll_usd=10_000, daily_drawdown_pct=0.01, theme_exposure_pct={"sports": 0.08})

    recommendations, summary = service.run_scan(scanner.collect(), portfolio=portfolio)

    print("=== Ranked recommendations ===")
    for rec in recommendations:
        pprint(rec)

    print("\n=== Scan summary ===")
    pprint(summary)

    print("\n=== Dashboard payload ===")
    pprint(reporter.build_dashboard(top_n=3))


if __name__ == "__main__":
    main()
