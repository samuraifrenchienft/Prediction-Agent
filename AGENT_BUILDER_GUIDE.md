# EDGE: Prediction Markets Agent Playbook (Jupiter-first, plus Kalshi + Polymarket)

This version is specifically for **prediction markets** (event contracts), not perps.


## Agent identity: EDGE

**Name:** EDGE  
**Meaning:** *Execution + Dislocation + Governance Engine*  
**Role:** A disciplined prediction-markets analyst that surfaces only executable, risk-qualified opportunities.

### Brand-safe alternates (if you want stylized naming)
- **E.D.G.E.** (Execution Dislocation Governance Engine)
- **EDGE-X** (same engine, extended modules)
- **EDG3** (visual style only, same pronunciation)

If you want the cleanest trust-forward brand, keep it simply **EDGE**.

## EDGE operating principle

> No forced trades. No filler. If a market is not executable after full risk and quality gating, EDGE stays silent or watchlists it.

If your system uses modular widgets and logic nodes, keep that architecture. The easiest reliable path is still **proposal-first**, then controlled execution.

## 1) Scope (what this agent should do)

### In scope (required)
1. **Market scanning**
   - Detect probability dislocations, abnormal volume, spread/depth changes, and rapid repricing.
2. **Thesis generation**
   - Explain why the market may be mispriced (news, incentives, base rates, timeline).
3. **Independent probability estimate**
   - Produce `p_true` + uncertainty band.
4. **Trade proposal**
   - Suggest action, entry range, invalidation, EV after costs, and max size.
5. **Risk monitoring**
   - Track exposure by theme, event date, and correlated drivers.
6. **Trust outputs**
   - Show evidence, what changed, and disconfirming evidence.

### Out of scope by default
- Fully autonomous trading without approvals.
- Leverage/martingale style behavior.
- Non-public/insider data.
- HFT/scalping behavior.

---

## 2) Capability scopes (practical permissions)

Allow by default:
- `read:markets`
- `read:news`
- `read:portfolio`
- `propose:trade`
- `write:watchlist`

Optional by request:
- `read:injury_reports` (sports markets only; approved sources)
- `read:social_signals` (comments/themes only; weak prior)
- `copy:leaders` (copy-trading analytics; proposal mode by default)

Approval required:
- `execute:trade`
- `rebalance:portfolio`
- `admin:risk_limits`

---

## 3) Minimal node architecture (V1)

Start with these 10 nodes:

1. **Market Ingest Node**
   - Odds/probability, spread, depth, volume, time-to-resolution.
2. **Rules/Resolution Node**
   - Parses contract wording, resolution source, dispute mechanics, and close time.
3. **News/Event Node**
   - Approved official + trusted media sources.
4. **Feature Node**
   - Momentum, volatility, liquidity quality, event proximity.
5. **Probability Node**
   - Ensemble estimate of `p_true` + uncertainty.
6. **Edge/EV Node**
   - Computes `edge = p_true - p_market` and net EV after full costs.
7. **Risk Policy Node**
   - Position caps, daily loss budget, correlation caps, venue limits.
8. **Recommendation Node**
   - Action, entry zone, no-trade zone, invalidation, max size, evidence.
9. **Approval Gate Node**
   - Blocks execution when permissions or policy are missing.
10. **Execution/Monitor Node**
   - Order placement (if allowed), fill tracking, reprice triggers, thesis-break alerts.

---

## 4) Venue profiles (Jupiter-first, then Kalshi and Polymarket)

## Jupiter prediction-markets profile (primary focus)
Use checks focused on on-chain event-market execution and market integrity:
- Prioritize Jupiter prediction markets in scan/ranking and polling cadence.
- Validate market contract metadata, resolution source, and settlement path.
- Include tx fees + slippage + failed-tx retry cost in EV and qualification gates.
- Enforce stale-quote timeout, max slippage guardrails, and execution survivability.
- Cap size by available depth and projected unwind cost, especially near close.

## Kalshi profile (regulated event contracts)
Use checks focused on contract semantics and centralized order-book quality:
- Validate settlement wording and official resolution path.
- Enforce spread/depth thresholds before sizing.
- Prefer limit entries in thin books.
- Reduce size near resolution when uncertainty remains high.

