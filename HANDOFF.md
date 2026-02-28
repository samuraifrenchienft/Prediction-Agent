# EDGE Agent Handoff Pack

This handoff is intended for the next coding agent to continue implementation quickly and safely.

## 1) Current status

EDGE is a **proposal-first** prediction-markets scaffold with:

- Core pipeline (`probability -> EV -> qualification -> risk -> recommendation`) in `edge_agent/nodes.py`.
- Orchestration in `edge_agent/engine.py`.
- In-memory watchlist and repository (`watchlist.py`, `repository.py`).
- Service/reporting layers for scan summaries and dashboard payloads (`service.py`, `reporting.py`).
- Adapter/scanner ingestion boundary (`adapters.py`, `scanner.py`).
- Unit tests across engine/service/scanner modules.

## 2) Entry points

- Demo run: `python run_edge_demo.py`
- Full tests: `pytest -q`
- Primary orchestration: `EdgeEngine.evaluate_market` and `EdgeEngine.evaluate_batch`
- App-facing boundary: `EdgeService.run_scan`

## 3) Architecture map

- `edge_agent/models.py`: domain DTOs + policy + recommendation serialization
- `edge_agent/nodes.py`: all decision nodes
- `edge_agent/engine.py`: wires node graph and updates state stores
- `edge_agent/adapters.py`: venue mock adapters (Jupiter/Kalshi/Polymarket)
- `edge_agent/scanner.py`: fan-in collector for adapter outputs
- `edge_agent/repository.py`: in-memory recommendation/history analytics
- `edge_agent/watchlist.py`: in-memory watchlist
- `edge_agent/service.py`: scan summary and watchlist APIs
- `edge_agent/reporting.py`: dashboard payload builder

## 4) Known gaps (next agent should prioritize)

1. **Persistence abstraction + durable DB implementation**
   - Introduce repository/watchlist interfaces.
   - Add SQLite/Postgres-backed implementations.
2. **Live adapters**
   - Replace static adapter payloads with real venue connectors.
   - Preserve normalized internal shapes.
3. **HTTP API surface**
   - Add FastAPI (or equivalent) around `EdgeService`/`EdgeReporter`.
4. **Policy profiles**
   - Add conservative/balanced/opportunistic runtime policy loading.
5. **Integration tests**
   - Add persistence + API contract tests beyond unit tests.

## 5) Guardrails for the next agent

- Keep system **proposal-first** (no autonomous execution side effects).
- Preserve normalized adapter output contract: `(MarketSnapshot, list[Catalyst], theme)`.
- Keep recommendation payload backwards-compatible (or version it explicitly).
- Add tests for every new module and preserve passing `pytest -q`.

## 6) Suggested first tasks for incoming agent

1. Create `storage/` package with repository/watchlist interfaces.
2. Implement `SQLiteRecommendationRepository` + `SQLiteWatchlistStore`.
3. Add a minimal `api.py` with `/scan`, `/summary`, `/watchlist`, `/dashboard` routes.
4. Add integration tests covering end-to-end scan -> persist -> summary.

## 7) Quick command checklist

```bash
python run_edge_demo.py
pytest -q
```

If both pass, environment is good and handoff is complete.
