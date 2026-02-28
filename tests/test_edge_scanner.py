from edge_agent import EdgeScanner, JupiterAdapter, KalshiAdapter, PolymarketAdapter, Venue


def test_scanner_collects_from_all_adapters() -> None:
    scanner = EdgeScanner(adapters=[JupiterAdapter(), KalshiAdapter(), PolymarketAdapter()])
    collected = scanner.collect()

    assert len(collected) == 3
    venues = {snapshot.venue for snapshot, _, _ in collected}
    assert venues == {Venue.JUPITER_PREDICTION, Venue.KALSHI, Venue.POLYMARKET}


def test_adapter_payload_shape() -> None:
    scanner = EdgeScanner(adapters=[JupiterAdapter()])
    (snapshot, catalysts, theme) = scanner.collect()[0]

    assert snapshot.market_id
    assert isinstance(catalysts, list)
    assert theme in {"sports", "macro", "politics"}
