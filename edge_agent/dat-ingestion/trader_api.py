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
        volume = float(profile.get("volume", 0) or 0)
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
            r = _SESS.get(
                f"{_DATA_API}/v1/leaderboard",
                params={"category": category, "limit": limit},
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            # API returns list directly or wrapped in {"leaderboard": [...]}
            if isinstance(data, list):
                result = data
            else:
                result = data.get("leaderboard", data.get("data", []))
            _save_cache(cache_key, result)
            return result
        except Exception as exc:
            log.warning("Leaderboard fetch failed: %s", exc)
            return []

    def fetch_wallet_trades(self, address: str, limit: int = 200) -> list[dict]:
        try:
            r = _SESS.get(
                f"{_DATA_API}/v1/trades",
                params={"user": address.lower(), "limit": limit},
                timeout=12,
            )
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else data.get("data", [])
        except Exception as exc:
            log.debug("Trades fetch failed for %s: %s", address[:10], exc)
            return []

    def fetch_wallet_positions(self, address: str) -> list[dict]:
        try:
            r = _SESS.get(
                f"{_DATA_API}/v1/positions",
                params={"user": address.lower()},
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else data.get("data", [])
        except Exception as exc:
            log.debug("Positions fetch failed for %s: %s", address[:10], exc)
            return []

    def _fetch_market_meta(self, condition_id: str) -> dict:
        cached = _load_cache(f"market_{condition_id}", _MARKET_TTL)
        if cached is not None:
            return cached
        try:
            r = _SESS.get(
                f"{_GAMMA_API}/markets",
                params={"conditionId": condition_id},
                timeout=8,
            )
            r.raise_for_status()
            data = r.json()
            meta = data[0] if isinstance(data, list) and data else (data or {})
            _save_cache(f"market_{condition_id}", meta)
            return meta
        except Exception:
            return {}

    def _fetch_token_price(self, token_id: str) -> float:
        try:
            r = _SESS.get(f"{_CLOB_API}/price/{token_id}", timeout=6)
            r.raise_for_status()
            return float(r.json().get("price", 0.5))
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
                account_age_days = (timestamps[-1] - timestamps[0]) / 86_400
                if account_age_days > 0:
                    trades_per_day = len(timestamps) / account_age_days
                    if trades_per_day > 100:
                        # >100 trades/day is inhuman — hard bot
                        return 0.0, True
                    elif trades_per_day > 50:
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
            vol = float(profile.get("volume", 0))
            if wr > _BOT_WIN_RATE_CEILING and vol > _BOT_VOLUME_FLOOR:
                hard_bot = True
                score    = min(score, 0.25)

        return score, hard_bot

    def _perf_score(self, trades: list[dict], positions: list[dict]) -> dict[str, Any]:
        """Compute performance stats for alltime / 30d / 7d windows."""
        now_ms = time.time() * 1000
        day30  = now_ms - 30 * 86_400_000
        day7   = now_ms - 7  * 86_400_000

        def _ts(t: dict) -> float:
            raw = t.get("timestamp") or t.get("createdAt") or 0
            raw = float(raw)
            # normalise to ms
            return raw * 1000 if raw < 1e12 else raw

        def _is_win(t: dict) -> bool | None:
            out = str(t.get("outcome", "")).upper()
            if out == "WIN":
                return True
            if out == "LOSS":
                return False
            return None

        def _pnl(t: dict) -> float:
            return float(t.get("cashPnl", t.get("pnl", 0)) or 0)

        def _size(t: dict) -> float:
            return float(t.get("usdcSize", t.get("size", 0)) or 0)

        # Streak on last 50 settled trades
        settled_50 = [t for t in trades if _is_win(t) is not None][:50]
        cur_streak = max_streak = temp = 0
        for t in settled_50:
            if _is_win(t):
                temp   += 1
                cur_streak = temp
                max_streak = max(max_streak, temp)
            else:
                temp = 0
        # current streak = consecutive wins from the MOST recent trade
        cur_streak = 0
        for t in settled_50:
            if _is_win(t):
                cur_streak += 1
            else:
                break

        def _price(t: dict) -> float:
            """Entry price of the trade (0.0–1.0)."""
            return float(
                t.get("price", t.get("outcomePrice", t.get("avgPrice", 0.5))) or 0.5
            )

        def _window_stats(bucket: list[dict]) -> dict:
            wins   = sum(1 for t in bucket if _is_win(t) is True)
            losses = sum(1 for t in bucket if _is_win(t) is False)
            total  = wins + losses
            pnl    = sum(_pnl(t) for t in bucket)
            vol    = sum(_size(t) for t in bucket)
            wr     = wins / total if total else 0.0
            return {"wins": wins, "losses": losses, "win_rate": wr,
                    "pnl": round(pnl, 2), "volume": round(vol, 2)}

        all_stats = _window_stats(trades)
        d30_stats = _window_stats([t for t in trades if _ts(t) >= day30])
        d7_stats  = _window_stats([t for t in trades if _ts(t) >= day7])

        # Realized PnL from positions
        pos_pnl = sum(float(p.get("cashPnl", p.get("realizedPnl", 0)) or 0)
                      for p in positions)

        # ── Edge-weighted genuine win rate ───────────────────────────────
        # Only count wins where entry price reflects REAL uncertainty.
        # Band 10¢–80¢: genuine bet. Outside that = certainty trade / farming.
        #   - Buy YES at 98¢ and win → meaningless, excluded
        #   - Buy YES at 50¢ and win → real edge, counted
        #   - Buy NO at 4¢ and win   → farming the other side, excluded
        settled_all = [t for t in trades if _is_win(t) is not None]
        genuine_wins = sum(
            1 for t in settled_all
            if _is_win(t) is True and 0.10 <= _price(t) <= 0.80
        )
        n_settled = all_stats["wins"] + all_stats["losses"]
        quality_win_rate = genuine_wins / max(n_settled, 1)

        # ── Avg position size factor ─────────────────────────────────────
        # Farmers typically bet $1–5 across thousands of markets.
        # Real traders take meaningful positions ($20+).
        avg_pos = all_stats["volume"] / max(n_settled, 1)
        size_factor = min(avg_pos / 20.0, 1.0)   # full credit at $20+ avg

        # Performance sub-score (0–1)
        roi     = all_stats["pnl"] / max(all_stats["volume"], 1)
        streak_factor = min(cur_streak / 10.0, 1.0)

        # Low-trade penalty (< 20 settled = less reliable)
        ltp = min(n_settled / _LOW_TRADE_PENALTY_N, 1.0)

        # quality_win_rate replaces raw win_rate; size_factor replaces flat +0.10
        perf_raw = (quality_win_rate * 0.40
                    + min(max(roi + 0.5, 0), 1) * 0.35
                    + streak_factor * 0.15
                    + size_factor * 0.10)
        perf_score = perf_raw * ltp

        # ── Market timing score ───────────────────────────────────────────
        # Measures how early / contrarian entries are.
        # Entry price on a winning trade that resolves YES:
        #   low entry price (e.g. 0.20) = bought cheap before crowd → high timing
        #   high entry price (e.g. 0.85) = bought near-certainty → low timing
        # Only genuine-uncertainty trades (10¢–80¢) count.
        timing_prices = [
            _price(t) for t in settled_all
            if _is_win(t) is True and 0.10 <= _price(t) <= 0.80
        ]
        if timing_prices:
            avg_entry = sum(timing_prices) / len(timing_prices)
            # Lower avg entry = earlier/better timing. Score peaks at 0.10, zero at 0.80.
            timing_score = round(1.0 - (avg_entry - 0.10) / 0.70, 4)
        else:
            timing_score = 0.0

        # ── Profit consistency score ──────────────────────────────────────
        # Coefficient of variation on per-trade PnL for winning trades.
        # Low CV = steady earner. High CV = one lucky hit skewing the record.
        win_pnls = [_pnl(t) for t in settled_all if _is_win(t) is True and _pnl(t) > 0]
        if len(win_pnls) >= 5:
            mean_pnl = sum(win_pnls) / len(win_pnls)
            variance = sum((x - mean_pnl) ** 2 for x in win_pnls) / len(win_pnls)
            cv = (variance ** 0.5) / mean_pnl if mean_pnl > 0 else 1.0
            # CV < 0.5 = very consistent, CV > 2.0 = one-hit wonder
            consistency_score = round(max(0.0, 1.0 - cv / 2.0), 4)
        else:
            consistency_score = 0.0   # not enough data

        # ── Fade (contrarian) detection ───────────────────────────────────
        # Entry price IS the market consensus probability at bet time.
        # Winning at a low entry price = you bet against the crowd and were right.
        # Fade score = proportion of genuine wins where entry was < 0.45
        # (crowd gave the outcome less than 45% chance).
        genuine_win_trades = [
            t for t in settled_all
            if _is_win(t) is True and 0.10 <= _price(t) <= 0.80
        ]
        contrarian_wins = sum(1 for t in genuine_win_trades if _price(t) < 0.45)
        fade_score = round(contrarian_wins / max(len(genuine_win_trades), 1), 4)

        # ── Position sizing discipline ────────────────────────────────────
        # Disciplined traders bet larger when they have edge — avg size on wins
        # should exceed avg size on losses. Ratio > 1.5 = strong discipline.
        win_sizes  = [_size(t) for t in settled_all if _is_win(t) is True  and _size(t) > 0]
        loss_sizes = [_size(t) for t in settled_all if _is_win(t) is False and _size(t) > 0]
        if win_sizes and loss_sizes:
            avg_win_size  = sum(win_sizes)  / len(win_sizes)
            avg_loss_size = sum(loss_sizes) / len(loss_sizes)
            size_ratio    = avg_win_size / max(avg_loss_size, 0.01)
            # Score 1.0 at ratio >= 1.5, 0.5 at ratio == 1.0, 0.0 at ratio <= 0.6
            sizing_discipline = round(min(1.0, max(0.0, (size_ratio - 0.6) / 0.9)), 4)
        else:
            sizing_discipline = 0.0

        # ── Category specialization ──────────────────────────────────────
        # Tally winning PnL per category; top 2 = trader's specialty.
        cat_pnl: dict[str, float] = {}
        for t in trades:
            cat = (t.get("category") or t.get("market_category") or "").strip()
            if not cat or _is_win(t) is not True:
                continue
            cat_pnl[cat] = cat_pnl.get(cat, 0.0) + _pnl(t)
        top_cats = [c for c, _ in sorted(cat_pnl.items(), key=lambda x: x[1], reverse=True)[:2]]

        return {
            "alltime":          all_stats,
            "30d":              d30_stats,
            "7d":               d7_stats,
            "pos_pnl":          round(pos_pnl, 2),
            "perf_score":       round(min(perf_score, 1.0), 4),
            "cur_streak":       cur_streak,
            "max_streak":       max_streak,
            "n_trades":         len(trades),
            "quality_win_rate":   round(quality_win_rate, 4),
            "avg_pos_size":       round(avg_pos, 2),
            "top_categories":     top_cats,
            "timing_score":       timing_score,
            "consistency_score":  consistency_score,
            "fade_score":         fade_score,
            "sizing_discipline":  sizing_discipline,
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
        from edge_agent.memory.trader_cache import TraderCache

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
        perf       = self._perf_score(trades, positions)
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
            TraderCache().upsert(ts.to_dict())
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
            from edge_agent.memory.trader_cache import TraderCache
            cached = TraderCache().get_top(limit)
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
