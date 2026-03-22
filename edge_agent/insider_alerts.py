"""
Insider Alert Engine — Polymarket Whale & Insider Bet Detection
===============================================================

Strategy:
  1. MARKET SWEEP  — every 5 min, poll Gamma API for niche markets ($25K-$1M volume)
                     that show sudden price moves (>8pp) since last cycle.
  2. TRADE TRACE   — for each flagged market, pull recent CLOB trades to find the
                     wallet(s) behind the move.
  3. WALLET PROFILE — check the wallet's lifetime history via Data API activity endpoint.
                      Fresh wallet + concentrated bet = high suspicion.
  4. SUSPICION SCORE — 0-100 composite score across 6 signals (freshness, size, niche,
                       concentration, timing, certainty filter).
  5. AI RESEARCH   — for score >= 50, fire web search for recent news on the market
                     topic to find confirmation that hasn't spread yet.
  6. ALERT         — formatted Telegram message to dedicated alert channel with brief,
                     confidence rating, and entry price (if market still open).
  7. RESOLUTION    — when a market settles, auto-tag the wallet as confirmed insider
                     if the bet paid off and add to watchlist for copy-trade tracking.

DB: edge_agent/memory/data/insider_alerts.db
  - price_snapshots  : last-seen price per market for delta detection
  - seen_trades      : trade IDs already processed (dedup)
  - alert_log        : every alert fired, wallet, market, score, outcome
"""
from __future__ import annotations

import html
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Awaitable

import requests

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# API endpoints (all public, no auth required)
# ---------------------------------------------------------------------------
_GAMMA_BASE = "https://gamma-api.polymarket.com"
_DATA_API   = "https://data-api.polymarket.com"
_CLOB_BASE  = "https://clob.polymarket.com"

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "EdgeInsiderBot/1.0"})

# ---------------------------------------------------------------------------
# Tuning constants
# ---------------------------------------------------------------------------
_MIN_MARKET_VOL      = 25_000     # ignore markets smaller than $25K (too illiquid)
_MAX_MARKET_VOL      = 2_000_000  # ignore mega-markets (insiders prefer mid-size)
_PRICE_MOVE_THRESH   = 0.08       # 8pp move in one cycle = suspicious
_MIN_TRADE_USD       = 1_500      # trades below $1,500 are ignored (noise)
_SUSPICION_FIRE_THR  = 45         # minimum suspicion score to fire an AI research + alert
_MAX_MARKETS_PER_RUN = 60         # cap per scan cycle to avoid rate-limiting
_WALLET_CACHE_TTL    = 3_600      # 1h — don't re-profile the same wallet within 1h
_DB_PATH = Path(__file__).parent / "memory" / "data" / "insider_alerts.db"

# Categories where insider activity is monitored.
# Sports included: early injury leaks, lineup changes, and match-fixing all
# show up as fresh wallets placing large bets before public news breaks.
_INSIDER_CATEGORIES = {
    # Non-sports (classic insider domains)
    "politics", "geopolitics", "elections", "business", "tech",
    "crypto", "economy", "science", "entertainment",
    # Sports — fresh wallet + large bet before news breaks
    "nba", "nhl", "nfl", "mlb", "nfl-super-bowl", "soccer",
    "sports", "basketball", "football", "hockey", "baseball",
    "ufc", "mma", "boxing", "tennis", "golf", "ncaa", "march-madness",
    "college-football", "cfb", "cbb", "wnba",
}

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class WalletProfile:
    address:         str
    lifetime_trades: int   = 0
    lifetime_markets: int  = 0
    total_volume_usd: float = 0.0
    first_seen_days_ago: float = 999.0   # how old is the wallet's first trade
    is_fresh:        bool  = True
    single_market_focus: bool = False   # only ever traded 1 market
    profiled_at:     float = field(default_factory=time.time)


@dataclass
class SuspicionResult:
    score:   int              # 0-100
    signals: list[str]        # human-readable signal descriptions
    verdict: str              # "HIGH" / "MEDIUM" / "LOW" / "NOISE"


