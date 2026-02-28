# EDGE AI Agent (Reference Scaffold)

This repository includes a starter implementation of the **EDGE** prediction-markets agent architecture.

## What's included

- `edge_agent/models.py`: typed domain models, venue enums, and policy objects.
- `edge_agent/nodes.py`: modular logic nodes (probability, venue-aware EV, qualification, risk, recommendation).
- `edge_agent/watchlist.py`: in-memory dynamic watchlist store.
- `edge_agent/repository.py`: in-memory recommendation repository and analytics helpers.
- `edge_agent/service.py`: application-style service layer for batch scans, summaries, and watchlist views.
- `edge_agent/reporting.py`: dashboard/report payload builder for UI/API consumers.
- `edge_agent/adapters.py`: venue adapters (Jupiter/Kalshi/Polymarket) that emit normalized market candidates.
- `edge_agent/scanner.py`: adapter fan-in collector that builds scan inputs for the engine.
- `edge_agent/engine.py`: `EdgeEngine` orchestration for proposal-first evaluation + watchlist/repository updates.
- `run_edge_demo.py`: local demo runner using adapters + scanner + engine/service/reporter.
- `tests/test_edge_engine.py`: engine behavior tests.
- `tests/test_edge_service.py`: service/reporting/serialization tests.
- `tests/test_edge_scanner.py`: adapter/scanner normalization tests.

## Run demo

```bash
python run_edge_demo.py
```

## Run tests

```bash
pytest -q
```

## Important

This scaffold is **proposal-first** and intentionally does not place real trades.
