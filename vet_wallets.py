"""
vet_wallets.py - Batch Wallet Vetting Script
=============================================

Runs full Tier-2 vetting on a list of Polymarket wallet addresses,
stores results in trader_cache.db, adds them to the watchlist for
ongoing tracking, and produces a formatted report + JSON export.

Usage:
    # Vet the seed wallets (stored in edge_agent/memory/data/seed_wallets.json)
    python vet_wallets.py

    # Vet specific wallets from the command line
    python vet_wallets.py 0xABC123 0xDEF456 0x789...

    # Vet seeds + extra wallets
    python vet_wallets.py --extra 0xABC123 0xDEF456

    # Skip watchlist registration (just score and report)
    python vet_wallets.py --no-watch

    # Save JSON export to a custom path
    python vet_wallets.py --out results/my_vet.json

    # Increase concurrency (default: 4 workers)
    python vet_wallets.py --workers 8

Output:
    • Formatted table printed to stdout
    • JSON export at ./vet_results_<timestamp>.json (or --out path)
    • All scores persisted to trader_cache.db (24h TTL)
    • All wallets added to watchlist table (6h re-vet cycle)
    • AI context snippet printed at end - paste into run_edge_bot for testing
"""
from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

# -- Make sure the project root is on sys.path so imports work ----------------
_PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(_PROJECT_ROOT))

# dat-ingestion has a hyphen so standard imports don't work - use importlib
_trader_mod   = importlib.import_module(".dat-ingestion.trader_api", "edge_agent")
TraderAPIClient = _trader_mod.TraderAPIClient
TraderScore     = _trader_mod.TraderScore

from edge_agent.memory.trader_cache import TraderCache

# -- Logging -------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("vet_wallets")

# -- Paths ---------------------------------------------------------------------
_SEED_FILE   = _PROJECT_ROOT / "edge_agent" / "memory" / "data" / "seed_wallets.json"
_DEFAULT_OUT = _PROJECT_ROOT / f"vet_results_{datetime.now(tz=timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _load_seed_wallets() -> list[dict]:
    """Load wallet list from seed_wallets.json. Returns [] if file missing."""
    if not _SEED_FILE.exists():
        log.warning("Seed file not found: %s", _SEED_FILE)
        return []
    try:
        with open(_SEED_FILE) as f:
            data = json.load(f)
        return data.get("wallets", [])
    except Exception as exc:
        log.error("Failed to load seed file: %s", exc)
        return []


def _normalise_addresses(raw: list[str]) -> list[str]:
    """Lowercase, strip, deduplicate, validate Ethereum address format."""
    seen: set[str] = set()
    result: list[str] = []
    for addr in raw:
        addr = addr.strip().lower()
        if not addr.startswith("0x") or len(addr) != 42:
            log.warning("Skipping invalid address: %s", addr)
            continue
        if addr not in seen:
            seen.add(addr)
            result.append(addr)
    return result


def _score_one(
    client: TraderAPIClient,
    address: str,
    profile: dict | None = None,
) -> TraderScore | None:
    """Vet a single wallet. Returns TraderScore or None on hard error."""
    try:
        log.info("  Vetting %s...", address[:10])
        ts = client.score_trader(address, profile or {})
        log.info(
            "  OK %s... score=%d bot=%d wins=%d pnl=$%.0f",
            address[:10],
            int(ts.final_score * 100),
            ts.bot_flag,
            int(ts.win_rate_alltime * 100),
            ts.pnl_alltime,
        )
        return ts
    except Exception as exc:
        log.error("  ✗ %s... failed: %s", address[:10], exc)
        return None


def _register_watchlist(
    cache: TraderCache,
    scores: list[TraderScore],
    seed_meta: dict[str, dict],
    added_by: str = "owner",
) -> int:
    """Add all successfully vetted wallets to the watchlist. Returns count added."""
    added = 0
    for ts in scores:
        if ts.bot_flag:
            log.info("  Skip watchlist for confirmed bot: %s...", ts.wallet_address[:10])
            continue
        meta = seed_meta.get(ts.wallet_address, {})
        vet_hours = int(meta.get("vet_interval_hours", 6))
        ok = cache.watchlist_add(
            address         = ts.wallet_address,
            display_name    = ts.display_name or ts.wallet_address[:10],
            added_by        = added_by,
            note            = meta.get("note", "batch vet import"),
            vet_interval_sec= vet_hours * 3600,
        )
        cache.watchlist_mark_vetted(
            address  = ts.wallet_address,
            score    = ts.final_score * 100,   # store 0–100 to match watchlist_vet_job
            bot_flag = ts.bot_flag,
        )
        added += 1
    return added


