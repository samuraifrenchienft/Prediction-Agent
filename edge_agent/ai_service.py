"""
AI service — free model rotation with Groq fallback.
=====================================================

Model priority (tried in order per task type):
  1. OpenRouter free tier — multiple models attempted in sequence
     Skips to next when a model returns 402 (credits) or 429 (rate limit)
  2. Groq free tier — final fallback if all OpenRouter models fail

API keys in .env:
  OPEN_ROUTER_API_KEY = sk-or-...
  GROQ_API_KEY        = gsk_...

Decision logging:
  Every successful AI call is written to decision_log.db so you can
  always answer: which model answered, how long it took, what prompt
  version was used, and what context blocks were active.
  Inject the log via set_decision_log() at startup.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from dotenv import find_dotenv, load_dotenv
from openai import OpenAI, APIStatusError, APIConnectionError

from .models import AIAnalysis  # noqa: F401 — re-exported for other modules


def _load_dotenv_chain() -> None:
    """Match run_edge_bot: local .env then parents fill missing keys (worktree inherits main)."""
    seen: list[Path] = []
    cur = Path(__file__).resolve().parent
    for _ in range(14):
        p = cur / ".env"
        if p.is_file():
            rp = p.resolve()
            if rp not in seen:
                seen.append(rp)
        cur = cur.parent
        if cur == cur.parent:
            break
    if not seen:
        load_dotenv(find_dotenv(usecwd=True) or find_dotenv())
        return
    for i, p in enumerate(seen):
        load_dotenv(p, override=(i == 0))


_load_dotenv_chain()

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional decision log — injected at startup by run_edge_bot.py
# When set, every successful AI call is written to decision_log.db
# ---------------------------------------------------------------------------
_decision_log = None   # type: ignore[assignment]  # DecisionLog | None


def set_decision_log(dlog: object) -> None:
    """Inject the DecisionLog instance. Call once at startup."""
    global _decision_log
    _decision_log = dlog
    log.info("[AI] DecisionLog wired — all AI calls will be audited.")

# ---------------------------------------------------------------------------
# Free model rotation lists — tried left-to-right until one succeeds.
# 402 = out of free credits on that model → skip
# 429 = rate limit on that model → skip
# 503 = model unavailable → skip
# ---------------------------------------------------------------------------

# Updated 2026-03-27 — verified from openrouter.ai/collections/free-models
# These are the confirmed active free-tier models on OpenRouter.
# openrouter/free is the catch-all smart router — always works as final fallback.
# Benchmarked 2026-03-27 — ordered by confirmed response speed + reliability.
# arcee/trinity: 1.0s, confirmed working | nemotron-nano: 2.5s, confirmed working
# stepfun + nemotron-super return empty content → placed at back as last-resort
# mistral-small / gemma-3 / llama-3.3 may 429 if rate-limited but recover in 60s
_OR_FREE_SIMPLE: list[str] = [
    "arcee-ai/trinity-large-preview:free",              # 1.0s confirmed — fastest working model
    "nvidia/nemotron-3-nano-30b-a3b:free",              # 2.5s confirmed — reliable fallback
    "mistralai/mistral-small-3.1-24b-instruct:free",    # fast when not rate-limited
    "google/gemma-3-12b-it:free",                       # solid fallback
    "meta-llama/llama-3.3-70b-instruct:free",           # strong fallback
    "minimax/minimax-m2.5:free",                        # large ctx fallback
    "stepfun/step-3.5-flash:free",                      # sometimes returns empty — last resort
]

_OR_FREE_COMPLEX: list[str] = [
    "arcee-ai/trinity-large-preview:free",              # 400B sparse MoE — strong reasoning
    "nvidia/nemotron-3-nano-30b-a3b:free",              # reliable and fast
    "mistralai/mistral-small-3.1-24b-instruct:free",    # 128k ctx — good for complex
    "meta-llama/llama-3.3-70b-instruct:free",           # strong reasoning
    "google/gemma-3-27b-it:free",                       # capable fallback
    "minimax/minimax-m2.5:free",                        # large ctx fallback
    "stepfun/step-3.5-flash:free",                      # last resort
]

_OR_FREE_CREATIVE: list[str] = [
    "arcee-ai/trinity-large-preview:free",              # 1.0s — best speed + quality
    "nvidia/nemotron-3-nano-30b-a3b:free",              # 2.5s — reliable
    "mistralai/mistral-small-3.1-24b-instruct:free",    # good creative writing
    "google/gemma-3-12b-it:free",                       # solid creative fallback
    "meta-llama/llama-3.3-70b-instruct:free",           # strong fallback
    "minimax/minimax-m2.5:free",                        # large ctx fallback
    "stepfun/step-3.5-flash:free",                      # last resort
]

_OR_FREE_MAP: dict[str, list[str]] = {
    "simple":   _OR_FREE_SIMPLE,
    "complex":  _OR_FREE_COMPLEX,
    "creative": _OR_FREE_CREATIVE,
}

# Groq free models — tried in order per task type
_GROQ_MODELS_SIMPLE: list[str] = [
    "llama-3.3-70b-versatile",      # most capable free Groq model
    "gemma2-9b-it",                  # fast, reliable fallback
    "llama3-70b-8192",              # strong fallback
    "llama3-8b-8192",               # fast fallback
    "llama-3.2-3b-preview",         # last resort
]

_GROQ_MODELS_COMPLEX: list[str] = [
    "llama-3.3-70b-versatile",      # most capable
    "llama3-70b-8192",              # strong fallback
    "gemma2-9b-it",                  # reliable fallback
    "llama3-8b-8192",               # smaller fallback
]

_GROQ_MODEL_MAP: dict[str, list[str]] = {
    "simple":   _GROQ_MODELS_SIMPLE,
    "complex":  _GROQ_MODELS_COMPLEX,
    "creative": _GROQ_MODELS_COMPLEX,
}

# Status codes that mean "this model slot is unavailable — try the next one"
_SKIP_STATUS_CODES = {400, 401, 402, 403, 404, 429, 502, 503, 504}

# Circuit breaker — cooldown per model after skip-able errors
# Avoids retrying a model we know is down for the next N seconds
_MODEL_COOLDOWNS: dict[str, float] = {}    # model_id → expiry timestamp

_COOLDOWN_SECS = {
    400: 60,      # bad request → 1 min
    401: 300,     # invalid API key → 5 min
    402: 300,     # no free credits on this model → 5 min (try again soon)
    403: 300,     # access denied / geo-blocked → 5 min
    404: 300,     # model not found / removed from free tier → 5 min (not 24h!)
    429: 60,      # rate limit → 1 min (each model has its own rate limit)
    502: 30,      # bad gateway (transient) → 30s
    503: 120,     # unavailable → 2 min
    504: 30,      # gateway timeout (transient) → 30s
}
_CONNECTION_COOLDOWN = 30  # connection error → 30s



def get_model_status(task_type: str = "creative") -> list[dict]:
    """Return live status of every candidate model for a given task type.

    Each entry: {model, provider, status, cooldown_secs_remaining}
    status is one of: "available", "cooldown", "exhausted"
    """
    now = time.time()
    candidates = []
    try:
        candidates = _get_candidates(task_type)
    except ValueError:
        pass  # no API keys set

    seen: set[str] = set()
    result = []
    for _client, model in candidates:
        if model in seen:
            continue
        seen.add(model)
        provider = model.split("/")[0] if "/" in model else "unknown"
        cooldown_until = _MODEL_COOLDOWNS.get(model, 0)
        remaining = max(0.0, cooldown_until - now)
        if remaining > 0:
            status = "cooldown"
        else:
            status = "available"
        result.append({
            "model": model,
            "provider": provider,
            "status": status,
            "cooldown_secs_remaining": int(remaining),
        })
    return result


def get_retry_eta() -> int:
    """Return seconds until the soonest model comes out of cooldown.

    Returns 0 if any model is already available.
    """
    now = time.time()
    active = [v - now for v in _MODEL_COOLDOWNS.values() if v > now]
    return int(min(active)) if active else 0


# ---------------------------------------------------------------------------
# Internal: build an ordered candidate list for this task type
# ---------------------------------------------------------------------------

def _get_candidates(task_type: str) -> list[tuple[OpenAI, str]]:
    """
    Return an ordered list of (client, model_id) pairs to try.

    Order for chat (creative/simple): Groq first (sub-second) → OpenRouter → DeepSeek
    Order for structured (simple):    OpenRouter first → Groq → DeepSeek
    Circuit breaker skips any model in cooldown automatically.
    """
    candidates: list[tuple[OpenAI, str]] = []

    groq_key = os.environ.get("GROQ_API_KEY")
    or_key = os.environ.get("OPEN_ROUTER_API_KEY")

    if groq_key:
        groq_client = OpenAI(base_url="https://api.groq.com/openai/v1", api_key=groq_key)

    if or_key:
        or_client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=or_key)

    # OpenRouter first — more reliable across networks than Groq (which geo-blocks)
    if or_key:
        for model in _OR_FREE_MAP.get(task_type, _OR_FREE_SIMPLE):
            candidates.append((or_client, model))

    # MiniMax direct API — paid, reliable, 1M context window
    minimax_key = os.environ.get("MINIMAX_API_KEY", "").strip()
    if minimax_key:
        _mm_client = OpenAI(
            base_url="https://api.minimax.io/v1",
            api_key=minimax_key,
        )
        candidates.append((_mm_client, "MiniMax-M2.7"))

    # Groq fallback — fast when accessible, but 403s in some regions/VPNs
    if groq_key:
        for model in _GROQ_MODEL_MAP.get(task_type, _GROQ_MODELS_SIMPLE):
            candidates.append((groq_client, model))

    # DeepSeek last — often 402 (free tier depleted)
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if deepseek_key:
        _ds_client = OpenAI(base_url="https://api.deepseek.com", api_key=deepseek_key)
        candidates.append((_ds_client, "deepseek-chat"))

    if not candidates:
        raise ValueError(
            "No AI API key found. Set OPEN_ROUTER_API_KEY or GROQ_API_KEY in .env"
        )
    return candidates


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_ai_response(
    prompt: str,
    task_type: str = "simple",
    system_prompt: str | None = None,
    prompt_version: str = "unknown",
    context_blocks: list[str] | None = None,
    user_id: str = "",
) -> dict | None:
    """
    Structured JSON response — rotates through free models until one succeeds.

    Use this for catalyst scoring and structured data extraction.
    Forces json_object response format so the result is always parseable.
    Returns None if all models fail.

    Args:
        prompt_version: the PromptRegistry version_id used (e.g. "catalyst_score@1.1")
        context_blocks: list of active context block names (for decision_log)
        user_id:        Telegram user_id string (for decision_log filtering)
    """
    messages: list[dict] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    candidates = _get_candidates(task_type)
    for client, model in candidates:
        # Per-model circuit breaker — skip if in cooldown
        cooldown_until = _MODEL_COOLDOWNS.get(model, 0)
        if time.time() < cooldown_until:
            log.debug("[AI] %s in cooldown (%.0fs left) — skipping", model, cooldown_until - time.time())
            continue

        t0 = time.monotonic()
        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                response_format={"type": "json_object"},
                timeout=20.0,
            )
            content = response.choices[0].message.content
            if not content or not content.strip():
                log.warning("[AI] Model %s returned empty response", model)
                continue
            result = json.loads(content)
            if not isinstance(result, dict):
                log.warning("[AI] Model %s returned non-dict JSON", model)
                continue
            latency_ms = int((time.monotonic() - t0) * 1000)
            log.debug("[AI] get_ai_response ✓ model=%s latency=%dms", model, latency_ms)

            # Audit log — fire-and-forget, never raises
            if _decision_log is not None:
                try:
                    _decision_log.log(
                        call_type       = "structured",
                        model_used      = model,
                        prompt_version  = prompt_version,
                        context_blocks  = context_blocks or [],
                        system_prompt   = system_prompt or "",
                        response        = json.dumps(result)[:500],
                        latency_ms      = latency_ms,
                        tokens_estimate = len((system_prompt or "") + prompt) // 4,
                        user_id         = user_id,
                    )
                except Exception:
                    pass

            return result

        except APIStatusError as exc:
            if exc.status_code in _SKIP_STATUS_CODES:
                cooldown_s = _COOLDOWN_SECS.get(exc.status_code, 60)
                _MODEL_COOLDOWNS[model] = time.time() + cooldown_s
                log.warning(
                    "[AI] %s → HTTP %d — cooldown %ds, trying next model",
                    model, exc.status_code, cooldown_s,
                )
                continue
            log.error("[AI] get_ai_response fatal error (model=%s): %s", model, exc)
            return None

        except APIConnectionError as exc:
            _MODEL_COOLDOWNS[model] = time.time() + _CONNECTION_COOLDOWN
            log.warning("[AI] %s → connection error — cooldown %ds, trying next model: %s", model, _CONNECTION_COOLDOWN, exc)
            continue

        except Exception as exc:
            log.error("[AI] get_ai_response unexpected error (model=%s): %s", model, exc)
            return None

    log.error("[AI] All models exhausted for get_ai_response (task=%s)", task_type)
    return None


def get_chat_response(
    prompt: str,
    task_type: str = "creative",
    system_prompt: str | None = None,
    prompt_version: str = "unknown",
    context_blocks: list[str] | None = None,
    correction_mode: bool = False,
    regime_safe: bool = True,
    user_id: str = "",
    max_tokens: int = 400,
) -> str | None:
    """
    Plain-text response — rotates through free models until one succeeds.

    Use this for freeform Telegram chat replies.
    Does NOT enforce json_object format so the model can reply naturally.
    Returns None if all models fail.

    Args:
        prompt_version:  the PromptRegistry version_id used (e.g. "chat_system@2.3")
        context_blocks:  list of active context block names (for decision_log)
        correction_mode: True when user told us our previous answer was wrong
        regime_safe:     False when feature drift was detected (ML disabled)
        user_id:         Telegram user_id string (for decision_log filtering)
    """
    messages: list[dict] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    candidates = _get_candidates(task_type)
    for client, model in candidates:
        # Per-model circuit breaker — skip if in cooldown
        cooldown_until = _MODEL_COOLDOWNS.get(model, 0)
        if time.time() < cooldown_until:
            log.debug("[AI] %s in cooldown (%.0fs left) — skipping", model, cooldown_until - time.time())
            continue

        t0 = time.monotonic()
        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                timeout=10.0,
                # No response_format constraint — plain text allowed
            )
            result = response.choices[0].message.content
            if not result or not str(result).strip():
                log.warning("[AI] %s returned empty content — trying next model", model)
                continue
            latency_ms = int((time.monotonic() - t0) * 1000)
            log.info("[AI] get_chat_response ✓ model=%s latency=%dms", model, latency_ms)

            # Audit log — fire-and-forget, never raises
            if _decision_log is not None:
                try:
                    _decision_log.log(
                        call_type       = "chat",
                        model_used      = model,
                        prompt_version  = prompt_version,
                        context_blocks  = context_blocks or [],
                        system_prompt   = system_prompt or "",
                        response        = (result or "")[:500],
                        latency_ms      = latency_ms,
                        tokens_estimate = len((system_prompt or "") + prompt) // 4,
                        correction_mode = correction_mode,
                        regime_safe     = regime_safe,
                        user_id         = user_id,
                    )
                except Exception:
                    pass

            return result

        except APIStatusError as exc:
            if exc.status_code in _SKIP_STATUS_CODES:
                cooldown_s = _COOLDOWN_SECS.get(exc.status_code, 60)
                _MODEL_COOLDOWNS[model] = time.time() + cooldown_s
                log.warning(
                    "[AI] %s → HTTP %d — cooldown %ds, trying next model: %s",
                    model, exc.status_code, cooldown_s, str(exc)[:120],
                )
                continue
            log.error("[AI] get_chat_response fatal (model=%s) HTTP %d: %s", model, exc.status_code, exc)
            return None

        except APIConnectionError as exc:
            _MODEL_COOLDOWNS[model] = time.time() + _CONNECTION_COOLDOWN
            log.warning(
                "[AI] %s → connection error — cooldown %ds: %s",
                model, _CONNECTION_COOLDOWN, exc,
            )
            continue

        except Exception as exc:
            log.error("[AI] get_chat_response unexpected (model=%s): %s", model, exc, exc_info=True)
            continue  # try next model instead of giving up

    log.error(
        "[AI] All models exhausted for get_chat_response (task=%s). "
        "Check API keys and console for HTTP/connection errors.",
        task_type,
    )
    return None
