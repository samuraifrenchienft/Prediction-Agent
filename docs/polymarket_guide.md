# Polymarket — User Guide for EDGE Agent Reference

## What is Polymarket?
Polymarket is a decentralized prediction market platform on the Polygon blockchain.
Users buy YES or NO shares on real-world events (politics, sports, crypto, economics).
Each share pays $1 if correct, $0 if wrong. The share price = market's implied probability.

Example: "Will the Fed cut rates in June?" trading at $0.63 YES = 63% implied probability.

---

## How to Sign Up

1. Go to polymarket.com
2. Click "Sign Up" — options:
   - **Magic Link** (email): simplest — no wallet needed upfront
   - **MetaMask / Coinbase Wallet / WalletConnect**: for self-custody users
3. Once logged in, Polymarket creates a **proxy wallet** (a smart contract wallet) for you automatically
4. Your proxy wallet address is what EDGE tracks with /wallet

**Age restriction**: Must be 18+. US users can access but regulated activity varies by state — check local laws.

---

## How to Deposit USDC

Polymarket runs on **Polygon** (not Ethereum mainnet). You need USDC on Polygon.

### Option A — Direct deposit via Polymarket bridge (easiest)
1. Log in → click "Deposit"
2. Select "Bridge from Ethereum" or "Buy with card" (via MoonPay/Stripe)
3. Min deposit: ~$1 USDC
4. Card fees: ~2-4% via MoonPay

### Option B — From Coinbase (cheapest)
1. Buy USDC on Coinbase
2. Send to your Polymarket wallet address **on Polygon network**
   - In Coinbase: Send → enter Polymarket address → select **Polygon** as network
   - Do NOT send on Ethereum mainnet — funds will be stuck until bridged
3. USDC arrives in ~1-2 minutes

### Option C — Bridge from Ethereum mainnet
1. Use official Polygon bridge: wallet.polygon.technology
2. Or use 3rd party: across.to, stargate.finance (faster, ~5 min)
3. Bridging costs gas (~$5-15 on Ethereum side)

---

## Fees

- **Trading fee**: 0% (Polymarket charges no fee on trades)
- **Withdrawal**: Small gas fee on Polygon (cents, not dollars)
- **Bridge out to Ethereum**: Gas on Ethereum side (~$5-20 depending on congestion)
- **Card deposit via MoonPay**: ~2-4%

---

## What are YES and NO shares?

- **YES share at $0.40**: costs $0.40, pays $1 if event happens → profit $0.60
- **NO share at $0.60**: costs $0.60, pays $1 if event does NOT happen → profit $0.40
- The market price reflects crowd consensus probability
- When event resolves, winning side gets $1/share, losing side gets $0

---

## How to Trade

1. Find a market using the search bar
2. Click YES or NO
3. Enter dollar amount (min ~$1)
4. Review order → click "Buy"
5. Transaction confirms on Polygon in ~2 seconds

**Limit orders**: Click the "..." menu to set a specific price (e.g., buy YES only at $0.35 or below)

---

## How to Withdraw

1. Click your profile → "Withdraw"
2. Choose: Polygon USDC (instant, cents fee) or bridge to Ethereum (~$10 gas, ~15 min)
3. Polymarket does NOT support direct bank withdrawals — withdraw to crypto wallet, then convert on Coinbase/Kraken

---

## Useful Terms

| Term | Meaning |
|---|---|
| USDC | USD-pegged stablecoin used for all trades |
| Proxy wallet | Your Polymarket smart contract wallet (tracked by EDGE) |
| CLOB | Central Limit Order Book — how Polymarket matches orders |
| Polygon | Layer 2 blockchain Polymarket runs on (fast, cheap gas) |
| Resolved | Market has been settled and winners paid out |
| Liquidity | How easy it is to buy/sell without moving the price |

---

## Tips for New Users

- Start with liquid markets (high volume) — easier to enter/exit
- Avoid markets with wide bid-ask spreads (>10 cents gap)
- Use /traders to find smart money wallets worth following
- Use /scan to see markets where EDGE has detected a mispricing
- Never trade more than you can afford to lose — prediction markets are inherently uncertain