## Polymarket profile (crypto event markets)
Use checks focused on market structure, token/outcome plumbing, and resolution reliability:
- Validate market rules, condition/outcome mapping, and resolution/oracle path.
- Include protocol/bridge/wallet transfer friction in effective execution cost where applicable.
- Apply strict liquidity filters for long-tail markets and widen no-trade zones in thin books.
- Require stronger corroboration when social-driven flow dominates price action.

---

## 5) Signals to emit (decision quality)

### Alpha signals
- `edge = p_true - p_market`
- `ev_net` after fees/slippage/impact
- Catalyst impact score
- Liquidity quality score

### Risk signals
- Uncertainty width
- Regime/headline volatility risk
- Correlation concentration
- Time-to-resolution risk
- Resolution/wording ambiguity risk

### Execution signals
- Entry range
- No-trade zone
- Scale-in/out triggers
- Thesis-break conditions
- Max position % bankroll

### Trust signals
- Evidence trace (top sources)
- Ensemble disagreement
- Change since last alert
- Disconfirming evidence

---


## 5A) Multi-layer market qualification filter (signal gate)

Your Zigma example is directionally strong. Yes, you should take this pattern.

Before any BUY/SELL proposal is emitted, require all qualification gates to pass:

1. **Liquidity depth gate**
   - Verify executable depth at target size.
   - Reject if projected slippage exceeds policy threshold.
2. **Edge resilience gate**
   - Stress `p_true` with uncertainty shocks and small assumption changes.
   - Reject if edge collapses under mild perturbations.
3. **Time-decay gate**
   - Evaluate edge half-life vs time-to-resolution.
   - Reject if expected edge decay outpaces executable window.
4. **Volatility/entropy discount gate**
   - Apply confidence haircut in headline-volatile or high-entropy regimes.
   - Reject if discounted EV falls below zero.
5. **Execution-trap gate**
   - Detect thin books, spread blowouts, stale quotes, rapid reversal zones.
   - Reject if entry/exit survivability is poor.

### Gate result states
- `qualified`: fully executable signal
- `watchlist`: thesis valid but not executable yet
- `rejected`: fails one or more hard gates

### Mandatory rejection logging
For every rejected market, store machine-readable reject reasons, for example:
- `reject_reason_codes`: `LOW_DEPTH`, `EDGE_FRAGILE`, `TIME_DECAY`, `ENTROPY_HIGH`, `EXECUTION_TRAP`
- `next_recheck_at`

This enforces “no forced trades, no filler.”

### Dynamic watchlist policy

Markets that fail execution gates but pass thesis quality should enter a dynamic watchlist.

- Recheck cadence: event-driven + periodic polling (e.g., 5-30s depending on venue/load).
- Promote to `qualified` only when all gates pass.
- Auto-drop stale markets after configurable TTL.
- In UI, show exactly what must change for promotion (depth, spread, confidence, catalyst confirmation).

## 6) EV and sizing rules (hard requirements)

### Net EV components
- `ev_gross`
- `fees`
- `slippage_cost`
- `impact_cost`
- `resolution_risk_haircut`
- `ev_net = ev_gross - all_costs`

### Hard no-trade rules
- `ev_net <= 0`
- Confidence below threshold
- Uncertainty too wide
- Liquidity below minimum
- Contract wording/resolution ambiguity above limit

### Sizing defaults
- Per-market cap: 1-3% bankroll (mode-dependent)
- Theme cap: user-defined
- Daily loss budget cap: hard stop
- Cooldown after thesis break

---

## 7) Copy-trading module (prediction-markets specific)

If enabled, treat copy as a separate strategy with strict controls.

### Leader evaluation
- Track record sample size
- Max drawdown
- Calibration quality by category
- Consistency near resolution windows
- Fill quality and slippage history

### Copy risk controls
- Estimate EV decay from follower lag
- Block copy if `copy_ev_net <= 0` after lag/slippage
- Cap overlap with existing correlated positions
- Auto-pause on leader regime shift or policy breach

### Copy payload fields
- `leader_id`
- `leader_style_cluster`
- `estimated_copy_slippage`
- `copy_ev_net`
- `copy_allowed`

---

## 8) Social/comment cues (themes only)

Use social cues as **weak priors**, never primary alpha.

- Ingest theme velocity + source quality.
- Downweight bot-like bursts and duplicate meme cascades.
- Require non-social corroboration for sizing increases.
- If social disagrees with fundamentals and no confirmation exists: `watch/no-trade`.

