# Prediction Agent — Roadmap

---

## 🔨 Current Sprint (In Progress / Up Next)

### ✅ Done
- Outcome resolution layer — bot checks if signals resolved WIN/LOSS every 2h
- Paper trading system — users tap 📈 YES / 📉 NO on alerts, P&L tracked automatically
- `/performance` shows EDGE actual win rate + user paper P&L + ROI
- `/mytrades` — active paper picks + settled history + P&L per pick
- Per-user long-term memory — favorite teams, rivals, players, family, city/timezone
- New-user onboarding — AI naturally collects profile info across first few conversations
- Personalized injury alerts — concern for fav players, rivalry-aware tone
- Player Return Game Announcement — detects Out → Active transitions, sends personalized alerts
- Sports Categories Expansion — WNBA, CFB, CBB, EPL, F1 with full team aliases
- Non-Sports Markets — Crypto, Politics, Entertainment coverage in AI
- Lower Qualified: 0 rate — widened thresholds for more market qualification
- AI Response Quality — better formatting, fallbacks, common phrase handling
- Intelligent Sports Intent Detection — prediction, recap, injury, schedule, standings queries
- Paper Trading Suggestions — proactive but natural, multiple request formats

### 🔲 Up Next

**Discord: `/mytrades` slash command**
- Same data as Telegram, Discord embed format
- Color coded: green = winning direction at current price, red = losing

**Dashboard tab: "My Trades"**
- Active picks table with live market price column (vs entry)
- P&L column (unrealized — based on current market price vs entry)
- Settled picks history below with final P&L

---

## 🔮 Future Plans

### Wallet Connect — Real P&L Tracking
Instead of paper picking YES/NO manually, user connects their Polymarket wallet
and EDGE automatically imports all their real positions.

**Scope:**
- User runs `/connect 0x…` in Telegram/Discord to link their wallet
- EDGE polls that wallet's Polymarket trade history via Gamma API
- Tracks **all** trades — not just ones EDGE recommended — giving full portfolio view
- Compares user's real trades against EDGE signals (how often they follow the bot)
- `/portfolio` command shows: open positions, real P&L, EDGE alignment score
- Dashboard "Portfolio" tab mirrors this with live unrealized P&L

**Why all trades, not just agent trades:**
A trader might follow 3 signals from EDGE but also place 10 of their own bets.
Tracking everything gives the user a true P&L picture and lets EDGE give smarter
advice ("your last 5 self-directed bets lost — here's where they diverged from signals")

**Human required:** Polygon RPC endpoint (Alchemy/Infura free tier) — see HUMAN_TASKS.md

---

## Phase 1: Platform Migration (Telegram → Discord)

### Why Discord
- Role-based channel gating — charge for access, restrict by tier
- Separate channels per signal type (`#alerts`, `#injuries`, `#chat`, `#logs`)
- No 4096-char message limit — full scan output fits
- Richer embeds (color-coded by edge strength)
- Slash commands feel more professional
- Better for community / subscription model

### What Stays the Same
All core logic is untouched — scan engine, injury pipeline, win-prob math,
BallDontLie, Kalshi/Polymarket adapters. Only the bot layer changes.

