# EDGE Agent — Future Plans

## Current State (Done)

- Real market data from Polymarket (Gamma API) and Jupiter Prediction Market API
- Kalshi adapter implemented (DNS issue in sandbox only, works in production)
- News API fetches real headlines per-market using market-specific queries
- AI analysis (Llama 4 Maverick via OpenRouter) evaluates each market with real catalysts
- Full recommendation pipeline: probability node → EV node → qualification gate → risk policy → recommendation
- All recommendations are proposal-only (`requires_approval=True`, no live trading)

---

## Phase 1 — Order Execution (Jupiter)

Wire up actual trade placement on Jupiter Prediction Market.

**API:** `POST https://api.jup.ag/prediction/v1/trading`
**Auth:** `x-api-key` header (same key already in `.env`)

Steps:
1. Add a Solana wallet keypair (private key in `.env` as `SOLANA_PRIVATE_KEY`)
2. Implement a `JupiterTrader` class that:
   - Takes a `Recommendation` with `action=BUY_YES` or `BUY_NO` and `qualification_state=QUALIFIED`
   - Calls the trading endpoint to place the order
   - Returns an order ID for tracking
3. Gate execution behind an explicit approval flag (not just `requires_approval`)
4. Integrate with the engine: `engine.execute(recommendation, trader)`

---

## Phase 2 — Position Tracking (Jupiter)

Track open positions and update `PortfolioState` with real exposure.

**API:** `GET https://api.jup.ag/prediction/v1/orders?ownerPubkey=<wallet>`
**API:** `GET https://api.jup.ag/prediction/v1/positions`

Steps:
1. Implement a `PositionTracker` that polls `/orders?ownerPubkey=...` on each scan cycle
2. Parse filled orders → update `PortfolioState.theme_exposure_pct` with real positions
3. Parse settled orders → calculate realized P&L, update `daily_drawdown_pct`
4. Feed live `PortfolioState` into the engine instead of the hardcoded values in `run_edge_demo.py`

**Order schema fields to use:**
- `isYes` / `isBuy` — position side
- `filledContracts` / `avgFillPriceUsd` — fill details
- `settled` — whether P&L is realized
- `sizeUsd` — notional exposure
- `status`: `pending` / `filled` / `failed`

---

## Phase 3 — Kalshi Integration (Full)

Kalshi API works but requires DNS resolution from the runtime environment (sandbox limitation).

**API:** `GET https://api.kalshi.com/trade-api/v2/markets?status=open&limit=20`
**Auth:** Public (no key needed for market data reads)

Steps:
1. Verify Kalshi adapter works end-to-end from production environment
2. Add Kalshi order placement if Kalshi trading API access is obtained (requires account + API key)

---

## Phase 4 — Orderbook Depth (Jupiter)

Replace `volume` as the proxy for `depth_usd` with real orderbook data.

**API:** `GET https://api.jup.ag/prediction/v1/events/{eventId}/markets`

The per-market endpoint may expose richer liquidity data than the bulk `/events` call.
Use `buyYesPriceUsd` / `sellYesPriceUsd` alongside orderbook depth to get a more accurate `spread_bps` and `depth_usd`.

---

## Phase 5 — Scheduler / Continuous Scan

Replace the one-shot `run_edge_demo.py` with a continuous scan loop.

Steps:
1. Add a scheduler (e.g. `apscheduler` or simple `time.sleep` loop) to re-scan every N minutes
2. Persist recommendations to a database (SQLite to start) instead of in-memory repository
3. Alert on new QUALIFIED recommendations (Discord webhook, email, or terminal notification)
4. Track recommendation staleness — drop watchlist entries that have been stale for X hours

---

## Phase 6 — UI / Dashboard

Expose the dashboard payload as a web interface.

Options:
- Simple: FastAPI endpoint serving `reporter.build_dashboard()` as JSON, consumed by a React frontend
- Quick: Streamlit app wrapping the scan + dashboard in a browser UI

The `EdgeDashboard` and `ScanSummary` dataclasses are already structured for this.