Recommended fields:
- `theme_shift`
- `social_conviction`
- `source_quality_mix`
- `social_adjustment_delta_prob`

---

## 9) Sports injury mode (star-player daily summary)

Yes, AI can pull injuries if requested.

### Top injuries today mode
- Run on schedule + on demand.
- Focus only on market-relevant star players.
- Return top `N` injuries by expected market impact.
- Suppress low-impact noise.

Suggested score:

`impact_score = player_importance × status_severity × market_sensitivity × freshness_weight`

Output per item:
- `player_name`, `team`, `league`
- `status` (`out|doubtful|questionable|probable`)
- `expected_market_delta`
- `next_event_time`
- `freshness_minutes`
- `source`, `source_tier`
- `lineup_confirmed`

Execution safeguard:
- If near lock and injury source is stale/conflicting, downgrade confidence and block auto-execution.

---

## 10) User modes

- **Conservative**: high confidence threshold, low turnover, smaller size
- **Balanced**: moderate thresholds and activity
- **Opportunistic**: lower edge threshold, strict hard caps

User controls:
- `max_daily_risk`
- `max_per_market_exposure`
- banned categories
- min confidence for alerts
- auto-approval limits (if any)

---

## 11) Recommendation payload (reference)

```json
{
  "market_id": "example_market",
  "timestamp": "2026-02-26T18:40:00Z",
  "venue": "jupiter_prediction|kalshi|polymarket",
  "market_prob": 0.41,
  "agent_prob": 0.49,
  "uncertainty_band": [0.44, 0.54],
  "edge": 0.08,
  "ev_gross": 0.067,
  "fees": 0.006,
  "slippage_cost": 0.005,
  "impact_cost": 0.002,
  "resolution_risk_haircut": 0.002,
  "ev_net": 0.052,
  "confidence": 0.72,
  "action": "BUY_YES",
  "entry_range": [0.39, 0.43],
  "max_position_pct_bankroll": 0.03,
  "thesis": ["...", "..."],
  "disconfirming_evidence": ["..."],
  "invalidation": ["..."],
  "change_since_last": {"driver": "injury_update", "delta_prob": 0.03},
  "sources": [{"type": "official", "id": "..."}],
  "requires_approval": true
}
```

---


## 12) Cohesive EDGE blueprint (how it all fits together)

### End-to-end behavior
1. Ingest market, rules/resolution, news, optional injuries/social/copy feeds.
2. Estimate `p_true` and uncertainty.
3. Compute EV with venue-specific costs and resolution-risk haircut.
4. Run multi-layer qualification gates.
5. Output only one of three states:
   - `qualified` -> emit executable recommendation
   - `watchlist` -> emit what must change
   - `rejected` -> log machine-readable reject reasons
6. Enforce approval and risk limits before any execution.
7. Monitor continuously and emit concise "what changed" updates.

### What users experience
- Top 3 executable opportunities now
- Short thesis + explicit disconfirming evidence
- Clear invalidation conditions
- Fast alerts on confidence drop or thesis break
- Zero noise when there is no qualified edge

### EDGE product tiers (optional)
- **Public/Core:** executable insights + watchlist + basic rationale
- **Pro:** expanded signal feed, historical audits, API, personalized sizing
- **Advanced:** basket construction, copy-leader analytics, deeper diagnostics


### Jupiter-first operating priorities

Given your focus, EDGE should default to Jupiter prediction markets:
- Rank Jupiter opportunities first and allocate most scan budget there.
- Use faster recheck cadence for Jupiter watchlist promotions.
- Keep stricter slippage and stale-quote guards on Jupiter execution paths.
- Route Kalshi/Polymarket as secondary opportunity sets unless manually promoted.

## 13) Rollout plan

### Week 1: proposal-only
- Build nodes 1-9.
- Show only top 3 risk-adjusted opportunities.
- Log full decision traces.

### Week 2: hardening
- Add correlation/theme caps and venue-specific filters.
- Add confidence-drop + thesis-break alerts.
- Add post-resolution calibration report.

### Week 3+: controlled execution
- Enable tiny size for whitelisted users only.
- Keep approval requirement for larger sizes.
- Auto-disable on drawdown/model drift.

---

## 14) Go-live checklist

- Can the agent explain **why now**?
- Does every trade include **what proves it wrong**?
- Are all costs in `ev_net`?
- Are exposure caps enforced pre-trade?
- Are resolution-rule risks explicitly checked?
- Is there a full audit trail of estimate changes?

