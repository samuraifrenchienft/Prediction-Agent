"""
Edge Knowledge Base — SQLite FTS5 full-text search.

Stores static platform guides, market education, and series references.
Searched on every Q&A call to inject relevant context into the AI prompt.
Database lives at edge_agent/memory/data/knowledge.db and is auto-populated
on first run. Add new docs by calling kb.add_doc() or editing DOCS below.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

_DB_PATH = Path(__file__).parent / "data" / "knowledge.db"

# ── Knowledge Documents ────────────────────────────────────────────────────────
# Each doc: (title, category, tags, content)
# Keep content concise — these get injected into AI prompts.

DOCS: list[tuple[str, str, str, str]] = [

    # ── What is a prediction market ──────────────────────────────────────────
    (
        "What is a prediction market?",
        "education",
        "prediction market basics intro beginner what how works",
        """A prediction market is a platform where people buy and sell contracts that pay out based on
whether a real-world event happens. If you buy YES at 40¢ and the event happens, you get $1 — a 60¢ profit.
If it doesn't happen, you lose your 40¢. The market price reflects the crowd's implied probability.
Example: A contract trading at 65¢ means the market believes there's a 65% chance the event resolves YES.
Key platforms: Polymarket (crypto-native, Polygon blockchain) and Kalshi (CFTC-regulated US exchange).
Prediction markets are used for hedging, speculation, and as forecasting tools — they often outperform polls.""",
    ),

    # ── How to read probability / odds ───────────────────────────────────────
    (
        "How to read probabilities and odds",
        "education",
        "probability odds price how read interpret percent chance",
        """Market price = implied probability. A YES contract at 72¢ = 72% implied chance of YES.
NO contract price = 1 - YES price. If YES is 72¢, NO is 28¢ (they sum to ~$1 minus fees).
Finding edge: If you believe true probability is 80% but market shows 72%, that's an 8-point edge.
Key terms:
- Bid: highest price someone will buy YES
- Ask: lowest price someone will sell YES
- Spread: ask minus bid (cost to trade in/out)
- Last price: most recent trade price
- Volume: total dollars traded (higher = more liquid, more reliable price)
A wide spread (e.g. 10+ cents) means the market is illiquid — be careful entering.""",
    ),

    # ── What is liquidity and spread ─────────────────────────────────────────
    (
        "Liquidity and spread explained",
        "education",
        "liquidity spread depth slippage thin market illiquid",
        """Liquidity = how easily you can enter/exit a position without moving the price.
Spread = ask price minus bid price. Tight spread (1-2¢) = liquid. Wide spread (10+¢) = illiquid.
Depth = total dollar value available on each side of the order book.
Why it matters: In an illiquid market your buy order can move the price against you (slippage).
Rule of thumb: Only trade markets with >$10k volume and spread under 5¢ unless you have strong conviction.
Kalshi tends to have tighter spreads on regulated markets (Fed, CPI, GDP).
Polymarket has higher volume on politics and crypto but wider spreads on niche markets.""",
    ),

    # ── Market resolution ────────────────────────────────────────────────────
    (
        "How prediction markets resolve",
        "education",
        "resolution resolve settle outcome criteria result close",
        """Markets resolve based on pre-defined resolution criteria written before trading begins.
