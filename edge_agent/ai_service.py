import json
import logging
import os

from dotenv import load_dotenv
from openai import APIConnectionError, APIStatusError, OpenAI

load_dotenv()

logger = logging.getLogger(__name__)

MODEL_MAP = {
    "simple": "meta-llama/llama-4-maverick",
    "complex": "meta-llama/llama-4-maverick",
    "creative": "meta-llama/llama-4-maverick",
}

_openrouter_api_key = os.environ.get("OPEN_ROUTER_API_KEY")
_client: OpenAI | None = None

if _openrouter_api_key:
    _client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=_openrouter_api_key,
    )
else:
    logger.warning("OPEN_ROUTER_API_KEY not set — AI service will return None for all requests.")


def get_ai_response(prompt: str, task_type: str = "simple", system_prompt: str | None = None) -> dict | None:
    """
    Gets a structured JSON response from the AI model.

    Returns a dict on success, or None if the call fails or the client is not configured.
    """
    if _client is None:
        logger.error("AI client is not initialized. Set OPEN_ROUTER_API_KEY in your .env file.")
        return None

    model = MODEL_MAP.get(task_type, "meta-llama/llama-4-maverick")

    messages: list[dict] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    try:
        response = _client.chat.completions.create(
            model=model,
            messages=messages,
            response_format={"type": "json_object"},
        )
        return json.loads(response.choices[0].message.content)

    except json.JSONDecodeError as exc:
        logger.error("AI returned non-JSON response: %s", exc)
        return None
    except APIConnectionError as exc:
        logger.error("Could not reach OpenRouter API: %s", exc)
        return None
    except APIStatusError as exc:
        logger.error("OpenRouter API error (status %s): %s", exc.status_code, exc.message)
        return None
    except ValueError as exc:
        logger.error("Configuration error: %s", exc)
        return None