If yes, it is production-shaped for prediction markets.


---

## 15) One-page implementation spec (engineering handoff)

### A) Node contract table

| Node | Required inputs | Required outputs | Hard guardrails |
|---|---|---|---|
| `market_ingest` | market id, implied prob/price, spread, depth, volume, close time | normalized market snapshot | reject stale or partial books |
| `rules_resolution` | contract text, venue metadata, oracle/resolution source | ambiguity score, resolution risk score, dispute flags | block if unresolved critical rule ambiguity |
| `news_event` | approved source feed, event timestamps | structured catalysts, source quality scores | ignore unverified-only claims |
| `feature_builder` | market snapshot + catalysts + history | momentum, liquidity quality, volatility, proximity features | clamp outliers and missing-value fallbacks |
| `probability_model` | feature set, priors | `p_true`, uncertainty band, model disagreement | block if calibration drift threshold exceeded |
| `edge_ev` | `p_true`, market prob, fee schedule, slippage model | `edge`, `ev_gross`, costs, `ev_net` | reject if `ev_net <= 0` |
| `risk_policy` | recommendation draft, portfolio state, limits | pass/fail, capped size, reject reasons | enforce per-market/theme/day caps |
| `qualification_gate` | EV/risk outputs, liquidity metrics | `qualified/watchlist/rejected`, reason codes | all 5 gates must pass for `qualified` |
| `recommendation` | qualified thesis + risk output + evidence | action, entry range, invalidation, confidence payload | must include disconfirming evidence |
| `approval_gate` | recommendation + user scopes | `requires_approval`, executable flag | block execute scopes unless granted |
| `execution_monitor` | executable order intents, live market updates | order status, fill quality, thesis-break alerts | auto-stop on policy breach/drawdown |

### B) Recommendation schema (minimum required fields)

```json
{
  "market_id": "string",
  "venue": "jupiter_prediction|kalshi|polymarket",
  "timestamp": "ISO-8601",
  "market_prob": 0.0,
  "agent_prob": 0.0,
  "uncertainty_band": [0.0, 0.0],
  "edge": 0.0,
  "ev_gross": 0.0,
  "fees": 0.0,
  "slippage_cost": 0.0,
  "impact_cost": 0.0,
  "resolution_risk_haircut": 0.0,
  "ev_net": 0.0,
  "confidence": 0.0,
  "action": "BUY_YES|BUY_NO|HOLD",
  "entry_range": [0.0, 0.0],
  "max_position_pct_bankroll": 0.0,
  "thesis": ["string"],
  "disconfirming_evidence": ["string"],
  "invalidation": ["string"],
  "change_since_last": {"driver": "string", "delta_prob": 0.0},
  "reject_reason_codes": ["string"],
  "requires_approval": true
}
```

### C) Policy defaults (ship-ready)

- `max_position_pct_bankroll`: 0.01-0.03 by mode
- `max_theme_exposure`: 0.10-0.20
- `daily_loss_budget`: hard stop (user configured)
- `min_confidence_for_alert`: 0.65-0.75
- `max_slippage_bps`: venue-specific hard cap
- `ambiguity_score_max`: block above threshold

### D) Alert matrix (UI + automation)

| Alert | Trigger | Action |
|---|---|---|
| High-conviction edge | `edge > 0.05` and `confidence > 0.70` and `ev_net > 0` | notify + show executable card |
| Watchlist promotion | market moves from `watchlist` to `qualified` | push alert with “what changed” |
| Thesis break | any invalidation condition true | close/reduce recommendation |
| Risk breach | theme or daily risk cap exceeded | block new entries |
| Resolution risk spike | ambiguity/dispute score rises quickly | downgrade confidence + hold |

### E) Delivery sequence (2-sprint plan)

**Sprint 1 (core EDGE, Jupiter-first):**
- Implement nodes through `qualification_gate` in proposal-only mode for Jupiter markets first.
- Ship recommendation payload + rejection logging.
- UI: top 3 opportunities + watchlist + reason codes.

**Sprint 2 (multi-venue expansion):**
- Add approval and execution monitor paths.
- Add Kalshi + Polymarket venue adapters using same qualification/risk gates.
- Add copy/sports/social optional modules behind feature flags and post-resolution calibration dashboard.
