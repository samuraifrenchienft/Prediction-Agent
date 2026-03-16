"""
Trader API Client — Polymarket wallet vetting & hot-trader discovery.
=====================================================================

Data sources (all public, no auth required):
  Polymarket Data API  https://data-api.polymarket.com
    /v1/leaderboard    — ranked traders by PnL/volume
    /v1/trades         — full trade history per wallet
    /v1/positions      — current open positions per wallet
  Polymarket Gamma API https://gamma-api.polymarket.com
    /markets           — market metadata (endDate, resolved)
  Polymarket CLOB API  https://clob.polymarket.com
    /price/{token_id}  — current mid-price for any token

Scoring formula
---------------
  final_score = anti_bot × 0.25 + performance × 0.50 + reliability × 0.25
  All sub-scores are 0.0–1.0; final_score displayed as 0–100.

Hidden-loss detection
---------------------
  Any open position where:
    - market endDate < today  (market should have resolved)
    - market resolved = False  (hasn't settled yet)
    - current token price < $0.15  (almost certainly a loss)
  → counted as an anticipated loss and deducted from adjusted PnL.
  The bigger the gap between stated PnL and adjusted PnL, the lower
  the reliability score.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any

import requests

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DATA_API    = "https://data-api.polymarket.com"
_GAMMA_API   = "https://gamma-api.polymarket.com"
_CLOB_API    = "https://clob.polymarket.com"

_CACHE_DIR   = ".cache"
os.makedirs(_CACHE_DIR, exist_ok=True)

_LB_TTL      = 14400   # 4 h  — leaderboard file cache
_MARKET_TTL  = 3600    # 1 h  — per-market Gamma cache
_SCORE_TTL   = 7200    # 2 h  — SQLite record TTL (set in trader_cache.py)

_BOT_PRICE_THRESHOLD  = 0.15   # token below this in an ended market → anticipated loss
_BOT_WIN_RATE_CEILING = 0.92   # win rate above this + volume > $5k → hard bot flag
_BOT_VOLUME_FLOOR     = 5_000  # minimum volume before bot-ceiling applies
_LOW_TRADE_PENALTY_N  = 20     # trades < this → scale performance score down

_SESS = requests.Session()
_SESS.headers.update({"Accept": "application/json", "User-Agent": "EdgeBot/1.0"})


def _api_retry(func, *args, retries: int = 3, **kwargs):
    """Retry an API call with exponential backoff. Returns result or raises last exception."""
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            return func(*args, **kwargs)
        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(2 ** attempt)  # 1s, 2s
    raise last_exc  # type: ignore[misc]

# ---------------------------------------------------------------------------
# In-memory bot address cache  (survives the process lifetime, 24 h TTL)
# ---------------------------------------------------------------------------

_BOT_FLAG_CACHE: dict[str, float] = {}   # address → unix ts when confirmed bot
_BOT_FLAG_TTL   = 86_400                  # 24 hours


def _is_known_bot(address: str) -> bool:
    ts = _BOT_FLAG_CACHE.get(address.lower())
    return ts is not None and (time.time() - ts) < _BOT_FLAG_TTL


def _mark_bot(address: str) -> None:
    _BOT_FLAG_CACHE[address.lower()] = time.time()


# Leaderboard field names that carry trade count (varies by API version)
_TRADES_KEYS = ("tradesCount", "numTrades", "trades_count", "totalTrades", "numPositions")


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass
class TraderScore:
    wallet_address:    str
    display_name:      str  = ""
    verified:          int  = 0
    # composite
    final_score:       float = 0.0
    anti_bot_score:    float = 0.0
    performance_score: float = 0.0
    reliability_score: float = 0.0
    bot_flag:          int   = 0
    # windows
    win_rate_alltime:  float = 0.0
    win_rate_30d:      float = 0.0
    win_rate_7d:       float = 0.0
    pnl_alltime:       float = 0.0
    pnl_alltime_adj:   float = 0.0
    pnl_30d:           float = 0.0
    pnl_7d:            float = 0.0
    volume_alltime:    float = 0.0
    trades_alltime:    int   = 0
    # streak
    current_streak:    int   = 0
    max_streak_50:     int   = 0
    # hidden-loss
    unsettled_count:       int   = 0
    hidden_loss_exposure:  float = 0.0
    # specialization — top categories by winning PnL (comma-separated)
    top_categories:        str   = ""
    # timing — how early/contrarian entries are (0–1)
    timing_score:          float = 0.0
    # consistency — low variance in winning PnL = steady earner (0–1)
    consistency_score:     float = 0.0
    # fade — proportion of wins that were contrarian bets (0–1)
    fade_score:            float = 0.0
    # sizing discipline — bets bigger on wins than losses (0–1)
    sizing_discipline:     float = 0.0
    # gain/loss ratio — avg win size ÷ avg loss size (>1.0 = wins bigger than losses)
    gl_ratio:              float = 0.0
    # copyable win rate — win rate weighted by position size (liquid market proxy)
    copyable_win_rate:     float = 0.0
    # on-chain wallet signals (from Polygon RPC via wallet_chain.py)
    wallet_nonce:          int   = -1   # total Polygon tx count (-1 = unknown)
    is_fresh_wallet:       int   = 0    # 1 = new/throwaway wallet flag
    # on-chain trade history (from Goldsky subgraph via goldsky_history.py)
    onchain_trade_count:   int   = 0    # total fills found on-chain
    onchain_burst_flag:    int   = 0    # 1 = >20 fills in a 1-hour window
    # meta
    fetched_at:  float = field(default_factory=time.time)
    expires_at:  float = field(default_factory=lambda: time.time() + _SCORE_TTL)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# File-cache helpers
# ---------------------------------------------------------------------------

def _cache_path(key: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_\-]", "_", key)
    return os.path.join(_CACHE_DIR, f"trader_{safe}.json")


def _load_cache(key: str, ttl: int) -> Any | None:
    path = _cache_path(key)
    try:
        if os.path.exists(path) and (time.time() - os.path.getmtime(path)) < ttl:
            with open(path) as f:
                return json.load(f)
    except Exception:
        pass
    return None


def _save_cache(key: str, data: Any) -> None:
    try:
        with open(_cache_path(key), "w") as f:
            json.dump(data, f)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------

class TraderAPIClient:

    # ------------------------------------------------------------------
    # Pre-filter (leaderboard-level, zero extra API calls)
    # ------------------------------------------------------------------

    @staticmethod
    def _prefilter(profile: dict) -> tuple[bool, str]:
        """
        Reject obvious non-human accounts using leaderboard data alone.
        Returns (keep: bool, reason: str).
        Called before any per-wallet API calls — completely free.
        """
        # Resolve trade count from whichever field name this API version uses
        n_trades = 0
        for key in _TRADES_KEYS:
            val = profile.get(key)
            if val:
                try:
                    n_trades = int(val)
                    break
                except (TypeError, ValueError):
                    pass

        # Rule 1 — HFT / scripted market-maker: no human places 10k+ bets
        if n_trades > 10_000:
            return False, f"HFT ({n_trades:,} trades)"

        # Rule 2 — Thin history: not enough data to score meaningfully
        if 0 < n_trades < 20:
            return False, f"Thin history ({n_trades} trades)"

        # Rule 3 — Micro-trade farmer: avg position size < $5
        #   Airdrop farmers place hundreds of $1 bets to game reward systems.
        #   They show inflated win counts but zero real edge.
        # NOTE: Polymarket leaderboard API returns "vol", not "volume"
        volume = float(profile.get("vol", profile.get("volume", 0)) or 0)
        if n_trades > 0 and volume > 0:
            avg_pos = volume / n_trades
            if avg_pos < 5.0:
                return False, f"Micro-trades (avg ${avg_pos:.2f})"

        return True, "ok"

    # ------------------------------------------------------------------
    # Raw data fetchers
    # ------------------------------------------------------------------

    def fetch_leaderboard(self, category: str = "OVERALL", limit: int = 50) -> list[dict]:
        cache_key = f"leaderboard_{category}_{limit}"
        cached = _load_cache(cache_key, _LB_TTL)
        if cached is not None:
            return cached
        try:
            def _do_fetch():
                r = _SESS.get(
                    f"{_DATA_API}/v1/leaderboard",
                    params={"category": category, "limit": limit},
                    timeout=10,
                )
                r.raise_for_status()
                return r.json()

            data = _api_retry(_do_fetch)
            # API returns list directly or wrapped in {"leaderboard": [...]}
            if isinstance(data, list):
                result = data
            else:
                result = data.get("leaderboard", data.get("data", []))
            _save_cache(cache_key, result)
            return result
        except Exception as exc:
            log.warning("Leaderboard fetch failed after retries: %s", exc)
            return []

    def fetch_wallet_trades(self, address: str, limit: int = 500) -> list[dict]:
        """
        Fetches wallet activity from /v1/activity (NOT /v1/trades).
        /v1/activity includes usdcSize (USD amount) vs /v1/trades which only
        has size in shares — critical for accurate volume and position-size scoring.
        Fetches up to 2 pages (1000 records) to capture both BUYs and REDEEMs
        needed for accurate win-rate calculation from cash flows.
        """
        all_records: list[dict] = []
        try:
            for offset in (0, 500):
                def _do_fetch(ofs=offset):
                    r = _SESS.get(
                        f"{_DATA_API}/v1/activity",
                        params={"user": address.lower(), "limit": limit, "offset": ofs},
                        timeout=15,
                    )
                    r.raise_for_status()
                    return r.json()

                data = _api_retry(_do_fetch)
                page = data if isinstance(data, list) else data.get("data", [])
                all_records.extend(page)
                if len(page) < limit:
                    break   # no more pages
            return all_records
        except Exception as exc:
            log.debug("Activity fetch failed for %s after retries: %s", address[:10], exc)
            return all_records  # return whatever we got before the error

    def fetch_wallet_positions(self, address: str) -> list[dict]:
        try:
            def _do_fetch():
                r = _SESS.get(
                    f"{_DATA_API}/v1/positions",
                    params={"user": address.lower()},
                    timeout=10,
                )
                r.raise_for_status()
                return r.json()

            data = _api_retry(_do_fetch)
            return data if isinstance(data, list) else data.get("data", [])
        except Exception as exc:
            log.debug("Positions fetch failed for %s after retries: %s", address[:10], exc)
            return []

    def _fetch_market_meta(self, condition_id: str) -> dict:
        cached = _load_cache(f"market_{condition_id}", _MARKET_TTL)
        if cached is not None:
            return cached
        try:
            def _do_fetch():
                r = _SESS.get(
                    f"{_GAMMA_API}/markets",
                    params={"conditionId": condition_id},
                    timeout=8,
                )
                r.raise_for_status()
                return r.json()

            data = _api_retry(_do_fetch)
            meta = data[0] if isinstance(data, list) and data else (data or {})
            _save_cache(f"market_{condition_id}", meta)
            return meta
        except Exception:
            return {}

    def _fetch_token_price(self, token_id: str) -> float:
        try:
            def _do_fetch():
                r = _SESS.get(f"{_CLOB_API}/price/{token_id}", timeout=6)
                r.raise_for_status()
                return r.json()

            data = _api_retry(_do_fetch)
            return float(data.get("price", 0.5))
        except Exception:
            return 0.5  # unknown → assume mid

    # ------------------------------------------------------------------
    # Scoring sub-components
    # ------------------------------------------------------------------

    def _check_unsettled(self, positions: list[dict]) -> dict[str, Any]:
        """
        For each open position: check if its market end date has passed but
        the market hasn't resolved.  If the current token price is < $0.15,
        treat it as an anticipated loss and calculate the unrealized loss.
        Returns a summary dict for reliability scoring.
        """
        now_ts   = time.time()
        today_dt = datetime.now(tz=timezone.utc)
        count    = 0
        total_loss = 0.0
        flagged  = []

        for pos in positions:
            condition_id = pos.get("conditionId") or pos.get("market", "")
            if not condition_id:
                continue
            meta = self._fetch_market_meta(condition_id)
            if not meta:
                continue

            # Parse end date
            end_str = meta.get("endDate") or meta.get("end_date_iso", "")
            resolved = meta.get("resolved", False)
            if resolved:
                continue   # properly settled — not a hidden loss
            if not end_str:
                continue

            try:
                end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            except ValueError:
                continue

            if end_dt > today_dt:
                continue   # market hasn't ended yet — legitimate open position

            # Market ended but not resolved → potential hidden loss
            token_id = pos.get("asset") or pos.get("tokenId", "")
            price = self._fetch_token_price(token_id) if token_id else 0.5

            if price >= _BOT_PRICE_THRESHOLD:
                continue   # price is still meaningful — not a clear loss

            # Anticipated loss calculation
            avg_price   = float(pos.get("avgPrice", 0.5))
            size        = float(pos.get("size", 0))
            unrealized  = max(0.0, (avg_price - price) * size)
            count      += 1
            total_loss += unrealized
            flagged.append({
                "market": meta.get("question", condition_id[:30]),
                "price":  round(price, 3),
                "loss":   round(unrealized, 2),
            })

        return {
            "count":               count,
            "total_unrealized_loss": round(total_loss, 2),
            "positions":           flagged,
        }

    def _bot_score(self, trades: list[dict], profile: dict) -> tuple[float, bool]:
        """Return (score 0-1, hard_bot_flag)."""
        from collections import Counter
        score = 0.0

        # Profile signals
        if profile.get("verifiedBadge") or profile.get("xUsername"):
            score += 0.35

        # Trade-timing analysis + velocity + burst detection
        timestamps: list[float] = []
        if len(trades) >= 5:
            raw_ts = sorted(
                t.get("timestamp", t.get("createdAt", 0))
                for t in trades
                if t.get("timestamp") or t.get("createdAt")
            )
            if len(raw_ts) >= 2:
                intervals = [
                    abs(raw_ts[i+1] - raw_ts[i])
                    for i in range(len(raw_ts) - 1)
                ]
                # Normalise to seconds if timestamps are in ms
                if intervals[0] > 1e10:
                    intervals = [x / 1000 for x in intervals]
                    timestamps = [x / 1000 for x in raw_ts]
                else:
                    timestamps = list(raw_ts)

                median_interval = statistics.median(intervals)
                if median_interval > 60:
                    score += 0.25

                # ── NEW: Trade velocity (trades per day) ─────────────────
                # We sample 200 activity records in reverse-chronological order.
                # For a very active trader, those 200 records may span only
                # hours — making velocity look astronomically high but meaning
                # nothing about daily behaviour. Only apply the check when the
                # sample spans at least 3 days (provides enough context).
                account_age_days = (timestamps[-1] - timestamps[0]) / 86_400
                if account_age_days >= 3:
                    trades_per_day = len(timestamps) / account_age_days
                    if trades_per_day > 500:
                        # >500 trades/day over 3+ days is clearly scripted
                        return 0.0, True
                    elif trades_per_day > 200:
                        # Strong bot signal
                        score = max(0.0, score - 0.25)

                # ── NEW: Burst detection (20+ trades in any 1-hour window)
                hour_buckets = Counter(int(ts) // 3600 for ts in timestamps)
                if max(hour_buckets.values(), default=0) > 20:
                    score = max(0.0, score - 0.30)
                    log.debug("Burst pattern detected for wallet")

        # Position-size variance (bots use uniform sizes)
        sizes = [
            float(t.get("size", t.get("shares", 0)))
            for t in trades
            if t.get("size") or t.get("shares")
        ]
        if len(sizes) >= 5 and statistics.mean(sizes) > 0:
            cv = statistics.stdev(sizes) / statistics.mean(sizes)
            if cv > 0.25:
                score += 0.20

        # Category diversity
        categories = {
            t.get("category", t.get("market_category", ""))
            for t in trades
            if t.get("category") or t.get("market_category")
        }
        if len(categories) > 1:
            score += 0.20

        # ── NEW: Near-certainty farming (90¢+ or sub-10¢ entries) ────────
        #   Certainty farmers buy near-resolved markets for airdrop credit.
        #   Real traders take positions at genuine uncertainty (10¢–90¢).
        prices = [
            float(t.get("price", t.get("outcomePrice", t.get("avgPrice", 0.5))) or 0.5)
            for t in trades
        ]
        if prices:
            near_certain = sum(1 for p in prices if p > 0.90 or p < 0.10)
            near_pct = near_certain / len(prices)
            if near_pct > 0.40:
                score = max(0.0, score - 0.30)
                log.debug("Farming flag: %.0f%% near-certain trades", near_pct * 100)

        score = min(1.0, score)

        # Hard bot flag: suspiciously perfect record at scale
        hard_bot = False
        settled  = [t for t in trades if t.get("outcome") or t.get("side")]
        if settled:
            wins = sum(
                1 for t in settled
                if str(t.get("outcome", "")).upper() == "WIN"
                or str(t.get("side", "")).upper() == t.get("marketOutcome", "")
            )
            wr = wins / len(settled)
            vol = float(profile.get("vol", profile.get("volume", 0)))
            if wr > _BOT_WIN_RATE_CEILING and vol > _BOT_VOLUME_FLOOR:
                hard_bot = True
                score    = min(score, 0.25)

        return score, hard_bot

    def _perf_score(
        self,
        trades: list[dict],
        positions: list[dict],
        profile: dict | None = None,
    ) -> dict[str, Any]:
        """
        Compute performance stats for alltime / 30d / 7d windows.

        Data source reality (Polymarket public API):
        - /v1/positions  → OPEN positions only. Settled positions are removed
          once a market resolves, so this endpoint CANNOT be used for win rate.
        - /v1/activity   → All TRADE (BUY/SELL) and REDEEM events.
          Win rate is derived from per-market cash flows: total USD spent (BUYs)
          vs. total USD received (SELLs + REDEEMs). Receipt > cost = win.
        - Leaderboard profile → pnl, vol (authoritative, server-computed alltime).
        """
        profile   = profile or {}
        now_ts    = time.time()
        now_ms    = now_ts * 1000
        day30     = now_ms - 30 * 86_400_000
        day7      = now_ms - 7  * 86_400_000

        # ── Authoritative alltime stats from leaderboard ──────────────────────
        lb_pnl = float(profile.get("pnl", 0) or 0)
        lb_vol = float(profile.get("vol", profile.get("volume", 0)) or 0)

        # ── Helpers ───────────────────────────────────────────────────────────
        def _ts(t: dict) -> float:
            raw = float(t.get("timestamp") or t.get("createdAt") or 0)
            return raw * 1000 if raw < 1e12 else raw   # normalise to ms

        def _size(t: dict) -> float:
            return float(t.get("usdcSize", t.get("size", 0)) or 0)

        def _price(t: dict) -> float:
            return float(
                t.get("price", t.get("outcomePrice", t.get("avgPrice", 0.5))) or 0.5
            )

        # ── Win rate from activity cash flows ─────────────────────────────────
        # /v1/positions only shows OPEN positions — settled ones disappear when
        # a market resolves.  Instead, reconstruct settled markets from activity:
        #   BUY events  → money spent per market (conditionId)
        #   SELL/REDEEM → money received per market
        #
        # DENOMINATOR FIX (v2):
        # Old approach: count only markets with BOTH costs AND receipts.
        # Problem: a trader who loses $5,000 across 157 markets and never
        # redeems (because tokens are worthless) has those losses silently
        # excluded — inflating win rate from a true ~51% to a fake 96%.
        #
        # Fix: any market with a BUY older than 30 days and no receipt
        # is assumed resolved and lost.  Recent markets (< 30 days) without
        # receipts are excluded (they may still be live).
        market_costs:     dict[str, float] = {}
        market_receipts:  dict[str, float] = {}
        market_oldest_ts: dict[str, float] = {}   # earliest BUY timestamp per market
        market_close_ts:  dict[str, float] = {}   # latest SELL/REDEEM timestamp per market

        buy_trades: list[dict] = []
        for t in trades:
            cid   = t.get("conditionId", "")
            ttype = t.get("type", "").upper()
            side  = t.get("side", "").upper()
            usd   = _size(t)
            t_ms  = _ts(t)   # normalised milliseconds

            if ttype == "TRADE" and side == "BUY":
                buy_trades.append(t)
                if cid:
                    market_costs[cid] = market_costs.get(cid, 0.0) + usd
                    # Track earliest BUY so we know how old this position is
                    if cid not in market_oldest_ts or t_ms < market_oldest_ts[cid]:
                        market_oldest_ts[cid] = t_ms
            elif ttype == "TRADE" and side == "SELL":
                if cid:
                    market_receipts[cid] = market_receipts.get(cid, 0.0) + usd
                    if t_ms > market_close_ts.get(cid, 0.0):
                        market_close_ts[cid] = t_ms
            elif ttype == "REDEEM":
                if cid:
                    market_receipts[cid] = market_receipts.get(cid, 0.0) + usd
                    if t_ms > market_close_ts.get(cid, 0.0):
                        market_close_ts[cid] = t_ms

        # Markets countable in win rate:
        #   a) Has a receipt (confirmed closed — win or loss)
        #   b) Has a BUY older than 30 days with no receipt (old enough to have resolved = loss)
        cutoff_ms = now_ms - 30 * 86_400_000
        countable_markets = {
            c for c in market_costs
            if (c in market_receipts)                              # confirmed outcome
            or (market_oldest_ts.get(c, now_ms) < cutoff_ms)     # old enough to have resolved
        }

        wins_act   = sum(1 for c in countable_markets
                         if market_receipts.get(c, 0.0) > market_costs[c])
        losses_act = len(countable_markets) - wins_act
        n_settled  = wins_act + losses_act
        win_rate   = wins_act / n_settled if n_settled > 0 else 0.0

        # ── Win streak from ordered market outcomes ────────────────────────────
        # Sort by close timestamp (most recent first).  Markets without a close
        # event fall back to their oldest BUY ts — this puts uncertain markets
        # last so they don't break the current streak prematurely.
        ordered_markets = sorted(
            countable_markets,
            key=lambda c: market_close_ts.get(c, market_oldest_ts.get(c, 0.0)),
            reverse=True,   # most-recent outcome first
        )

        cur_streak  = 0
        for cid in ordered_markets:
            won = market_receipts.get(cid, 0.0) > market_costs[cid]
            if cur_streak == 0:
                cur_streak = 1 if won else -1
            elif (cur_streak > 0 and won) or (cur_streak < 0 and not won):
                cur_streak += (1 if won else -1)
            else:
                break   # streak broken — stop

        # Max streak over the most-recent 50 markets
        max_streak = 0
        _tmp = 0
        for cid in ordered_markets[:50]:
            won = market_receipts.get(cid, 0.0) > market_costs[cid]
            if _tmp == 0:
                _tmp = 1 if won else -1
            elif (_tmp > 0 and won) or (_tmp < 0 and not won):
                _tmp += (1 if won else -1)
            else:
                max_streak = max(max_streak, abs(_tmp))
                _tmp = 1 if won else -1
        max_streak = max(max_streak, abs(_tmp))

        # ── Activity-based PnL ─────────────────────────────────────────────────
        # Summing all receipts minus all costs gives realised cash-flow P&L.
        # This is the most reliable PnL signal for wallets that aren't in the
        # top-N leaderboard (where lb_pnl would be zero from an empty profile).
        activity_pnl = sum(market_receipts.values()) - sum(market_costs.values())

        # Also gather open-position P&L for reliability scoring reference
        pos_pnl = sum(
            float(p.get("cashPnl", p.get("realizedPnl", 0)) or 0)
            for p in positions
            if p.get("redeemable") or float(p.get("curPrice", 1) or 1) == 0
        )

        # ── Activity volume by time window ────────────────────────────────────
        def _window_vol(bucket: list[dict]) -> float:
            return round(sum(_size(t) for t in bucket), 2)

        vol_30d = _window_vol([t for t in buy_trades if _ts(t) >= day30])
        vol_7d  = _window_vol([t for t in buy_trades if _ts(t) >= day7])

        # ── Certainty farming discount on win rate ────────────────────────────
        # A trader buying at 0.99¢ "wins" 99% of the time but earns almost
        # nothing — identical to a certainty farmer.  We discount the effective
        # win rate by the fraction of near-certain trades so that high-volume
        # farmers don't masquerade as skilled traders in the perf score.
        # Discount only kicks in when >= 40% of trades are near-certain
        # (preserves score for genuinely skilled high-win-rate traders).
        prices = [_price(t) for t in trades if t.get("type", "").upper() == "TRADE"]
        if prices:
            near_certain_cnt = sum(1 for p in prices if p > 0.85 or p < 0.10)
            near_certain_pct = near_certain_cnt / len(prices)
            if near_certain_pct > 0.40:
                # Discount win rate proportionally: e.g. 60% near-certain → win_rate *= 0.60
                discount = 1.0 - (near_certain_pct - 0.40)
                win_rate = round(win_rate * max(discount, 0.30), 4)
                log.debug(
                    "Certainty farming discount %.0f%% near-certain → win_rate adj to %.1f%%",
                    near_certain_pct * 100, win_rate * 100,
                )

        # ── Timing / style signals ────────────────────────────────────────────
        genuine_prices = [p for p in prices if 0.10 <= p <= 0.80]
        if genuine_prices:
            avg_entry    = sum(genuine_prices) / len(genuine_prices)
            timing_score = round(1.0 - (avg_entry - 0.10) / 0.70, 4)
        else:
            timing_score = 0.0

        contrarian = sum(1 for p in genuine_prices if p < 0.45)
        fade_score = round(contrarian / max(len(genuine_prices), 1), 4)

        avg_entry = sum(genuine_prices) / len(genuine_prices) if genuine_prices else 0.5

        # ── ROI and performance sub-score ─────────────────────────────────────
        # Priority: leaderboard PnL (authoritative) → activity cash-flow PnL
        # (accurate for non-top-N wallets) → open-position PnL (last resort).
        effective_pnl = (lb_pnl        if lb_pnl != 0
                         else activity_pnl if activity_pnl != 0.0
                         else pos_pnl)
        effective_vol = lb_vol if lb_vol != 0 else _window_vol(buy_trades)
        roi           = effective_pnl / max(effective_vol, 1)

        n_activity  = len(trades)
        avg_pos     = effective_vol / max(n_activity, 1)
        size_factor = min(avg_pos / 20.0, 1.0)   # full credit at $20+ avg

        # ── Gain/Loss ratio ───────────────────────────────────────────────────
        # Avg profit per winning market ÷ avg loss per losing market.
        # A wallet winning $200 avg and losing $50 avg has GL=4.0 — real edge.
        # A wallet winning $50 avg and losing $200 avg has GL=0.25 — gambling.
        win_amounts  = [market_receipts.get(c, 0.0) - market_costs[c]
                        for c in countable_markets
                        if market_receipts.get(c, 0.0) > market_costs[c]]
        loss_amounts = [market_costs[c] - market_receipts.get(c, 0.0)
                        for c in countable_markets
                        if market_receipts.get(c, 0.0) <= market_costs[c] and c in market_receipts]
        avg_win_size  = sum(win_amounts)  / len(win_amounts)  if win_amounts  else 0.0
        avg_loss_size = sum(loss_amounts) / len(loss_amounts) if loss_amounts else 1.0
        gl_ratio      = avg_win_size / max(avg_loss_size, 0.01)
        # Normalise: GL=1.0 → 0.33 score | GL=2.0 → 0.67 | GL=3.0+ → 1.0
        gl_score      = round(min(gl_ratio / 3.0, 1.0), 4)

        # ── Consistency score ────────────────────────────────────────────────
        # Low coefficient of variation in per-market PnL = steady earner (good).
        # High CV = gambler who got lucky on a few big bets (bad).
        all_pnl_values = (
            [market_receipts.get(c, 0.0) - market_costs[c] for c in countable_markets]
        )
        if len(all_pnl_values) >= 5:
            pnl_mean = sum(all_pnl_values) / len(all_pnl_values)
            pnl_var  = sum((x - pnl_mean) ** 2 for x in all_pnl_values) / (len(all_pnl_values) - 1)
            pnl_std  = math.sqrt(pnl_var) if pnl_var > 0 else 0.0
            cv_pnl   = pnl_std / max(abs(pnl_mean), 0.01)
            consistency_score = round(max(0.0, 1.0 - min(cv_pnl / 2.0, 1.0)), 4)
        else:
            consistency_score = 0.0

        # ── Sizing discipline ────────────────────────────────────────────────
        # Do they bet bigger on wins than losses?  avg_win_stake / avg_loss_stake
        # Ratio > 1.0 = Kelly-like behaviour — sizing up when confident.
        # Ratio < 1.0 = sizing up on losers (martingale / tilt).
        win_stakes  = [market_costs[c] for c in countable_markets
                       if market_receipts.get(c, 0.0) > market_costs[c]]
        loss_stakes = [market_costs[c] for c in countable_markets
                       if market_receipts.get(c, 0.0) <= market_costs[c] and c in market_receipts]
        avg_win_stake  = sum(win_stakes)  / len(win_stakes)  if win_stakes  else 0.0
        avg_loss_stake = sum(loss_stakes) / len(loss_stakes) if loss_stakes else 1.0
        sizing_ratio    = avg_win_stake / max(avg_loss_stake, 0.01)
        sizing_discipline = round(min(sizing_ratio / 2.0, 1.0), 4)

        # ── Liquid / copyable market weighting ───────────────────────────────
        # Wins from large positions (>$50 staked) count as fully copyable.
        # Wins from micro positions (<$5) count near-zero — too thin to follow.
        # This penalises wallets that farm tiny illiquid markets where the user
        # can't enter at a reasonable price even if they wanted to copy.
        _COPY_FLOOR = 5.0    # below this stake = essentially uncopyable
        _COPY_FULL  = 50.0   # at/above this stake = fully copyable
        weighted_wins = sum(
            min(max(market_costs[c] - _COPY_FLOOR, 0.0) / (_COPY_FULL - _COPY_FLOOR), 1.0)
            for c in countable_markets
            if market_receipts.get(c, 0.0) > market_costs[c]
        )
        copyable_win_rate = round(weighted_wins / max(n_settled, 1), 4)

        # ── Low-trade penalty (ltp) — fixed floor so lb-verified traders ─────
        # aren't crushed just because our 500-record activity sample is thin.
        # Floor: lb_vol / $100k → a $100k+ leaderboard trader gets ltp ≥ 0.5
        ltp_from_activity = min(n_settled / _LOW_TRADE_PENALTY_N, 1.0)
        ltp_from_volume   = min(effective_vol / 100_000, 0.7)
        ltp = max(ltp_from_activity, ltp_from_volume)

        perf_raw = (win_rate          * 0.40   # consistency — must win regularly
                    + gl_score          * 0.20   # win bigger than you lose (new)
                    + copyable_win_rate * 0.15   # wins in liquid copyable markets (new)
                    + min(max(roi + 0.5, 0), 1) * 0.15   # overall ROI
                    + size_factor       * 0.10)  # position discipline

        # Hard win-rate gate: sub-55% with enough sample = no consistent edge
        # Big PnL from a few lucky wins doesn't make someone smart money
        _MIN_WIN_RATE   = 0.55
        _MIN_SAMPLE     = 15          # need at least 15 settled trades to apply gate
        if win_rate < _MIN_WIN_RATE and n_settled >= _MIN_SAMPLE:
            perf_raw *= 0.50          # 50% penalty — dragged well below passing grade

        perf_score = min(perf_raw * max(ltp, 0.15), 1.0)   # hard floor of 0.15 on ltp

        # ── Category specialization ───────────────────────────────────────────
        def _cat_from_slug(slug: str) -> str:
            if not slug:
                return ""
            s = slug.lower()
            for kw, label in (
                ("nba-", "NBA"), ("nfl-", "NFL"), ("nhl-", "NHL"),
                ("mlb-", "MLB"), ("soccer", "Soccer"), ("f1-", "F1"),
                ("btc", "Crypto"), ("eth", "Crypto"), ("sol-", "Crypto"),
                ("election", "Politics"), ("president", "Politics"),
                ("fed-", "Economics"), ("cpi-", "Economics"),
                ("cs2-", "Esports"), ("esport", "Esports"),
            ):
                if s.startswith(kw) or kw in s:
                    return label
            return ""

        cat_vol: dict[str, float] = {}
        for t in buy_trades:
            cat = _cat_from_slug(t.get("slug", t.get("eventSlug", "")))
            if cat:
                cat_vol[cat] = cat_vol.get(cat, 0.0) + _size(t)
        top_cats = [c for c, _ in sorted(cat_vol.items(), key=lambda x: x[1], reverse=True)[:2]]

        # ── Strategy classification ───────────────────────────────────────────
        # Derives a human-readable trading style from the computed signals.
        # Used in AI copy-trade suggestions ("NBA Specialist on a 7-game streak").
        top_cat_name = top_cats[0] if top_cats else ""
        top_cat_pct = (
            cat_vol.get(top_cat_name, 0.0) / max(effective_vol, 1.0)
            if top_cat_name else 0.0
        )
        if top_cat_pct > 0.70:
            strategy_tag = f"{top_cat_name} Specialist"
        elif fade_score > 0.55:
            strategy_tag = "Contrarian"
        elif timing_score > 0.65 and avg_entry < 0.40:
            strategy_tag = "Value Hunter"
        elif timing_score > 0.60 and fade_score < 0.35:
            strategy_tag = "Momentum"
        else:
            strategy_tag = "Generalist"

        all_stats = {
            "wins": wins_act, "losses": losses_act,
            "win_rate": round(win_rate, 4),
            "pnl": round(effective_pnl, 2),
            "pnl_activity": round(activity_pnl, 2),   # cash-flow PnL (independent of LB)
            "volume": round(effective_vol, 2),
            "n_countable_markets": n_settled,          # denominator used for win rate
        }
        # ── 30d / 7d windowed win rates ─────────────────────────────────────
        # Re-filter countable markets by close timestamp for time-windowed stats.
        # Enables momentum detection: alltime 70% but 30d 45% = declining wallet.
        def _windowed_wr(cutoff_ms: float) -> dict:
            w_markets = {
                c for c in countable_markets
                if market_close_ts.get(c, market_oldest_ts.get(c, 0.0)) >= cutoff_ms
            }
            if not w_markets:
                return {"wins": 0, "losses": 0, "win_rate": 0.0}
            w_wins = sum(1 for c in w_markets
                         if market_receipts.get(c, 0.0) > market_costs[c])
            w_total = len(w_markets)
            return {
                "wins": w_wins,
                "losses": w_total - w_wins,
                "win_rate": round(w_wins / w_total, 4),
            }

        wr_30d = _windowed_wr(day30)
        wr_7d  = _windowed_wr(day7)
        d30_stats = {**wr_30d, "pnl": 0.0, "volume": vol_30d}
        d7_stats  = {**wr_7d,  "pnl": 0.0, "volume": vol_7d}

        return {
            "alltime":           all_stats,
            "30d":               d30_stats,
            "7d":                d7_stats,
            "pos_pnl":           round(pos_pnl, 2),
            "perf_score":        round(min(perf_score, 1.0), 4),
            "cur_streak":        cur_streak,
            "max_streak":        max_streak,
            "n_trades":          n_activity,
            "n_settled_markets": n_settled,
            "quality_win_rate":  round(win_rate, 4),
            "avg_pos_size":      round(avg_pos, 2),
            "top_categories":    top_cats,
            "timing_score":      timing_score,
            "consistency_score": consistency_score,
            "fade_score":        fade_score,
            "sizing_discipline": sizing_discipline,
            "gl_ratio":          round(gl_ratio, 4),
            "copyable_win_rate": copyable_win_rate,
            "strategy_tag":      strategy_tag,
        }

    def _reliability_score(
        self, unsettled: dict[str, Any], realized_pnl: float
    ) -> tuple[float, float]:
        """
        Returns (reliability_score 0-1, adjusted_pnl).
        Deducts hidden-loss exposure from reliability proportionally.
        """
        loss = unsettled["total_unrealized_loss"]
        adjusted_pnl = realized_pnl - loss
        hidden_ratio = loss / max(abs(realized_pnl), 1.0)
        reliability  = max(0.0, 1.0 - hidden_ratio * 2.0)
        return round(reliability, 4), round(adjusted_pnl, 2)

    # ------------------------------------------------------------------
    # Main scoring entry point
    # ------------------------------------------------------------------

    def score_trader(self, address: str, profile: dict | None = None) -> TraderScore:
        """
        Full wallet vet.  Fetches trades + positions, computes all sub-scores,
        persists to TraderCache, and returns a TraderScore.
        """
        from edge_agent.memory.trader_cache import get_trader_cache

        address = address.lower().strip()
        profile = profile or {}

        # ── Short-circuit: already confirmed bot in this process session ──
        if _is_known_bot(address):
            log.debug("score_trader: cached bot skip for %s…", address[:8])
            return TraderScore(
                wallet_address = address,
                display_name   = profile.get("userName", profile.get("name", "")),
                bot_flag       = 1,
                final_score    = 0.0,
                fetched_at     = time.time(),
                expires_at     = time.time() + _BOT_FLAG_TTL,
            )

        trades    = self.fetch_wallet_trades(address)
        positions = self.fetch_wallet_positions(address)

        unsettled  = self._check_unsettled(positions)
        perf       = self._perf_score(trades, positions, profile)
        bot_sc, hard_bot = self._bot_score(trades, profile)
        realized_pnl     = perf["pos_pnl"] or perf["alltime"]["pnl"]
        rel_sc, adj_pnl  = self._reliability_score(unsettled, realized_pnl)

        # ── On-chain wallet signals (Polygon RPC — no key required) ──────
        from edge_agent.vetting.wallet_chain import wallet_chain_signals
        chain = wallet_chain_signals(address)

        # ── On-chain trade history (Goldsky subgraph — no key required) ──
        from edge_agent.vetting.goldsky_history import goldsky_summary
        goldsky = goldsky_summary(address)

        # Goldsky burst flag → tighten anti-bot score
        if goldsky["burst_flag"]:
            bot_sc = max(0.0, bot_sc - 0.20)
            log.debug("Goldsky burst flag applied for %s…", address[:10])

        # ── Base composite score ──────────────────────────────────────────
        final = (bot_sc * 0.25 + perf["perf_score"] * 0.50 + rel_sc * 0.25)

        # Fresh-wallet penalty: new throwaway wallets get trust deducted
        final = max(0.0, final - chain["fresh_penalty"])

        ts = TraderScore(
            wallet_address    = address,
            display_name      = profile.get("userName", profile.get("name", "")),
            verified          = int(bool(profile.get("verifiedBadge"))),
            final_score       = round(final, 4),
            anti_bot_score    = round(bot_sc, 4),
            performance_score = round(perf["perf_score"], 4),
            reliability_score = round(rel_sc, 4),
            bot_flag          = int(hard_bot),
            win_rate_alltime  = round(perf["alltime"]["win_rate"], 4),
            win_rate_30d      = round(perf["30d"]["win_rate"], 4),
            win_rate_7d       = round(perf["7d"]["win_rate"], 4),
            pnl_alltime       = perf["alltime"]["pnl"],
            pnl_alltime_adj   = adj_pnl,
            pnl_30d           = perf["30d"]["pnl"],
            pnl_7d            = perf["7d"]["pnl"],
            volume_alltime    = perf["alltime"]["volume"],
            trades_alltime    = perf["n_trades"],
            current_streak    = perf["cur_streak"],
            max_streak_50     = perf["max_streak"],
            unsettled_count        = unsettled["count"],
            hidden_loss_exposure   = unsettled["total_unrealized_loss"],
            top_categories         = ", ".join(perf["top_categories"]),
            timing_score           = perf["timing_score"],
            consistency_score      = perf["consistency_score"],
            fade_score             = perf["fade_score"],
            sizing_discipline      = perf["sizing_discipline"],
            gl_ratio               = perf["gl_ratio"],
            copyable_win_rate      = perf["copyable_win_rate"],
            # on-chain wallet signals
            wallet_nonce           = chain["nonce"],
            is_fresh_wallet        = int(chain["is_fresh"]),
            # on-chain trade history
            onchain_trade_count    = goldsky["onchain_count"],
            onchain_burst_flag     = int(goldsky["burst_flag"]),
        )

        # Cache confirmed bots so we skip them for 24 h
        if ts.bot_flag:
            _mark_bot(address)

        try:
            get_trader_cache().upsert(ts.to_dict())
        except Exception as exc:
            log.debug("TraderCache upsert failed: %s", exc)

        return ts

    # ------------------------------------------------------------------
    # Leaderboard scan
    # ------------------------------------------------------------------

    def get_hot_traders(
        self, limit: int = 10, category: str = "OVERALL"
    ) -> list[TraderScore]:
        """
        Fetch leaderboard, pre-filter obvious bots/farmers for free, then
        score only the surviving candidates concurrently.  Returns top N
        human traders sorted by final_score.
        """
        # Pull a large sample to survive pre-filter attrition
        lb = self.fetch_leaderboard(category=category, limit=100)
        if not lb:
            # Fall back to cached results
            from edge_agent.memory.trader_cache import get_trader_cache
            cached = get_trader_cache().get_top(limit)
            return [TraderScore(**{k: v for k, v in r.items()
                                   if k in TraderScore.__dataclass_fields__})
                    for r in cached]

        # ── PRE-FILTER: zero extra API calls ─────────────────────────────
        candidates: list[dict] = []
        for entry in lb:
            addr = entry.get("proxyWallet", "")
            if not addr:
                continue
            if _is_known_bot(addr):
                log.debug("Skipping known bot: %s…", addr[:8])
                continue
            keep, reason = self._prefilter(entry)
            if not keep:
                log.debug("Pre-filtered %s…: %s", addr[:8], reason)
                continue
            candidates.append(entry)

        log.info(
            "Leaderboard: %d fetched → %d candidates after pre-filter",
            len(lb), len(candidates),
        )

        # ── SCORE only surviving candidates, hard-capped at 25 ──────────
        scores: list[TraderScore] = []
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {
                pool.submit(self.score_trader, entry.get("proxyWallet", ""), entry): entry
                for entry in candidates[:25]   # hard cap on API calls
            }
            for fut in as_completed(futures):
                try:
                    ts = fut.result(timeout=20)
                    if ts.bot_flag:
                        _mark_bot(ts.wallet_address)   # cache for 24 h
                    else:
                        scores.append(ts)
                except Exception as exc:
                    log.debug("Scoring failed for a wallet: %s", exc)

        scores.sort(key=lambda s: s.final_score, reverse=True)
        return scores[:limit]

    # ------------------------------------------------------------------
    # Tier-0/1 discovery sweep  (no per-wallet API calls)
    # ------------------------------------------------------------------

    # Leaderboard categories to sweep — maps our internal name → API param value
    # Updated 2026-03-15: Polymarket retired PROFIT/VOLUME/MONTHLY/WEEKLY/DAILY.
    # Current valid categories: OVERALL, SPORTS, CRYPTO, POLITICS (case-insensitive).
    _SWEEP_CATEGORIES: dict[str, str] = {
        "overall":  "OVERALL",
        "sports":   "SPORTS",
        "crypto":   "CRYPTO",
        "politics": "POLITICS",
    }

    def discovery_sweep(
        self,
        per_category: int = 100,
        fast_score_threshold: float = 20.0,
    ) -> dict[str, Any]:
        """
        Tier-0/1 discovery sweep: pull 5 leaderboard categories, fast-score
        every wallet using only leaderboard data (zero per-wallet API calls),
        and write everything into the discovery_pool table.

        Fast score (0–100):
          - ROI component  50 pts  → pnl / vol, capped at 1.0
          - PnL component  30 pts  → log scale, capped at $100k → full credit
          - Volume floor   20 pts  → vol / $10k, capped at 1.0
          Penalty: –15 pts if avg_pos_size < $5 (micro-trader flag)
          Penalty: –10 pts if n_trades > 5000 (likely HFT)

        Wallets that survive pre-filter AND fast_score >= threshold are
        flagged as vet_priority=1 so pool_get_vet_queue() surfaces them first.

        Returns summary dict: {category: count_added, ...}
        """
        from edge_agent.memory.trader_cache import get_trader_cache
        cache = get_trader_cache()
        summary: dict[str, Any] = {}
        seen: set[str] = set()   # deduplicate across categories

        for cat_name, cat_param in self._SWEEP_CATEGORIES.items():
            lb = self.fetch_leaderboard(category=cat_param, limit=per_category)
            added = 0

            for rank_idx, entry in enumerate(lb, start=1):
                addr = entry.get("proxyWallet", "").lower()
                if not addr or addr in seen:
                    continue
                seen.add(addr)

                # ── Tier-0: extract raw leaderboard figures ──────────────
                pnl = float(entry.get("pnl", 0) or 0)
                vol = float(entry.get("vol", entry.get("volume", 0)) or 0)
                n_trades = 0
                for key in _TRADES_KEYS:
                    val = entry.get(key)
                    if val:
                        try:
                            n_trades = int(val)
                            break
                        except (TypeError, ValueError):
                            pass

                # ── Tier-1: fast score (all in-process, 0 API calls) ────
                roi = pnl / max(vol, 1.0)

                roi_pts = min(max(roi, 0.0), 1.0) * 50.0

                pnl_pts = 0.0
                if pnl > 0:
                    import math as _math
                    # log(pnl+1)/log(100001) → 0 at $0, 1.0 at $100k
                    pnl_pts = min(_math.log(pnl + 1) / _math.log(100_001), 1.0) * 30.0

                vol_pts = min(vol / 10_000, 1.0) * 20.0

                fast = roi_pts + pnl_pts + vol_pts

                # Penalties
                avg_pos = vol / max(n_trades, 1)
                if avg_pos < 5.0 and n_trades > 0:
                    fast -= 15.0   # micro-trade farmer
                if n_trades > 5_000:
                    fast -= 10.0   # likely HFT / script

                fast = round(max(0.0, min(100.0, fast)), 2)

                # Bot pre-flag from leaderboard data alone
                bot_pre = 0
                keep, reason = self._prefilter(entry)
                if not keep:
                    bot_pre = 1

                pool_row = {
                    "wallet_address": addr,
                    "display_name":   entry.get("userName", entry.get("name", "")),
                    "category":       cat_name,
                    "lb_rank":        rank_idx,
                    "pnl_alltime":    round(pnl, 2),
                    "volume_alltime": round(vol, 2),
                    "roi":            round(roi, 4),
                    "fast_score":     fast,
                    "bot_preflag":    bot_pre,
                    "vet_priority":   1 if (fast >= fast_score_threshold and not bot_pre) else 0,
                    "full_vet_done":  0,
                }

                try:
                    cache.pool_upsert(pool_row)
                    added += 1
                except Exception as exc:
                    log.debug("pool_upsert failed for %s: %s", addr[:10], exc)

            summary[cat_name] = added
            log.info(
                "[discovery_sweep] %s: %d/%d added to pool",
                cat_name, added, len(lb),
            )

        summary["total_unique"] = len(seen)
        return summary
