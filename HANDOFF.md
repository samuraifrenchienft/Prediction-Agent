# Handoff Document

## Current State

The prediction agent is fully operational with a structured Brand DNA configuration layer,
clean AI service integration, and a comprehensive test suite. The demo runs end-to-end.

---

## Completed Work

### Phase 1 — Demo Stabilisation
- **News API Integration**: `NewsAPIClient` in `edge_agent/dat-ingestion/news_api.py`
- **Import and path resolution**: Fixed `dat-ingestion` module import via `importlib`
- **Data model alignment**: Added `Catalyst` dataclass to `models.py`; `AIAnalysis` aliased for compat
- **AI service return type**: `get_ai_response` now returns `dict | None` (was incorrectly returning `AIAnalysis`)
- **Demo execution**: `run_edge_demo.py` runs end-to-end without crashing

### Phase 2 — Handoff To-Do Items
- **Error handling**: `ai_service.py` uses specific handlers (`JSONDecodeError`, `APIConnectionError`,
  `APIStatusError`, `ValueError`) and `logging` instead of bare `print`
- **AI prompts refined**: Both the probability node (`nodes.py`) and catalyst gatekeeper
  (`catalyst_engine.py`) now require strict JSON schemas with no extra keys or markdown
- **Unit tests added**: `tests/test_news_api.py` (4 tests), `tests/test_ai_service.py` (5 tests)
- **Configuration management**: `OpenAI` client created once at module load; warning logged if
  key is missing; `None` returned gracefully rather than raising at call time

### Phase 3 — Brand DNA Framework
- **`edge_agent/brand_dna.py`**: `StrategyDNA`, `CopyDNA`, `VisualDNA`, `BrandDNA` dataclasses.
  Each block exposes a `to_system_prompt()` method; `BrandDNA` adds `to_briefing_prompt()`.
- **`edge_agent/presets.py`**: Two fully-drafted presets:
  - `PREDICTION_MARKET_DNA` — macro, politics, sports (Kalshi / Polymarket / Jupiter)
  - `CRYPTO_DEFI_DNA` — DeFi protocol events, governance, on-chain catalysts
- **`EdgeScanner`** now accepts `brand_dna`; query auto-derived from `StrategyDNA` keywords
- **`CatalystDetectionEngine`** accepts `strategy_dna`; uses DNA prompt as gatekeeper + filters
  articles below `relevance_threshold`
- **`EdgeReporter.build_briefing(brand_dna)`**: New method that sends full 3-block prompt to
  the briefing LLM and returns a structured `EdgeBriefing` dataclass

### Phase 4 — Test Suite Hardening
- **`tests/test_brand_dna.py`**: 20 tests covering DNA `to_system_prompt()`, `build_news_query()`,
  `to_briefing_prompt()`, and both presets
- **`tests/test_edge_scanner.py`**: Refactored to mock `CatalystDetectionEngine` — no longer
  hits real NewsAPI; added tests for DNA pass-through
- **`tests/test_ai_functions.py`**: Fixed brittle `test_complex_prompt` assertion to use
  topic-level keyword matching instead of exact word matching

---

## Architecture Overview

```
Brand DNA preset (presets.py)
        │ strategy
        ▼
EdgeScanner.collect()
        │ uses strategy.build_news_query() → NewsAPI
        ▼
CatalystDetectionEngine._create_catalyst_from_article()
        │ uses strategy.to_system_prompt() as gatekeeper AI prompt
        │ filters articles below relevance_threshold
        ▼
list[Catalyst]  →  EdgeEngine.evaluate_batch()
                          │
                          ▼
                   list[Recommendation]
                          │
                          ▼
              EdgeReporter.build_briefing(brand_dna)
                          │ uses brand_dna.to_briefing_prompt() → AI synthesis
                          ▼
                    EdgeBriefing (Header / Top Opportunities /
                                  Catalyst Summary / Watchlist / Footer)
```

## Key Files

| File | Purpose |
|------|---------|
| `edge_agent/brand_dna.py` | DNA dataclasses and prompt builders |
| `edge_agent/presets.py` | `PREDICTION_MARKET_DNA`, `CRYPTO_DEFI_DNA` presets |
| `edge_agent/ai_service.py` | OpenRouter API client (module-level singleton) |
| `edge_agent/catalyst_engine.py` | Gatekeeper AI + relevance filtering |
| `edge_agent/nodes.py` | Probability estimation, EV calculation, qualification gate |
| `edge_agent/reporting.py` | Dashboard + Brand DNA briefing generation |
| `run_edge_demo.py` | End-to-end demo entry point |

## Environment Variables Required

| Variable | Used by |
|----------|---------|
| `OPEN_ROUTER_API_KEY` | `ai_service.py` — all AI calls |
| `NEWS_API_KEY` | `dat-ingestion/news_api.py` — news headline fetching |

---

## Suggested Next Steps

- **Live integration test suite**: Mark `test_ai_functions.py` and `test_api_connections.py`
  with `pytest.mark.integration` and exclude from default `pytest` run
- **Additional presets**: `EQUITY_RESEARCH_DNA`, `SPORTS_BETTING_DNA`
- **Frontend / dashboard**: Consume `EdgeBriefing` output in a web UI or email digest
- **Scheduled scanning**: Wrap `run_edge_demo.py` in a cron job or cloud scheduler
- **Async batch evaluation**: `EdgeEngine.evaluate_batch` could be parallelised with
  `asyncio` for large market sets