### Work Required
| Task | Est. Effort |
|------|-------------|
| Swap `python-telegram-bot` for `discord.py` | 1 day |
| Rebuild command handlers as slash commands | 1 day |
| Rebuild alert formatting as Discord embeds (color by edge) | 1 day |
| Per-user state + subscription role gating | 2-3 days |
| Onboarding flow (`/start` explanation, `/help` that actually explains edge) | 1 day |
| Channel routing (alerts → #alerts, injuries → #injuries, etc.) | 0.5 day |

---

## Phase 2: Hosting Migration

### Option A — Railway (Already Have Access)
- **Cost:** $5/month always-on
- **Ease:** ✅ Easiest — connect GitHub repo, auto-deploys on every `git push`
- **Setup:** Add `Procfile` and `railway.json` to repo (~10 min)
- **Best for:** Getting live fast with zero server config

**Files needed:**
```
# Procfile
worker: python run_edge_bot.py
```
```json
// railway.json
{
  "build": { "builder": "NIXPACKS" },
  "deploy": { "startCommand": "python run_edge_bot.py", "restartPolicyType": "ON_FAILURE" }
}
```

### Option B — Oracle Cloud Free Tier (Preferred — $0 Forever)
- **Cost:** $0 — Always Free tier, no expiry
- **Specs:** 4 ARM cores + 24GB RAM (massively overpowered for this bot)
- **Ease:** ⚠️ Medium — requires SSH setup + systemd service (~1 hour first time)
- **Best for:** Long-term free hosting with full control

**Setup steps (save for later):**
1. Sign up at cloud.oracle.com → Create Always Free VM (Ampere ARM shape)
2. SSH into the VM
3. `sudo apt update && sudo apt install python3-pip python3-venv git -y`
4. `git clone https://github.com/samuraifrenchienft/Prediction-Agent.git`
5. `cd Prediction-Agent && python3 -m venv venv && source venv/bin/activate`
6. `pip install -r requirements.txt`
7. Copy `.env` file to server (never commit it)
8. Create systemd service so bot restarts on crash/reboot:

```ini
# /etc/systemd/system/prediction-agent.service
[Unit]
Description=Prediction Agent Discord Bot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/Prediction-Agent
ExecStart=/home/ubuntu/Prediction-Agent/venv/bin/python run_edge_bot.py
Restart=always
RestartSec=10
EnvironmentFile=/home/ubuntu/Prediction-Agent/.env

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable prediction-agent
sudo systemctl start prediction-agent
sudo systemctl status prediction-agent  # verify running
```

**Verdict:** Start on Railway to move fast, migrate to Oracle when ready for permanent free hosting.

---

## Phase 3: Product Readiness (Before Charging Subscribers)

### Critical Gaps
| Gap | Priority | Est. Effort | Status |
|-----|----------|-------------|--------|
| **`/performance` command** — win rate, avg edge, ROI since launch | 🔴 High | 2 days | ✅ Done |
| **Outcome resolution** — did signals actually win? | 🔴 High | 2 days | ✅ Done |
| **Paper trading** — users pick YES/NO, P&L tracked | 🔴 High | 1 day | ✅ Done |
| **`/mytrades`** — view active paper picks (Telegram + Discord) | 🔴 High | 1 day | 🔲 Next |
| **Dashboard "My Trades" tab** — live active picks + history | 🟠 Medium | 2 days | 🔲 Next |
| **Lower "Qualified: 0" rate** — widen confidence threshold or more markets | 🔴 High | 1 day | 🔲 |
| **Polymarket CLOB spread/liquidity** — read bid/ask not just last price | 🟠 Medium | 2-3 days | 🔲 |
| **Multi-user per-user state** — each subscriber gets their own alert stream | 🟠 Medium | 2-3 days | 🔲 |
| **Persistent alert history** — survives restarts, queryable | 🟡 Low | 1 day | 🔲 |
| **Non-sports Polymarket** — crypto, politics, culture markets | 🟡 Low | 2 days | 🔲 |
| **Wallet connect** — real P&L, all Polymarket trades auto-imported | 🔵 V2 | 3-5 days | Future |
| **Trade execution bridge** — one-click to Kalshi/Polymarket via API | 🔵 V2 | 3-5 days | Future |

### Minimum Viable Subscription (what you need before charging)
1. Bot runs 24/7 on a server (not your desktop)
2. `/performance` showing a real track record
3. Alert rate > 0 most days (lower the threshold)
4. Multi-user gating via Discord roles
5. Onboarding that explains what edge means and how to use it

---

## Notes
- Core engine (scan, injury, win-prob) is solid — don't over-engineer it
- Focus on output quality and alert rate before adding more data sources
- Railway first, Oracle later — get live before perfecting infrastructure