Binary markets: resolve YES ($1) or NO ($0).
Resolution sources vary: official government data (CPI, GDP), sports scores, election results.
Important: Read the resolution criteria before trading — ambiguous wording can cause disputes.
Kalshi: Resolves according to the official data source listed (e.g. BLS for CPI, Fed statement for rates).
Polymarket: Uses UMA oracle protocol — a decentralized dispute/resolution system.
Timing: Most markets resolve within hours or days of the event. Long-dated markets (elections) take longer.
If the event is cancelled or doesn't occur as described, most markets resolve NO or are voided.""",
    ),

    # ── Risk management / Kelly ───────────────────────────────────────────────
    (
        "Risk management and bankroll sizing",
        "education",
        "kelly criterion risk bankroll sizing position bet size drawdown",
        """Kelly Criterion: Optimal bet size = (edge / odds). Example: 60% true prob, 50¢ market price.
Edge = 60% - 50% = 10%. Odds = 1/0.5 = 2x. Kelly = 10% / 2 = 5% of bankroll per trade.
In practice: Use half-Kelly (2.5% here) to reduce variance. Never go above 10% on one position.
Diversification: Spread across uncorrelated markets (sports, macro, politics) — don't put 80% on one election.
Daily drawdown limit: Set a max loss per day (e.g. 2% of bankroll). If hit, stop trading for the day.
Never chase losses. Prediction markets reward patience and discipline over action.
Edge tracks: spread_bps, depth_usd, volume_24h — use these to screen out illiquid markets.""",
    ),

    # ── Polymarket account setup ──────────────────────────────────────────────
    (
        "How to create a Polymarket account",
        "polymarket",
        "polymarket account setup create sign up register wallet deposit how to start",
        """Polymarket is a decentralized prediction market on the Polygon blockchain. No US ID required.
Step 1: Go to polymarket.com — click 'Sign In' top right.
Step 2: Connect a crypto wallet (MetaMask, Coinbase Wallet, or use Magic Link with email — easiest for beginners).
Step 3: Magic Link option: enter your email, receive a link, Polymarket creates a wallet for you automatically.
Step 4: To deposit — click 'Deposit' and add USDC on the Polygon network.
  - From Coinbase: send USDC selecting Polygon network (cheaper fees than Ethereum).
  - Minimum deposit: $1 (but $10+ recommended to cover gas fees).
Step 5: Once USDC appears in your balance, you're ready to trade.
Note: All balances are in USDC (USD stablecoin). Winnings paid in USDC, withdrawable to any wallet.""",
    ),

    # ── Polymarket UI guide ───────────────────────────────────────────────────
    (
        "Polymarket UI guide — how to navigate",
        "polymarket",
        "polymarket interface ui navigate browse market page portfolio trade order",
        """Polymarket home page: Shows trending markets sorted by volume. Use category filters (Politics, Sports, Crypto, etc.).
Market page layout:
- Top: Market question and resolution criteria (READ THIS before trading)
- Chart: Price history of YES contract
- Order book: Current bids and asks
- Right panel: Buy YES / Buy NO buttons with amount input
- Activity tab: Recent trades and who's trading
Placing a trade:
1. Click the market you want
2. Choose YES or NO
3. Enter dollar amount
4. Review the price and estimated shares
5. Click 'Buy' and confirm in your wallet
Portfolio tab (top nav): Shows your open positions, P&L, and trade history.
My Markets: Track markets you've traded or bookmarked.
Tip: Sort by 'Volume' on the homepage to find the most liquid markets.""",
    ),

    # ── Kalshi account setup ──────────────────────────────────────────────────
    (
        "How to create a Kalshi account",
        "kalshi",
        "kalshi account setup create sign up register deposit how to start kyc",
        """Kalshi is a CFTC-regulated prediction exchange — US users welcome, ID verification required.
Step 1: Go to kalshi.com — click 'Sign Up'.
Step 2: Enter your email and create a password.
Step 3: Verify your email via the link sent to your inbox.
Step 4: Complete KYC (Know Your Customer): provide your full name, date of birth, SSN last 4 digits, and address. This is required by US regulations. Takes 1-2 minutes.
Step 5: Deposit funds — go to 'Wallet' then 'Deposit'.
  - Bank transfer (ACH): Free, takes 1-3 business days. Minimum $5.
  - Debit card: Instant, small fee. Minimum $5.
Step 6: Once funds clear, go to 'Markets' and start trading.
Note: Kalshi is USD-native (not crypto). Winnings paid to your Kalshi wallet, withdraw to bank anytime.""",
    ),

    # ── Kalshi UI guide ───────────────────────────────────────────────────────
    (
        "Kalshi UI guide — how to navigate",
        "kalshi",
        "kalshi interface ui navigate browse market trade portfolio order book",
        """Kalshi home page: Featured markets and categories — Politics, Economics, Weather, Sports, Finance.
Market page layout:
- Header: Question title and resolution date
- Resolution criteria box: Exactly how/when the market settles (important!)
- Price chart: YES price history
- Order book: Shows available YES/NO orders at each price level
- Trade panel: Enter quantity of contracts (each contract = $1 face value)
Placing a trade:
1. Select a market from the Markets page
2. Choose YES or NO tab in the trade panel
3. Set order type: Market (instant fill) or Limit (set your price)
4. Enter number of contracts
5. Review total cost and click 'Buy'
Portfolio page: Your open positions, closed trades, and P&L history.
Kalshi Markets categories:
- Economics: Fed rates (KXFED), CPI inflation (KXINFL), GDP (KXGDP)
- Politics: Presidential (KXPRES), Congressional races
- Crypto: Bitcoin price (KXBTC), Ethereum (KXETH)
- Sports: NBA (KXNBA), NFL (KXNFL)
- Weather: High temperatures (KXHIGHNY for NYC)""",
    ),

    # ── Kalshi series reference ───────────────────────────────────────────────
    (
        "Kalshi series reference guide",
        "kalshi",
        "KXFED KXBTC KXETH KXINFL KXGDP KXPRES KXNBA KXNFL KXHIGHNY series ticker meaning",
        """Kalshi market series — each series has recurring contracts tied to a specific event type:

KXFED — Federal Reserve rate decision. Resolves based on FOMC statement.
  Contracts: Will the Fed cut/hold/hike at the next meeting? High volume, tight spreads.

KXINFL — US CPI inflation. Resolves to BLS Consumer Price Index data.
  Contracts: Will CPI be above/below X% next month?

KXGDP — US GDP growth. Resolves to BEA advance GDP estimate.
  Contracts: Will Q[X] GDP be above/below X%?

KXBTC — Bitcoin price. Resolves to Coinbase BTC/USD closing price.
  Contracts: Will BTC be above/below $X at end of [period]?

KXETH — Ethereum price. Same structure as KXBTC but for ETH.

KXPRES — US Presidential election and approval markets.
  Contracts: Who will win? Approval ratings above/below threshold?

KXNBA — NBA basketball. Championship winner, series results.
  Contracts: Will [team] win the NBA championship? High volume during playoffs.

KXNFL — NFL football. Super Bowl winner, game outcomes.
  Contracts: Will [team] win the Super Bowl?

KXHIGHNY — NYC high temperature. Daily temperature contracts.
  Contracts: Will NYC high temp be above/below X°F on [date]?""",
    ),

    # ── Binary vs scalar markets ──────────────────────────────────────────────
    (
        "Binary vs scalar markets explained",
        "education",
        "binary scalar range market type yes no range outcome contract structure",
        """Binary markets: Two outcomes only — YES or NO. Contract pays $1 if YES, $0 if NO.
Example: 'Will Bitcoin be above $100,000 on Dec 31?' — resolves YES or NO.
Most Kalshi and Polymarket contracts are binary.

Scalar/Range markets: Resolve to a value on a scale, not just YES/NO.
Example: 'What will CPI be in March?' — payoff depends on the exact number.
Less common, but Kalshi has some range markets on economic data.

Multi-outcome markets: Polymarket uses these for elections — one contract per candidate.
Only one outcome pays $1, all others go to $0. Sum of all contract prices ≈ $1.
Example: Candidate A at 55¢, Candidate B at 44¢, Other at 1¢ — sum = 100¢.
Strategy differs: In multi-outcome markets, buying the longshot at 1¢ is high risk/reward.""",
    ),

    # ── How to find edges ─────────────────────────────────────────────────────
    (
        "How to find edges in prediction markets",
        "strategy",
        "edge advantage mispricing alpha signal strategy how find exploit inefficiency",
        """An 'edge' is when your estimated probability differs meaningfully from the market price.
Edge = your_probability - market_probability. Positive = bet YES, Negative = bet NO.

Sources of edge:
1. News lag: Market hasn't updated to breaking news yet. Act fast — disappears in minutes.
2. Model disagreement: Your analysis of data (polls, economic models) differs from market consensus.
3. Liquidity illusion: Thin market with wide spread — careful, may be mispriced for a reason.
4. Recency bias: Market overweights recent events. Fade extreme moves.
5. Favorite-longshot bias: Favorites often slightly underpriced, longshots often overpriced.

Edge's signals (what this bot tracks):
- Spread vs volume ratio (wide spread on high-volume = opportunity)
- AI-estimated probability vs market implied probability
- News catalyst quality and direction
- Time to resolution (short-term markets reprice faster)

Rule: Only bet when edge > 5 percentage points AND market is liquid (>$10k volume, <5¢ spread).""",
    ),

    # ── Comparing Kalshi vs Polymarket ────────────────────────────────────────
    (
        "Kalshi vs Polymarket comparison",
        "education",
        "kalshi polymarket difference compare which better platform choose",
        """Kalshi:
- CFTC-regulated US exchange. Requires ID verification (KYC).
- USD deposits via bank/debit. No crypto needed.
- Tighter spreads on macro markets (Fed, CPI, GDP).
- Lower volume overall but more reliable for regulated events.
- API available with RSA key auth. Great for automated trading.
- Best for: US economic data, Fed decisions, regulated market types.

Polymarket:
- Decentralized, runs on Polygon blockchain. No KYC (US residents technically restricted but widely used).
- Requires USDC (USD stablecoin) — need a crypto wallet.
- Much higher volume on politics and crypto markets.
- More exotic markets — any topic imaginable.
- REST API available (Gamma API), no auth required.
- Best for: Elections, crypto prices, sports, high-volume political markets.

Summary: Use Kalshi for macro/economic markets. Use Polymarket for political/crypto/sports with high volume.""",
    ),

    # ── What is a good win rate ───────────────────────────────────────────────
    (
        "What is a good win rate in prediction markets?",
        "education",
        "win rate good winning percentage break even profitable roi returns",
        """Win rate needed to be profitable depends on what odds you're buying at:
- Buying at 50% (even money): need >50% win rate to profit
- Buying at 40% (underdog): need >40% win rate to profit (higher payout offsets lower hit rate)
- Buying at 70% (favorite): need >70% win rate to profit (lower payout means you need to hit often)

Benchmarks for prediction market traders:
- Break-even: match your average entry price as a win rate
- Good: 55-60% win rate at near 50/50 markets
- Elite: 60%+ sustained over 50+ resolved bets
- Top Polymarket wallets tracked by EDGE average 58-65% win rate

Why win rate alone is misleading: a trader who only bets 95% favorites can have a 94% win rate and still lose money. Focus on ROI (return on investment) and EV (expected value) instead.
Rule of thumb: if your win rate consistently beats the market price you entered at, you have edge.""",
    ),

    # ── Vig, juice, and sportsbook odds explained ─────────────────────────────
    (
        "What is vig, juice, and how to read American odds?",
        "education",
        "vig juice -110 american odds moneyline plus minus sportsbook what means",
        """American odds tell you how much you win relative to your bet:
- Negative odds (-110, -200): how much you must bet to win $100
  -110 means bet $110 to win $100 profit (risk more to win less — you're the favorite)
  -200 means bet $200 to win $100 profit (heavy favorite)
- Positive odds (+130, +300): how much you win if you bet $100
  +130 means bet $100 to win $130 profit (underdog)
  +300 means bet $100 to win $300 profit (big underdog)

Implied probability from American odds:
- Negative: implied% = |odds| / (|odds| + 100) → -200 = 200/300 = 66.7%
- Positive: implied% = 100 / (odds + 100) → +130 = 100/230 = 43.5%

Vig (juice): the bookmaker's cut. A standard -110/-110 line adds up to 104.8% — the extra 4.8% is the vig. It's why sportsbooks profit regardless of outcome.
Vig-free line: remove the vig to get true implied probability. At -110/-110, each side is truly 50%.

Prediction markets like Polymarket have no vig — prices reflect pure crowd probability.""",
    ),

    # ── Bankroll management and bet sizing ────────────────────────────────────
    (
        "How much should I bet? Bankroll management guide",
        "education",
        "how much bet bankroll size kelly criterion unit risk management stake",
        """Never risk more than you can afford to lose. Start small — even $5-10 per trade teaches you the mechanics.

Simple unit system (recommended for beginners):
- Set your bankroll (e.g. $100 total)
- 1 unit = 1-2% of bankroll ($1-2 on $100)
- Standard bet: 1 unit. High confidence: 2 units. Never exceed 5 units on one trade.

Kelly Criterion (math-based sizing):
Kelly% = (edge / odds_received)
Example: You think 70% chance, market at 60% → edge = 10%, odds = 60¢ payout on $1
Kelly% = 0.10 / 1.67 = 6% of bankroll. Use half-Kelly (3%) for safety.

EDGE shows kelly-adjusted sizing in every alert. Look for the "Recommended size" field.

Rules for beginners:
1. Never put more than 5% of bankroll on one market
2. Diversify — 10-20 small positions beats 2 large ones
3. Paper trade first until you hit 20+ resolved bets with positive ROI
4. Scale up only after proving consistent edge""",
    ),

    # ── Disputed market resolution ────────────────────────────────────────────
    (
        "What happens if a prediction market resolves disputed or wrong?",
        "education",
        "disputed resolution wrong resolve invalid void cancel refund NaN",
        """Disputed resolutions do happen — here's what to expect on each platform:

Polymarket:
- Uses UMA (Universal Market Access) as the resolution oracle
- Anyone can dispute a resolution by staking UMA tokens within 48 hours
- Disputed markets go to a vote among UMA token holders (~72h process)
- If overturned, all positions are refunded at original entry price (no profit/loss)
- If upheld, original resolution stands
- Edge cases: if a market question is ambiguous or the event is too close to call, UMA may resolve N/A — all positions refunded

Kalshi:
- CFTC-regulated — Kalshi itself is the resolution authority
- Resolution disputes go through Kalshi's formal appeals process
- Email support@kalshi.com within 24h of resolution
- If Kalshi made an error, positions are corrected and P&L adjusted
- N/A resolutions: all contracts settle at $0.50 (halfway) — not a full refund
- Kalshi has a legal obligation to resolve accurately under CFTC rules

Best practices:
- Read the resolution criteria BEFORE buying (shown on every market page)
- Avoid markets with vague resolution criteria ("Will X happen soon?")
- After a market resolves, check your portfolio immediately — credits appear within minutes on Polymarket, within 1 business day on Kalshi""",
    ),

    # ── Getting started checklist ─────────────────────────────────────────────
    (
        "Getting started checklist — new user guide",
        "education",
        "getting started new user first time checklist beginner guide onboarding step by step",
        """Welcome to prediction markets. Here's the fastest path from zero to your first trade:

Step 1 — Pick a platform
- Polymarket: best for variety, no US restrictions on most markets, crypto wallet required
- Kalshi: best for US users, regulated, bank deposit accepted, easier for beginners

Step 2 — Create an account (5 min)
- Polymarket: go to polymarket.com → connect wallet (MetaMask or use Magic Link email login)
- Kalshi: go to kalshi.com → sign up → verify ID (KYC, takes ~1 day)

Step 3 — Deposit (minimum ~$10-20 to start)
- Polymarket: deposit USDC (buy on Coinbase or Kraken → send to your Polygon wallet)
- Kalshi: link bank account → ACH transfer (free, 1-3 days)

Step 4 — Find a market
- Use /scan or /top in this bot for high-edge opportunities
- Or browse Polymarket/Kalshi directly and pick something you have a strong opinion on

Step 5 — Paper trade first
- Tap YES or NO on any EDGE alert to paper trade it risk-free
- Track results with /mytrades and /performance
- Wait for 10-20 resolved bets before committing real money

Step 6 — Make your first real trade
- Start with 1 unit (1-2% of bankroll)
- Buy during high-liquidity hours (US market hours, 9am-5pm ET)
- Check the spread — if bid/ask spread is >5%, consider waiting

You can ask me anything at any stage — setup, deposits, strategy, or understanding a specific market.""",
    ),

    # ── Withdrawals ───────────────────────────────────────────────────────────
    (
        "How to withdraw winnings",
        "education",
        "withdraw withdrawal cash out winnings profit money bank wallet",
        """Polymarket withdrawals:
- Go to your profile → Withdraw
- Enter amount of USDC to withdraw
- Choose destination wallet address (MetaMask, Coinbase, etc.)
- Polygon transactions are fast (seconds) and cheap (<$0.01 fee)
- From your wallet, you can swap USDC to USD on Coinbase and bank transfer

Kalshi withdrawals:
- Go to Wallet → Withdraw
- Select bank account (must be same account used for deposit, first time)
- ACH transfer: Free, 1-3 business days
- Minimum withdrawal: $1
- No limits on withdrawal frequency

Taxes: In the US, prediction market winnings are taxable. Keep records of all trades.
Kalshi sends 1099 forms if winnings exceed $600. Polymarket does not (it's decentralized).""",
    ),
]


