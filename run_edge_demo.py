from pprint import pprint

from edge_agent import EdgeEngine, EdgeReporter, EdgeScanner, EdgeService, PortfolioState
from edge_agent.adapters import JupiterAdapter, KalshiAdapter, PolymarketAdapter
from edge_agent.presets import PREDICTION_MARKET_DNA


def main() -> None:
    engine = EdgeEngine()
    service = EdgeService(engine=engine)
    reporter = EdgeReporter(service=service)
    scanner = EdgeScanner(
        adapters=[JupiterAdapter(), KalshiAdapter(), PolymarketAdapter()],
        brand_dna=PREDICTION_MARKET_DNA,
    )
    portfolio = PortfolioState(bankroll_usd=10_000, daily_drawdown_pct=0.01, theme_exposure_pct={"sports": 0.08})

    recommendations, summary = service.run_scan(scanner.collect(), portfolio=portfolio)

    print("=== Ranked recommendations ===")
    for rec in recommendations:
        pprint(rec)

    print("\n=== Scan summary ===")
    pprint(summary)

    print("\n=== Dashboard payload ===")
    pprint(reporter.build_dashboard(top_n=3))

    print("\n=== Strategic Briefing (Brand DNA) ===")
    briefing = reporter.build_briefing(brand_dna=PREDICTION_MARKET_DNA, top_n=3)
    print(f"\n[Header]\n{briefing.header}")
    print(f"\n[Top Opportunities]\n{briefing.top_opportunities}")
    print(f"\n[Catalyst Summary]\n{briefing.catalyst_summary}")
    print(f"\n[Watchlist]\n{briefing.watchlist}")
    print(f"\n[Footer]\n{briefing.footer}")
    print(f"\n[Image Prompt]\n{briefing.image_prompt}")


if __name__ == "__main__":
    main()
