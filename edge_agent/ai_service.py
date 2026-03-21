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

from dotenv import find_dotenv, load_dotenv
from openai import OpenAI, APIStatusError, APIConnectionError

from .models import AIAnalysis  # noqa: F401 — re-exported for other modules

# Search current dir and all parent dirs for .env
load_dotenv(find_dotenv(usecwd=True) or find_dotenv())

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

# Updated 2026-03-15 — pruned retired models, added current free-tier options.
# Check https://openrouter.ai/models?q=free for latest availability.
_OR_FREE_SIMPLE: list[str] = [
    "stepfun/step-3.5-flash:free",              # 256k ctx — fast, reliable
    "nvidia/nemotron-3-nano-30b-a3b:free",       # 256k ctx — small but capable
    "liquid/lfm-2.5-1.2b-instruct:free",         # 32k ctx — ultra-fast fallback
    "arcee-ai/trinity-mini:free",                 # 131k ctx — lightweight
]

_OR_FREE_COMPLEX: list[str] = [
    "nvidia/nemotron-3-super-120b-a12b:free",    # 262k ctx — strongest free model
    "arcee-ai/trinity-large-preview:free",        # 131k ctx — solid all-rounder
    "stepfun/step-3.5-flash:free",                # 256k ctx — fast fallback
    "nvidia/nemotron-3-nano-30b-a3b:free",        # 256k ctx — last resort
]

_OR_FREE_CREATIVE: list[str] = [
    "nvidia/nemotron-3-super-120b-a12b:free",    # 262k ctx — strongest free model
    "arcee-ai/trinity-large-preview:free",        # 131k ctx — solid all-rounder
    "stepfun/step-3.5-flash:free",                # 256k ctx — fast fallback
    "nvidia/nemotron-3-nano-30b-a3b:free",        # 256k ctx — last resort
]

_OR_FREE_MAP: dict[str, list[str]] = {
    "simple":   _OR_FREE_SIMPLE,
    "complex":  _OR_FREE_COMPLEX,
    "creative": _OR_FREE_CREATIVE,
}

# Groq free fallback models
_GROQ_MODEL_MAP: dict[str, str] = {
    "simple":   "llama-3.1-8b-instant",
    "complex":  "llama-3.3-70b-versatile",
    "creative": "llama-3.3-70b-versatile",
}

# Status codes that mean "this model slot is unavailable — try the next one"
_SKIP_STATUS_CODES = {402, 429, 503}

# Circuit breaker — cooldown per model after skip-able errors
# Avoids retrying a model we know is down for the next N seconds
_MODEL_COOLDOWNS: dict[str, float] = {}   # model_id → expiry timestamp
_COOLDOWN_SECS = {
    402: 3600,   # out of credits → 1 hour cooldown
    429: 120,    # rate limit → 2 min cooldown
    503: 300,    # unavailable → 5 min cooldown
}
_CONNECTION_COOLDOWN = 60  # connection error → 1 min cooldown


# ---------------------------------------------------------------------------
# Internal: build an ordered candidate list for this task type
# ---------------------------------------------------------------------------

def _get_candidates(task_type: str) -> list[tuple[OpenAI, str]]:
    """
    Return an ordered list of (client, model_id) pairs to try.

    OpenRouter free models come first (multiple fallbacks within the free tier),
    followed by Groq as a final-resort fallback.
    """
    candidates: list[tuple[OpenAI, str]] = []

    or_key = os.environ.get("OPEN_ROUTER_API_KEY")
    if or_key:
        or_client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=or_key)
        for model in _OR_FREE_MAP.get(task_type, _OR_FREE_SIMPLE):
            candidates.append((or_client, model))

    groq_key = os.environ.get("GROQ_API_KEY")
    if groq_key:
        groq_client = OpenAI(
            base_url="https://api.groq.com/openai/v1", api_key=groq_key
        )
        candidates.append(
            (groq_client, _GROQ_MODEL_MAP.get(task_type, "llama-3.1-8b-instant"))
        )

    # DeepSeek — OpenAI-compatible, ultra-cheap overflow ($0.028/1M input cache hit)
    # Sign up: https://platform.deepseek.com
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
        # Circuit breaker: skip models in cooldown
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
                timeout=90.0,
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
                _MODEL_COOLDOWNS[model] = time.time() + _COOLDOWN_SECS.get(exc.status_code, 60)
                log.warning(
                    "[AI] %s → HTTP %d — cooldown %ds, trying next model",
                    model, exc.status_code, _COOLDOWN_SECS.get(exc.status_code, 60),
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
        # Circuit breaker: skip models in cooldown
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
                timeout=90.0,
                # No response_format constraint — plain text allowed
            )
            result = response.choices[0].message.content
            latency_ms = int((time.monotonic() - t0) * 1000)
            log.debug("[AI] get_chat_response ✓ model=%s latency=%dms", model, latency_ms)

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
                _MODEL_COOLDOWNS[model] = time.time() + _COOLDOWN_SECS.get(exc.status_code, 60)
                log.warning(
                    "[AI] %s → HTTP %d — cooldown %ds, trying next model",
                    model, exc.status_code, _COOLDOWN_SECS.get(exc.status_code, 60),
                )
                continue
            log.error("[AI] get_chat_response fatal error (model=%s): %s", model, exc)
            return None

        except APIConnectionError as exc:
            _MODEL_COOLDOWNS[model] = time.time() + _CONNECTION_COOLDOWN
            log.warning("[AI] %s → connection error — cooldown %ds, trying next model: %s", model, _CONNECTION_COOLDOWN, exc)
            continue

        except Exception as exc:
            log.error("[AI] get_chat_response unexpected error (model=%s): %s", model, exc)
            return None

    log.error("[AI] All models exhausted for get_chat_response (task=%s)", task_type)
    return None
