# Human-Required Tasks

Tasks that cannot be automated and require manual action by the project owner.
Complete these after the current bot development sprint is stable.

---

## 🔴 Critical (Bot won't work without these)

- [ ] **Telegram Bot Token** — ensure `TELEGRAM_BOT_TOKEN` is set in `.env`
- [ ] **Approved Chat IDs** — ensure `TELEGRAM_CHAT_IDS` in `.env` contains your Telegram user ID(s)
- [ ] **Claude API Key** — ensure `ANTHROPIC_API_KEY` is set in `.env` for AI chat responses

---

## 🟡 High Priority (Significant feature gaps without these)

- [ ] **Polygon RPC upgrade** (optional but recommended)
  - The fresh-wallet checker (`edge_agent/vetting/wallet_chain.py`) uses free public RPCs
  - Public RPCs can be slow or rate-limited under load
  - Get a free key from [Alchemy](https://alchemy.com) or [Infura](https://infura.io)
  - Add your Polygon HTTPS RPC URL to the top of `_RPC_URLS` list in `wallet_chain.py`

- [ ] **Tavily API Key** (web search for AI chat)
  - Sign up at [tavily.com](https://tavily.com) — 1,000 free searches/month
  - Add to `.env` as `TAVILY_API_KEY`
  - Without this, sport/injury web search falls back to Serper (also needs key)

- [ ] **Serper API Key** (Google search fallback)
  - Sign up at [serper.dev](https://serper.dev) — 2,500 free searches/month
  - Add to `.env` as `SERPER_API_KEY`

- [ ] **Trader cache warmup**
  - After restarting the bot, run `/traders` once to trigger a live rescore
  - Or wait for the daily 8am PT auto-refresh job to populate the cache
  - Without a warm cache, `/traders` falls back to live scoring (~30s delay)

---

## 🟠 Medium Priority (Quality of life / accuracy)

- [ ] **Review trader trust score thresholds**
  - Currently: ✅ ≥75, 🟡 55-74, 🔴 <55
  - After running for a week, evaluate if these thresholds match real trader quality
  - Adjust `_BOT_WIN_RATE_CEILING` and score weights in `trader_api.py` if needed

- [ ] **Review fresh-wallet penalty values**
  - `wallet_chain.py` penalizes new wallets (0-0.25 deducted from trust score)
  - If legitimate new smart money traders are being penalized too heavily, reduce `_FRESH_NONCE_THRESHOLD` from 10 to 5
  - Monitor for false positives

- [ ] **Polymarket docs accuracy check**
  - Review `docs/polymarket_guide.md` — verify fee info, deposit methods, and limits are current
  - Polymarket UI changes frequently; update as needed

- [ ] **Kalshi docs accuracy check**
  - Review `docs/kalshi_guide.md` — verify the ~7% fee structure and deposit limits
  - Check Kalshi's current supported states/countries for international users

- [ ] **Trader specialization labels**
  - The `/traders` command shows top 2 categories per trader (e.g., "Politics, Sports")
  - Verify these labels match real trader behavior after a few days of data
  - Categories come from `market_category` field in Polymarket trade data

---

## 🟢 Future / Nice to Have

- [ ] **Scraper repo integration** — the `third_party/` repos are cloned locally for reference
  - `polymarket-insider-tracker`: review DBSCAN clustering code for coordinated wallet detection
  - `poly_data`: consider running the Goldsky scraper to build a local historical dataset
  - Requires: PostgreSQL or SQLite for storage, scheduled cron job to update

- [ ] **Kalshi API credentials** — for live Kalshi market data in scan
  - The bot has Kalshi integration but may need valid API keys
  - Check `kalshi_private_key.pem` is present and `.env` has `KALSHI_API_KEY_ID`

- [ ] **NewsAPI key** — for news-driven catalyst detection
  - Add `NEWSAPI_KEY` to `.env` if not already set

- [ ] **Trader docs page** — build a simple public-facing explainer page
  - "What is trust score?", "What does 45/100 mean?", "What is a fade score?"
  - Could be a GitHub Pages site or Notion page, linked from the bot's /help

- [ ] **Bot username / description** — update in @BotFather on Telegram
  - Set bot description to explain what EDGE is
  - Set bot commands list via BotFather so Telegram shows command autocomplete

---

## Notes

- All `.env` secrets are gitignored — never commit them
- The `third_party/` directory is gitignored — repos are local reference only
- Human tasks are tracked here; code TODOs are in `ROADMAP.md`