class KnowledgeBase:
    """
    SQLite FTS5 knowledge base for Edge.
    Searches docs by relevance to a user question and returns
    formatted context for injection into the AI prompt.
    """

    def __init__(self, db_path: Path = _DB_PATH) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._setup()

    def _setup(self) -> None:
        c = self._conn
        # Regular table for metadata
        c.execute("""
            CREATE TABLE IF NOT EXISTS docs (
                id      INTEGER PRIMARY KEY,
                title   TEXT NOT NULL,
                category TEXT NOT NULL,
                tags    TEXT NOT NULL,
                content TEXT NOT NULL
            )
        """)
        # FTS5 virtual table for full-text search
        c.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts
            USING fts5(title, tags, content, content=docs, content_rowid=id)
        """)
        c.commit()
        # Seed docs if empty
        count = c.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
        if count == 0:
            self._seed_docs()

    def _seed_docs(self) -> None:
        c = self._conn
        for title, category, tags, content in DOCS:
            c.execute(
                "INSERT INTO docs (title, category, tags, content) VALUES (?, ?, ?, ?)",
                (title, category, tags, content),
            )
        # Rebuild FTS index
        c.execute("INSERT INTO docs_fts(docs_fts) VALUES('rebuild')")
        c.commit()

    def add_doc(self, title: str, category: str, tags: str, content: str) -> None:
        """Add a new doc to the knowledge base."""
        c = self._conn
        rowid = c.execute(
            "INSERT INTO docs (title, category, tags, content) VALUES (?, ?, ?, ?)",
            (title, category, tags, content),
        ).lastrowid
        c.execute("INSERT INTO docs_fts(rowid, title, tags, content) VALUES (?, ?, ?, ?)",
                  (rowid, title, tags, content))
        c.commit()

    def search(self, query: str, limit: int = 3) -> list[dict]:
        """Full-text search. Returns list of {title, category, content} dicts."""
        # Build FTS5 OR query from individual words (removes punctuation/numbers)
        import re
        words = re.findall(r"[a-zA-Z]{3,}", query.lower())
        if not words:
            return []
        fts_query = " OR ".join(words)
        try:
            rows = self._conn.execute(
                """
                SELECT d.title, d.category, d.content
                FROM docs_fts f
                JOIN docs d ON d.id = f.rowid
                WHERE docs_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (fts_query, limit),
            ).fetchall()
            return [{"title": r[0], "category": r[1], "content": r[2]} for r in rows]
        except Exception:
            return []

    def get_context_for_question(self, question: str, max_chars: int = 1200) -> str:
        """
        Returns a formatted string of relevant knowledge base entries
        ready to inject into an AI prompt. Returns empty string if nothing relevant.
        """
        results = self.search(question, limit=3)
        if not results:
            return ""

        parts = ["\n\nRelevant knowledge base context:"]
        total = 0
        for r in results:
            snippet = f"\n[{r['title']}]\n{r['content'].strip()}"
            if total + len(snippet) > max_chars:
                break
            parts.append(snippet)
            total += len(snippet)

        return "\n".join(parts)

    def stats(self) -> dict:
        count = self._conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
        cats = self._conn.execute(
            "SELECT category, COUNT(*) FROM docs GROUP BY category"
        ).fetchall()
        return {"total_docs": count, "by_category": dict(cats)}

    def close(self) -> None:
        self._conn.close()
