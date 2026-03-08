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
"""
from __future__ import annotations

import json
import logging
import os

from dotenv import find_dotenv, load_dotenv
from openai import OpenAI, APIStatusError, APIConnectionError

from .models import AIAnalysis  # noqa: F401 — re-exported for other modules

# Search current dir and all parent dirs for .env
load_dotenv(find_dotenv(usecwd=True) or find_dotenv())

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Free model rotation lists — tried left-to-right until one succeeds.
# 402 = out of free credits on that model → skip
# 429 = rate limit on that model → skip
# 503 = model unavailable → skip
# ---------------------------------------------------------------------------

_OR_FREE_SIMPLE: list[str] = [
    "stepfun/step-3.5-flash:free",
    "qwen/qwen2.5-7b-instruct:free",
    "google/gemma-3-4b-it:free",
    "meta-llama/llama-3.2-3b-instruct:free",
    "microsoft/phi-3-mini-128k-instruct:free",
]

_OR_FREE_COMPLEX: list[str] = [
    "arcee-ai/trinity-large-preview:free",
    "deepseek/deepseek-chat-v3-0324:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "qwen/qwen2.5-72b-instruct:free",
    "mistralai/mistral-7b-instruct:free",
]

_OR_FREE_CREATIVE: list[str] = [
    "arcee-ai/trinity-large-preview:free",
    "deepseek/deepseek-chat-v3-0324:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "qwen/qwen2.5-72b-instruct:free",
    "mistralai/mistral-7b-instruct:free",
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
) -> dict | None:
    """
    Structured JSON response — rotates through free models until one succeeds.

    Use this for catalyst scoring and structured data extraction.
    Forces json_object response format so the result is always parseable.
    Returns None if all models fail.
    """
    messages: list[dict] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    candidates = _get_candidates(task_type)
    for client, model in candidates:
        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                response_format={"type": "json_object"},
            )
            result = json.loads(response.choices[0].message.content)
            log.debug("[AI] get_ai_response ✓ model=%s", model)
            return result

        except APIStatusError as exc:
            if exc.status_code in _SKIP_STATUS_CODES:
                log.warning(
                    "[AI] %s → HTTP %d — trying next model",
                    model, exc.status_code,
                )
                continue
            log.error("[AI] get_ai_response fatal error (model=%s): %s", model, exc)
            return None

        except APIConnectionError as exc:
            log.warning("[AI] %s → connection error — trying next model: %s", model, exc)
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
) -> str | None:
    """
    Plain-text response — rotates through free models until one succeeds.

    Use this for freeform Telegram chat replies.
    Does NOT enforce json_object format so the model can reply naturally.
    Returns None if all models fail.
    """
    messages: list[dict] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    candidates = _get_candidates(task_type)
    for client, model in candidates:
        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                # No response_format constraint — plain text allowed
            )
            result = response.choices[0].message.content
            log.debug("[AI] get_chat_response ✓ model=%s", model)
            return result

        except APIStatusError as exc:
            if exc.status_code in _SKIP_STATUS_CODES:
                log.warning(
                    "[AI] %s → HTTP %d — trying next model",
                    model, exc.status_code,
                )
                continue
            log.error("[AI] get_chat_response fatal error (model=%s): %s", model, exc)
            return None

        except APIConnectionError as exc:
            log.warning("[AI] %s → connection error — trying next model: %s", model, exc)
            continue

        except Exception as exc:
            log.error("[AI] get_chat_response unexpected error (model=%s): %s", model, exc)
            return None

    log.error("[AI] All models exhausted for get_chat_response (task=%s)", task_type)
    return None
