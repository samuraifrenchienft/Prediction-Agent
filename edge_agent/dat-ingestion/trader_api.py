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
        score = 0.0

        # Profile signals
        if profile.get("verifiedBadge") or profile.get("xUsername"):
            score += 0.35

        # Trade-timing analysis
        if len(trades) >= 5:
            timestamps = sorted(
                t.get("timestamp", t.get("createdAt", 0))
                for t in trades
                if t.get("timestamp") or t.get("createdAt")
            )
            if len(timestamps) >= 2:
                intervals = [
                    abs(timestamps[i+1] - timestamps[i])
                    for i in range(len(timestamps) - 1)
                ]
                # Convert to seconds if timestamps are in ms
                sample = intervals[0]
                if sample > 1e10:
                    intervals = [x / 1000 for x in intervals]
                median_interval = statistics.median(intervals)
                if median_interval > 60:
                    score += 0.25

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

        # Performance sub-score (0–1)
        wr      = all_stats["win_rate"]
        roi     = all_stats["pnl"] / max(all_stats["volume"], 1)
        streak_factor = min(cur_streak / 10.0, 1.0)

        # Low-trade penalty (< 20 settled = less reliable)
        n_settled = all_stats["wins"] + all_stats["losses"]
        ltp = min(n_settled / _LOW_TRADE_PENALTY_N, 1.0)

        perf_raw = (wr * 0.40 + min(max(roi + 0.5, 0), 1) * 0.35
                    + streak_factor * 0.15 + 0.10)
        perf_score = perf_raw * ltp

        return {
            "alltime":     all_stats,
            "30d":         d30_stats,
            "7d":          d7_stats,
            "pos_pnl":     round(pos_pnl, 2),
            "perf_score":  round(min(perf_score, 1.0), 4),
            "cur_streak":  cur_streak,
            "max_streak":  max_streak,
            "n_trades":    len(trades),
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

        trades    = self.fetch_wallet_trades(address)
        positions = self.fetch_wallet_positions(address)

        unsettled  = self._check_unsettled(positions)
        perf       = self._perf_score(trades, positions)
        bot_sc, hard_bot = self._bot_score(trades, profile)
        realized_pnl     = perf["pos_pnl"] or perf["alltime"]["pnl"]
        rel_sc, adj_pnl  = self._reliability_score(unsettled, realized_pnl)

        final = (bot_sc * 0.25 + perf["perf_score"] * 0.50 + rel_sc * 0.25)

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
        )

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
        Fetch leaderboard, score each wallet concurrently, return top N
        human traders sorted by final_score (bots filtered out).
        """
        lb = self.fetch_leaderboard(category=category, limit=max(limit * 5, 50))
        if not lb:
            # Fall back to cached results
            from edge_agent.memory.trader_cache import TraderCache
            cached = TraderCache().get_top(limit)
            return [TraderScore(**{k: v for k, v in r.items()
                                   if k in TraderScore.__dataclass_fields__})
                    for r in cached]

        scores: list[TraderScore] = []
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {
                pool.submit(self.score_trader, entry.get("proxyWallet", ""), entry): entry
                for entry in lb
                if entry.get("proxyWallet")
            }
            for fut in as_completed(futures):
                try:
                    ts = fut.result(timeout=20)
                    if not ts.bot_flag:
                        scores.append(ts)
                except Exception as exc:
                    log.debug("Scoring failed for a wallet: %s", exc)

        scores.sort(key=lambda s: s.final_score, reverse=True)
        return scores[:limit]