@dataclass
class InsiderAlert:
    alert_id:       str
    wallet:         str
    market_id:      str       # conditionId
    market_question: str
    market_vol_24h: float
    current_price:  float     # YES price at time of detection (cents)
    trade_size_usd: float
    suspicion:      SuspicionResult
    research:       str       # AI web search summary
    fired_at:       float = field(default_factory=time.time)
    outcome:        str   = "pending"   # "win" / "loss" / "pending"


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS price_snapshots (
            condition_id   TEXT PRIMARY KEY,
            question       TEXT NOT NULL DEFAULT '',
            last_price     REAL NOT NULL DEFAULT 0.5,
            vol_24h        REAL NOT NULL DEFAULT 0,
            category       TEXT NOT NULL DEFAULT '',
            updated_at     REAL NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS seen_trades (
            trade_id       TEXT PRIMARY KEY,
            condition_id   TEXT NOT NULL,
            wallet         TEXT NOT NULL,
            size_usd       REAL NOT NULL,
            seen_at        REAL NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS alert_log (
            alert_id       TEXT PRIMARY KEY,
            wallet         TEXT NOT NULL,
            condition_id   TEXT NOT NULL,
            question       TEXT NOT NULL DEFAULT '',
            trade_size_usd REAL NOT NULL,
            suspicion_score INTEGER NOT NULL,
            signals        TEXT NOT NULL DEFAULT '[]',
            research       TEXT NOT NULL DEFAULT '',
            current_price  REAL NOT NULL DEFAULT 0,
            outcome        TEXT NOT NULL DEFAULT 'pending',
            fired_at       REAL NOT NULL DEFAULT 0,
            resolved_at    REAL
        );

        CREATE TABLE IF NOT EXISTS wallet_profile_cache (
            address        TEXT PRIMARY KEY,
            profile_json   TEXT NOT NULL,
            cached_at      REAL NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_seen_trades_cid ON seen_trades(condition_id);
        CREATE INDEX IF NOT EXISTS idx_alert_log_wallet ON alert_log(wallet);
        CREATE INDEX IF NOT EXISTS idx_alert_log_fired ON alert_log(fired_at);
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# Polymarket API calls
# ---------------------------------------------------------------------------

def _fetch_niche_markets(limit: int = _MAX_MARKETS_PER_RUN) -> list[dict]:
    """
    Pull active markets in the volume sweet spot ($25K–$2M) where insider
    activity is most impactful. Returns up to `limit` markets ordered by
    24h volume descending so we cover the most-traded niche markets first.
    """
    try:
        resp = _SESSION.get(
            f"{_GAMMA_BASE}/markets",
            params={
                "active":    "true",
                "closed":    "false",
                "limit":     min(limit * 2, 200),   # over-fetch then filter
                "order":     "volume24hrClob",
                "ascending": "false",
            },
            timeout=12,
        )
        resp.raise_for_status()
        markets = resp.json()

        filtered = []
        for m in markets:
            try:
                vol = float(m.get("volume24hrClob") or m.get("volumeNum") or 0)
                if _MIN_MARKET_VOL <= vol <= _MAX_MARKET_VOL:
                    filtered.append(m)
                    if len(filtered) >= limit:
                        break
            except (TypeError, ValueError):
                continue

        log.debug("[insider] Fetched %d niche markets (from %d)", len(filtered), len(markets))
        return filtered

    except Exception as exc:
        log.warning("[insider] Market fetch failed: %s", exc)
        return []


def _fetch_recent_trades(condition_id: str, limit: int = 50) -> list[dict]:
    """
    Pull recent fills from the CLOB for a specific market.
    Returns list of trade dicts with: id, market, size, price, maker, side, timestamp.
    No auth required.
    """
    try:
        resp = _SESSION.get(
            f"{_CLOB_BASE}/trades",
            params={"market": condition_id, "limit": limit},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        # CLOB returns {"data": [...]} or a list directly
        if isinstance(data, dict):
            return data.get("data", [])
        if isinstance(data, list):
            return data
        return []
    except Exception as exc:
        log.debug("[insider] CLOB trades fetch failed for %s: %s", condition_id[:12], exc)
        return []


def _profile_wallet_fresh(address: str) -> WalletProfile:
    """
    Pull lifetime activity for a wallet via Data API.
    Returns WalletProfile with freshness indicators.
    Uses a high limit to get a representative sample.
    """
    profile = WalletProfile(address=address)
    try:
        resp = _SESSION.get(
            f"{_DATA_API}/v1/activity",
            params={"user": address, "limit": 500},
            timeout=12,
        )
        resp.raise_for_status()
        activity = resp.json()

        if not activity:
            # Zero history — brand new wallet
            profile.lifetime_trades = 0
            profile.is_fresh = True
            profile.single_market_focus = True
            return profile

        # Count unique conditionIds and total volume
        condition_ids: set[str] = set()
        total_vol = 0.0
        earliest_ts_ms = float("inf")
        now_ms = time.time() * 1000

        for event in activity:
            cid = event.get("conditionId") or event.get("market", "")
            if cid:
                condition_ids.add(cid)

            # Volume from cash flows
            amount = 0.0
            try:
                amount = abs(float(event.get("usdcSize") or event.get("amount") or 0))
            except (TypeError, ValueError):
                pass
            total_vol += amount

            # Track earliest timestamp
            try:
                ts = float(event.get("timestamp") or event.get("created_at") or 0)
                if ts > 0 and ts < earliest_ts_ms:
                    earliest_ts_ms = ts
            except (TypeError, ValueError):
                pass

        profile.lifetime_trades = len(activity)
        profile.lifetime_markets = len(condition_ids)
        profile.total_volume_usd = round(total_vol, 2)
        profile.single_market_focus = len(condition_ids) <= 1

        # Freshness: how old is this wallet?
        if earliest_ts_ms < float("inf"):
            # timestamp in milliseconds if > 1e12, else seconds
            if earliest_ts_ms > 1e12:
                earliest_ts_ms /= 1000
            days_old = (time.time() - earliest_ts_ms) / 86400
            profile.first_seen_days_ago = round(days_old, 1)
        else:
            profile.first_seen_days_ago = 0.0

        # Fresh: wallet younger than 7 days OR fewer than 20 lifetime trades
        profile.is_fresh = (
            profile.first_seen_days_ago < 7
            or profile.lifetime_trades < 20
        )

    except Exception as exc:
        log.debug("[insider] Wallet profile failed for %s: %s", address[:10], exc)

    return profile


# ---------------------------------------------------------------------------
# Suspicion scorer
# ---------------------------------------------------------------------------

def score_suspicion(
    trade_size_usd: float,
    current_price: float,
    market_vol_24h: float,
    profile: WalletProfile,
) -> SuspicionResult:
    """
    Score a trade for insider suspicion on a 0-100 scale.

    Signal breakdown:
      Fresh wallet         — 0 or 30 pts  (most important signal)
      Large position       — 0-25 pts     (size relative to market volume)
      Niche market         — 0-20 pts     (lower volume = more suspicious)
      Concentrated focus   — 0-15 pts     (single-market wallet)
      Genuine uncertainty  — gate only    (price 15-82% — not certainty farming)
      Fast entry           — 0-10 pts     (wallet age < 3 days)
    """
    signals: list[str] = []
    score = 0

    # --- Gate: filter out near-certain outcomes (certainty farming / spam) ---
    if current_price > 0.82 or current_price < 0.12:
        return SuspicionResult(
            score=0,
            signals=["FILTERED: near-certain outcome (certainty farming or spam)"],
            verdict="NOISE",
        )

    # --- Signal 1: Fresh wallet ---
    if profile.lifetime_trades < 5:
        score += 30
        signals.append(f"Brand new wallet ({profile.lifetime_trades} lifetime trades)")
    elif profile.lifetime_trades < 20:
        score += 18
        signals.append(f"Very fresh wallet ({profile.lifetime_trades} trades, {profile.first_seen_days_ago:.0f}d old)")
    elif profile.is_fresh:
        score += 10
        signals.append(f"Relatively fresh wallet ({profile.first_seen_days_ago:.0f}d old)")

    # --- Signal 2: Large position size ---
    if trade_size_usd >= 10_000:
        score += 25
        signals.append(f"Very large position: ${trade_size_usd:,.0f}")
    elif trade_size_usd >= 5_000:
        score += 18
        signals.append(f"Large position: ${trade_size_usd:,.0f}")
    elif trade_size_usd >= 2_000:
        score += 10
        signals.append(f"Significant position: ${trade_size_usd:,.0f}")
    else:
        score += 3
        signals.append(f"Position: ${trade_size_usd:,.0f}")

    # --- Signal 3: Niche market (lower volume = harder for public to have info) ---
    if market_vol_24h < 50_000:
        score += 20
        signals.append(f"Very niche market (${market_vol_24h:,.0f} 24h vol)")
    elif market_vol_24h < 150_000:
        score += 12
        signals.append(f"Niche market (${market_vol_24h:,.0f} 24h vol)")
    elif market_vol_24h < 500_000:
        score += 5
        signals.append(f"Mid-size market (${market_vol_24h:,.0f} 24h vol)")

    # --- Signal 4: Single-market concentration ---
    if profile.single_market_focus:
        score += 15
        signals.append("Wallet trades only this market (laser-focused)")
    elif profile.lifetime_markets <= 3:
        score += 8
        signals.append(f"Wallet only active in {profile.lifetime_markets} markets")

    # --- Signal 5: Very new wallet (< 3 days) ---
    if profile.first_seen_days_ago < 3 and profile.lifetime_trades < 10:
        score += 10
        signals.append(f"Wallet created {profile.first_seen_days_ago:.1f} days ago — likely created for this bet")

    # Cap at 100
    score = min(score, 100)

    if score >= 70:
        verdict = "HIGH"
    elif score >= 50:
        verdict = "MEDIUM-HIGH"
    elif score >= _SUSPICION_FIRE_THR:
        verdict = "MEDIUM"
    else:
        verdict = "LOW"

    return SuspicionResult(score=score, signals=signals, verdict=verdict)


# ---------------------------------------------------------------------------
# Main engine class
# ---------------------------------------------------------------------------

class InsiderAlertEngine:
    """
    Stateful engine that runs periodic insider detection scans.

    Typical usage (from run_edge_bot.py background job):
        engine = InsiderAlertEngine(search_fn=_web_search, send_fn=_send_to_channel)
        await engine.run_scan()   # call every 5 minutes
    """

    def __init__(
        self,
        search_fn: Callable[[str], str] | None = None,
        ai_brief_fn: Callable[[str, str], Awaitable[str]] | None = None,
    ) -> None:
        """
        Args:
            search_fn:    sync function(query: str) -> str  (Tavily or Serper)
            ai_brief_fn:  async function(market_question: str, search_context: str) -> str
        """
        self._search_fn   = search_fn
        self._ai_brief_fn = ai_brief_fn
        self._conn        = _connect()
        _init_db(self._conn)
        # In-memory wallet profile cache (supplement the DB cache)
        self._profile_cache: dict[str, WalletProfile] = {}
        log.info("[insider] InsiderAlertEngine initialised — DB: %s", _DB_PATH)

    # ------------------------------------------------------------------
    # Price snapshot management
    # ------------------------------------------------------------------

    def _get_last_price(self, condition_id: str) -> float | None:
        row = self._conn.execute(
            "SELECT last_price FROM price_snapshots WHERE condition_id = ?",
            (condition_id,),
        ).fetchone()
        return float(row["last_price"]) if row else None

    def _update_snapshot(self, market: dict, price: float) -> None:
        cid = market.get("conditionId", "")
        question = market.get("question", "")[:300]
        vol = float(market.get("volume24hrClob") or market.get("volumeNum") or 0)
        tags = json.dumps(market.get("tags") or [])
        self._conn.execute("""
            INSERT INTO price_snapshots (condition_id, question, last_price, vol_24h, category, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(condition_id) DO UPDATE SET
                last_price = excluded.last_price,
                vol_24h    = excluded.vol_24h,
                updated_at = excluded.updated_at
        """, (cid, question, price, vol, tags, time.time()))
        self._conn.commit()

    # ------------------------------------------------------------------
    # Trade deduplication
    # ------------------------------------------------------------------

    def _is_new_trade(self, trade_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM seen_trades WHERE trade_id = ?", (trade_id,)
        ).fetchone()
        return row is None

    def _mark_trade_seen(self, trade_id: str, condition_id: str, wallet: str, size: float) -> None:
        try:
            self._conn.execute(
                "INSERT OR IGNORE INTO seen_trades (trade_id, condition_id, wallet, size_usd, seen_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (trade_id, condition_id, wallet, size, time.time()),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            log.debug("[insider] mark_trade_seen error: %s", exc)

    # ------------------------------------------------------------------
    # Wallet profiling (with cache)
    # ------------------------------------------------------------------

    def _get_profile(self, address: str) -> WalletProfile:
        """Return cached profile or fetch fresh."""
        # Check in-memory cache
        if address in self._profile_cache:
            p = self._profile_cache[address]
            if time.time() - p.profiled_at < _WALLET_CACHE_TTL:
                return p

        # Check DB cache
        row = self._conn.execute(
            "SELECT profile_json, cached_at FROM wallet_profile_cache WHERE address = ?",
            (address,),
        ).fetchone()
        if row and time.time() - float(row["cached_at"]) < _WALLET_CACHE_TTL:
            try:
                data = json.loads(row["profile_json"])
                p = WalletProfile(**data)
                self._profile_cache[address] = p
                return p
            except Exception:
                pass

        # Fresh fetch
        p = _profile_wallet_fresh(address)
        self._profile_cache[address] = p
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO wallet_profile_cache (address, profile_json, cached_at) "
                "VALUES (?, ?, ?)",
                (address, json.dumps(asdict(p)), time.time()),
            )
            self._conn.commit()
        except sqlite3.Error:
            pass
        return p

    # ------------------------------------------------------------------
    # AI research
    # ------------------------------------------------------------------

    def _research_market(self, market_question: str) -> str:
        """
        Run two targeted web searches for a flagged market and return a
        compact summary of any confirmation signals found.
        """
        if not self._search_fn:
            return "(no search function configured)"

        results: list[str] = []

        # Search 1: breaking news on the topic
        query1 = f"{market_question} news announcement recent"
        try:
            r1 = self._search_fn(query1, max_results=4)
            if r1:
                results.append(r1)
        except Exception as exc:
            log.debug("[insider] research search1 failed: %s", exc)

        # Search 2: look for leaks, insider signals, unusual activity
        # Strip common question prefixes to get to the core topic
        topic = market_question
        for prefix in ("Will ", "Does ", "Has ", "Is ", "Can ", "Did ", "When will ", "Who will "):
            if topic.startswith(prefix):
                topic = topic[len(prefix):]
                break
        query2 = f"{topic[:80]} insider leak rumor confirmation"
        try:
            r2 = self._search_fn(query2, max_results=3)
            if r2:
                results.append(r2)
        except Exception as exc:
            log.debug("[insider] research search2 failed: %s", exc)

        return "\n".join(results) if results else "(no relevant news found)"

    # ------------------------------------------------------------------
    # Alert formatting
    # ------------------------------------------------------------------

    @staticmethod
    def _fmt_alert(alert: InsiderAlert) -> str:
        """Format the Telegram HTML alert message."""
        e = html.escape
        verdict = alert.suspicion.verdict
        emoji = {
            "HIGH":        "🚨",
            "MEDIUM-HIGH": "⚠️",
            "MEDIUM":      "🔍",
        }.get(verdict, "📊")

        signals_text = "\n".join(f"  • {e(s)}" for s in alert.suspicion.signals)

        price_pct = int(alert.current_price * 100)
        entry_note = ""
        if alert.current_price < 0.75:
            entry_note = f"  Entry still open at <b>{price_pct}%</b> YES"
        else:
            entry_note = f"  Market at <b>{price_pct}%</b> — entry window closing"

        # Trim research block to keep alert readable
        research_lines = alert.research.strip().splitlines()
        research_trim = "\n".join(research_lines[:8])
        if len(research_lines) > 8:
            research_trim += "\n  ..."

        addr_short = alert.wallet[:6] + "..." + alert.wallet[-4:]

        lines = [
            f"{emoji} <b>INSIDER ALERT — Suspicion {alert.suspicion.score}/100 [{verdict}]</b>",
            "",
            f"<b>Market:</b> <i>{e(alert.market_question[:120])}</i>",
            f"<b>Wallet:</b> <code>{e(addr_short)}</code>",
            f"<b>Position:</b> ${alert.trade_size_usd:,.0f} YES",
            entry_note,
            f"<b>24h Vol:</b> ${alert.market_vol_24h:,.0f}",
            "",
            "<b>Signals detected:</b>",
            signals_text,
            "",
            "<b>What I found:</b>",
            f"<i>{e(research_trim)}</i>",
            "",
            f"<code>{e(alert.wallet)}</code>",
            f"<a href=\"https://polymarket.com/profile/{e(alert.wallet)}\">View wallet on Polymarket</a>",
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Per-market wallet dedup — avoid spamming the same alert across cycles
    # ------------------------------------------------------------------

    def _already_alerted(self, wallet: str, condition_id: str, window_hours: int = 24) -> bool:
        """Return True if we already fired an alert for this wallet+market in the last window_hours."""
        cutoff = time.time() - window_hours * 3600
        row = self._conn.execute(
            "SELECT 1 FROM alert_log WHERE wallet = ? AND condition_id = ? AND fired_at >= ?",
            (wallet, condition_id, cutoff),
        ).fetchone()
        return row is not None

    # ------------------------------------------------------------------
    # Alert persistence
    # ------------------------------------------------------------------

    def _save_alert(self, alert: InsiderAlert) -> None:
        try:
            self._conn.execute("""
                INSERT OR IGNORE INTO alert_log
                    (alert_id, wallet, condition_id, question, trade_size_usd,
                     suspicion_score, signals, research, current_price, outcome, fired_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                alert.alert_id,
                alert.wallet,
                alert.market_id,
                alert.market_question,
                alert.trade_size_usd,
                alert.suspicion.score,
                json.dumps(alert.suspicion.signals),
                alert.research[:2000],
                alert.current_price,
                alert.outcome,
                alert.fired_at,
            ))
            self._conn.commit()
        except sqlite3.Error as exc:
            log.warning("[insider] save_alert error: %s", exc)

    # ------------------------------------------------------------------
    # Resolution tracking — call this from outcome_tracker integration
    # ------------------------------------------------------------------

    def record_outcome(self, condition_id: str, resolved_yes: bool) -> list[str]:
        """
        When a market resolves, update any pending alerts for that market.
        Returns list of wallet addresses that won (for auto-watchlist addition).

        Call this from the OutcomeTracker resolution callback.
        """
        outcome = "win" if resolved_yes else "loss"
        try:
            self._conn.execute("""
                UPDATE alert_log
                SET outcome = ?, resolved_at = ?
                WHERE condition_id = ? AND outcome = 'pending'
            """, (outcome, time.time(), condition_id))
            self._conn.commit()

            if resolved_yes:
                rows = self._conn.execute(
                    "SELECT wallet FROM alert_log WHERE condition_id = ? AND outcome = 'win'",
                    (condition_id,),
                ).fetchall()
                return [r["wallet"] for r in rows]
        except sqlite3.Error as exc:
            log.warning("[insider] record_outcome error: %s", exc)
        return []

    # ------------------------------------------------------------------
    # Recent alerts — for /insider command
    # ------------------------------------------------------------------

    def get_recent_alerts(self, limit: int = 10) -> list[dict]:
        """Return the most recent alerts for display via /insider command."""
        try:
            rows = self._conn.execute("""
                SELECT alert_id, wallet, question, trade_size_usd, suspicion_score,
                       current_price, outcome, fired_at
                FROM alert_log
                ORDER BY fired_at DESC
                LIMIT ?
            """, (limit,)).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.Error:
            return []

    # ------------------------------------------------------------------
    # Main scan cycle
    # ------------------------------------------------------------------

    async def run_scan(self, send_alert_fn: Callable[[str], Awaitable[None]]) -> int:
        """
        Full scan cycle:
          1. Fetch niche markets
          2. Detect price moves vs last snapshot
          3. Pull recent CLOB trades for moved markets
          4. Filter + score suspicious wallets
          5. Research + alert for high scorers

        Returns count of alerts fired this cycle.
        """
        alerts_fired = 0
        markets = _fetch_niche_markets()

        if not markets:
            log.warning("[insider] No markets fetched — skipping scan")
            return 0

        for market in markets:
            cid = market.get("conditionId", "")
            if not cid:
                continue

            # Skip non-insider categories (sports markets are not insider-tradeable)
            tags = market.get("tags") or []
            if isinstance(tags, str):
                try:
                    tags = json.loads(tags)
                except Exception:
                    tags = []
            tag_slugs = {
                (t.get("slug") or t.get("label") or t if isinstance(t, str) else "").lower()
                for t in tags
            }
            # If any tag slug overlaps with insider categories, allow it.
            # If no tags match AND the set isn't empty (i.e., tags exist), skip.
            if tag_slugs and not tag_slugs.intersection(_INSIDER_CATEGORIES):
                continue

            # Current YES price
            try:
                prices = market.get("outcomePrices") or []
                # Gamma API returns outcomePrices as a JSON-encoded string
                if isinstance(prices, str):
                    try:
                        prices = json.loads(prices)
                    except (json.JSONDecodeError, ValueError):
                        prices = []
                current_price = float(prices[0]) if prices else float(market.get("lastTradePrice") or 0.5)
            except (TypeError, ValueError, IndexError):
                current_price = 0.5

            vol_24h = float(market.get("volume24hrClob") or market.get("volumeNum") or 0)
            question = market.get("question", "Unknown market")

            # Check price delta vs last snapshot
            last_price = self._get_last_price(cid)
            price_moved = (
                last_price is not None
                and abs(current_price - last_price) >= _PRICE_MOVE_THRESH
            )

            # Update snapshot regardless
            self._update_snapshot(market, current_price)

            # Skip if price hasn't moved significantly (only investigate movers)
            # Exception: if we've never seen this market before, add it but don't alert yet
            if not price_moved and last_price is not None:
                continue
            if last_price is None:
                # First time seeing this market — snapshot recorded, move on
                continue

            log.info(
                "[insider] Price move detected: %s | %.2f -> %.2f (%.0fpp) | vol=$%.0f",
                question[:50], last_price, current_price,
                abs(current_price - last_price) * 100, vol_24h,
            )

            # Pull recent CLOB trades for this market
            trades = _fetch_recent_trades(cid, limit=50)
            if not trades:
                continue

            # Filter to large recent BUY trades from unknown wallets
            now = time.time()
            cutoff = now - 900   # last 15 minutes — 3× scan interval avoids dropping trades during API delays

            for trade in trades:
                trade_id = trade.get("id") or trade.get("tradeId") or ""
                if not trade_id or not self._is_new_trade(trade_id):
                    continue

                # Only flag BUY orders — insiders accumulate YES (or NO) positions,
                # they don't sell. SELL orders from the maker's perspective = reducing exposure.
                side = (trade.get("side") or trade.get("makerSide") or "").upper()
                if side and side not in ("BUY", ""):
                    self._mark_trade_seen(trade_id, cid, "", 0)
                    continue

                # Parse trade fields (CLOB format varies slightly)
                wallet = (
                    trade.get("maker")
                    or trade.get("makerAddress")
                    or trade.get("maker_address")
                    or ""
                )
                if not wallet or wallet.lower() in ("", "0x0000000000000000000000000000000000000000"):
                    continue

                # Trade size in USD
                # CLOB returns size in SHARES (tokens), usdcSize in USD.
                # Always prefer usdcSize; when absent, compute shares × price.
                try:
                    usdc_size = trade.get("usdcSize") or trade.get("matchedAmount")
                    if usdc_size is not None:
                        size = float(usdc_size)
                    else:
                        shares = float(trade.get("size") or 0)
                        price_fill = float(trade.get("price") or current_price)
                        size = shares * price_fill if price_fill > 0 else shares
                except (TypeError, ValueError):
                    size = 0.0

                if size < _MIN_TRADE_USD:
                    self._mark_trade_seen(trade_id, cid, wallet, size)
                    continue

                # Trade timestamp check
                try:
                    ts = float(trade.get("timestamp") or trade.get("created_at") or now)
                    if ts > 1e12:
                        ts /= 1000
                    if ts < cutoff:
                        self._mark_trade_seen(trade_id, cid, wallet, size)
                        continue
                except (TypeError, ValueError):
                    pass

                # Mark seen before processing to avoid double-alerts on retry
                self._mark_trade_seen(trade_id, cid, wallet, size)

                # Profile the wallet
                profile = self._get_profile(wallet)

                # Score suspicion
                result = score_suspicion(
                    trade_size_usd=size,
                    current_price=current_price,
                    market_vol_24h=vol_24h,
                    profile=profile,
                )

                log.info(
                    "[insider] %s | wallet %s | $%.0f | score %d [%s]",
                    question[:40], wallet[:10], size, result.score, result.verdict,
                )

                if result.score < _SUSPICION_FIRE_THR:
                    continue  # not interesting enough

                # Dedup: don't re-alert the same wallet on the same market within 24h
                if self._already_alerted(wallet, cid):
                    log.debug(
                        "[insider] Skipping duplicate alert: wallet=%s market=%s",
                        wallet[:10], question[:40],
                    )
                    continue

                # AI research
                research = self._research_market(question)

                # Build alert
                import uuid
                alert = InsiderAlert(
                    alert_id=str(uuid.uuid4())[:8],
                    wallet=wallet,
                    market_id=cid,
                    market_question=question,
                    market_vol_24h=vol_24h,
                    current_price=current_price,
                    trade_size_usd=size,
                    suspicion=result,
                    research=research,
                )
                self._save_alert(alert)

                # Format and send
                try:
                    msg = self._fmt_alert(alert)
                    await send_alert_fn(msg)
                    alerts_fired += 1
                    log.info(
                        "[insider] Alert fired: score=%d wallet=%s market=%s",
                        result.score, wallet[:10], question[:40],
                    )
                except Exception as exc:
                    log.warning("[insider] Failed to send alert: %s", exc)

        return alerts_fired

    # ------------------------------------------------------------------
    # Cleanup — keep seen_trades table from growing unbounded
    # ------------------------------------------------------------------

    def cleanup_old_records(self, days: int = 30) -> None:
        """Prune records older than `days` days."""
        cutoff = time.time() - (days * 86400)
        try:
            self._conn.execute("DELETE FROM seen_trades WHERE seen_at < ?", (cutoff,))
            self._conn.execute(
                "DELETE FROM price_snapshots WHERE updated_at < ?", (cutoff,)
            )
            self._conn.execute(
                "DELETE FROM wallet_profile_cache WHERE cached_at < ?",
                (time.time() - _WALLET_CACHE_TTL * 2,),
            )
            self._conn.commit()
            log.info("[insider] Cleanup complete (cutoff=%dd)", days)
        except sqlite3.Error as exc:
            log.warning("[insider] Cleanup error: %s", exc)