def _format_table(scores: list[TraderScore]) -> str:
    """Render a formatted ASCII table of results."""
    sep = "+" + "-"*25 + "+" + "-"*7 + "+" + "-"*10 + "+" + "-"*13 + "+" + "-"*14 + "+" + "-"*9 + "+"
    lines = [
        "",
        sep,
        "| Wallet                  | Score | Win Rate | PnL         | Volume       | Bot     |",
        sep,
    ]
    for ts in sorted(scores, key=lambda s: s.final_score, reverse=True):
        addr_short = f"{ts.wallet_address[:6]}...{ts.wallet_address[-4:]}"
        score_pct  = int(ts.final_score * 100)
        win_pct    = int(ts.win_rate_alltime * 100)
        pnl_str    = f"${ts.pnl_alltime:,.0f}"
        vol_str    = f"${ts.volume_alltime:,.0f}"
        bot_str    = "!!! BOT" if ts.bot_flag else "Human  "
        lines.append(
            f"| {addr_short:<23} | {score_pct:>5} | {win_pct:>7}% | {pnl_str:>11} | {vol_str:>12} | {bot_str:<7} |"
        )
    lines.append(sep)
    return "\n".join(lines)


def _build_ai_context(scores: list[TraderScore]) -> str:
    """
    Build an AI-ready context block for injecting into the system prompt.
    Shows only human wallets sorted by score.
    """
    humans = [s for s in scores if not s.bot_flag]
    humans.sort(key=lambda s: s.final_score, reverse=True)
    if not humans:
        return "[Seeded Wallets] No human wallets passed vetting."

    lines = [f"[Seeded Wallets - {len(humans)} human traders vetted]"]
    for ts in humans:
        cats = ts.top_categories or "Mixed"
        lines.append(
            f"• {ts.wallet_address[:10]}... | Score {int(ts.final_score*100)}/100 | "
            f"Win {int(ts.win_rate_alltime*100)}% | PnL ${ts.pnl_alltime:,.0f} | "
            f"Vol ${ts.volume_alltime:,.0f} | Speciality: {cats}"
        )
    return "\n".join(lines)


def _export_json(scores: list[TraderScore], path: Path) -> None:
    """Write full results to JSON for external analysis."""
    data = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "count":         len(scores),
        "results":       [ts.to_dict() for ts in scores],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    log.info("JSON export written to %s", path)


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Batch-vet Polymarket wallets and feed results into the AI context."
    )
    parser.add_argument(
        "addresses",
        nargs="*",
        help="Wallet addresses to vet (in addition to seed file wallets)",
    )
    parser.add_argument(
        "--extra",
        nargs="+",
        metavar="ADDR",
        help="Additional wallet addresses to vet (alternative to positional args)",
    )
    parser.add_argument(
        "--no-seed",
        action="store_true",
        help="Skip seed_wallets.json - only vet addresses from command line",
    )
    parser.add_argument(
        "--no-watch",
        action="store_true",
        help="Skip adding wallets to the watchlist",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=_DEFAULT_OUT,
        help="Path for JSON export (default: vet_results_<timestamp>.json)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Concurrent vetting workers (default: 4, max recommended: 8)",
    )
    parser.add_argument(
        "--min-score",
        type=int,
        default=45,
        help="Exclude results below this score (0-100) from report (still stored in DB). Default 45.",
    )
    args = parser.parse_args()

    # -- Gather wallets --------------------------------------------------------
    seed_entries: list[dict] = [] if args.no_seed else _load_seed_wallets()
    seed_addresses = _normalise_addresses([e["address"] for e in seed_entries])
    seed_meta      = {e["address"].lower(): e for e in seed_entries}

    cli_addresses = _normalise_addresses(
        (args.addresses or []) + (args.extra or [])
    )

    # Deduplicate: seed first (preserves metadata), then CLI extras
    all_addresses: list[str] = []
    seen: set[str] = set()
    for addr in seed_addresses + cli_addresses:
        if addr not in seen:
            seen.add(addr)
            all_addresses.append(addr)

    if not all_addresses:
        log.error("No wallet addresses to vet. Add addresses to seed_wallets.json or pass as args.")
        sys.exit(1)

    log.info("=" * 60)
    log.info("EDGE Batch Wallet Vetter")
    log.info("Wallets to vet: %d", len(all_addresses))
    log.info("Workers:        %d", args.workers)
    log.info("=" * 60)

    # -- Vet concurrently ------------------------------------------------------
    client = TraderAPIClient()
    scores: list[TraderScore] = []
    start  = time.time()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(
                _score_one,
                client,
                addr,
                {"proxyWallet": addr, **seed_meta.get(addr, {})},
            ): addr
            for addr in all_addresses
        }
        for fut in as_completed(futures):
            result = fut.result()
            if result is not None:
                scores.append(result)

    elapsed = time.time() - start
    log.info("Vetting complete: %d/%d succeeded in %.1fs", len(scores), len(all_addresses), elapsed)

    # -- Watchlist registration -------------------------------------------------
    if not args.no_watch and scores:
        cache   = TraderCache()
        n_added = _register_watchlist(cache, scores, seed_meta)
        log.info("Watchlist: %d wallets registered for ongoing 6h re-vet", n_added)

    # -- Report ----------------------------------------------------------------
    display_scores = (
        [s for s in scores if int(s.final_score * 100) >= args.min_score]
        if args.min_score > 0 else scores
    )

    print("\n" + "=" * 80)
    print(" EDGE WALLET VETTING REPORT")
    print(f" {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 80)
    print(_format_table(display_scores))

    # Score breakdown per wallet
    print("\n-- Detailed Breakdown ------------------------------------------------------")
    for ts in sorted(display_scores, key=lambda s: s.final_score, reverse=True):
        status    = "[BOT]" if ts.bot_flag else "[OK] "
        addr_disp = f"{ts.wallet_address[:8]}..."
        cats_disp = ts.top_categories or "Unknown"
        gl   = getattr(ts, "gl_ratio", 0.0)
        cwr  = getattr(ts, "copyable_win_rate", 0.0)
        gl_str  = f"{gl:.2f}x" if gl else "—"
        cwr_str = f"{cwr:.1%}" if cwr else "—"
        print(
            f"\n{status} {addr_disp}  [{int(ts.final_score * 100)}/100]\n"
            f"   Anti-bot: {int(ts.anti_bot_score*100):>3}/100  |  "
            f"Performance: {int(ts.performance_score*100):>3}/100  |  "
            f"Reliability: {int(ts.reliability_score*100):>3}/100\n"
            f"   Win Rate: {ts.win_rate_alltime:.1%}  |  "
            f"G/L Ratio: {gl_str}  |  "
            f"Copyable WR: {cwr_str}\n"
            f"   PnL: ${ts.pnl_alltime:,.2f}  |  "
            f"Volume: ${ts.volume_alltime:,.2f}  |  "
            f"Speciality: {cats_disp}\n"
            f"   Hidden-loss: ${ts.hidden_loss_exposure:,.2f}  |  "
            f"Fresh wallet: {'YES [WARN]' if ts.is_fresh_wallet else 'No'}"
        )
        tokens = getattr(ts, "tokens_launched", 0)
        chains = getattr(ts, "launch_chains", "")
        if tokens == 0:
            print("   Token launches: none detected on this address")
        else:
            print(f"   Token launches: {tokens} ({chains}) — use /adddev to score this dev separately")
        if ts.onchain_burst_flag:
            print("   [WARN] ON-CHAIN BURST FLAG: rapid-fire trades detected in 1-hour windows")
        if ts.hidden_loss_exposure > 500:
            print(f"   [WARN] HIDDEN LOSS: ${ts.hidden_loss_exposure:,.2f} in unresolved losing positions")

    # Summary stats
    humans = [s for s in scores if not s.bot_flag]
    bots   = [s for s in scores if s.bot_flag]
    if humans:
        avg_score = sum(s.final_score for s in humans) / len(humans)
        avg_wr    = sum(s.win_rate_alltime for s in humans) / len(humans)
        total_pnl = sum(s.pnl_alltime for s in humans)
        print(
            f"\n-- Summary -----------------------------------------------------------------\n"
            f"   Total vetted:  {len(scores)}\n"
            f"   Human traders: {len(humans)}  |  Bots flagged: {len(bots)}\n"
            f"   Avg score:     {int(avg_score * 100)}/100\n"
            f"   Avg win rate:  {avg_wr:.1%}\n"
            f"   Combined PnL:  ${total_pnl:,.2f}"
        )

    # -- AI Context Block ------------------------------------------------------
    ai_ctx = _build_ai_context(scores)
    print(
        f"\n-- AI Context Block (inject into system prompt for testing) -----------------\n"
        f"{ai_ctx}\n"
    )

    # -- JSON Export -----------------------------------------------------------
    _export_json(scores, args.out)
    print(f"Full results exported -> {args.out}\n")


if __name__ == "__main__":
    main()
