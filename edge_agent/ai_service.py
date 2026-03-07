import json
import os

from dotenv import find_dotenv, load_dotenv
from openai import OpenAI

from .models import AIAnalysis

# Search current dir and all parent dirs for .env
load_dotenv(find_dotenv(usecwd=True) or find_dotenv())

# OpenRouter free models (primary) — https://openrouter.ai/models?q=free
_OR_SIMPLE  = "stepfun/step-3.5-flash:free"
_OR_COMPLEX = "arcee-ai/trinity-large-preview:free"

MODEL_MAP = {
    "simple":   _OR_SIMPLE,
    "complex":  _OR_COMPLEX,
    "creative": _OR_COMPLEX,
}

# Groq fallback models (if GROQ_API_KEY works on your network)
_GROQ_MODEL_MAP = {
    "simple":   "llama-3.1-8b-instant",
    "complex":  "llama-3.3-70b-versatile",
    "creative": "llama-3.3-70b-versatile",
}


def _get_client_and_model(task_type: str) -> tuple[OpenAI, str]:
    openrouter_key = os.environ.get("OPEN_ROUTER_API_KEY")
    if openrouter_key:
        client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=openrouter_key)
        return client, MODEL_MAP.get(task_type, _OR_SIMPLE)

    groq_key = os.environ.get("GROQ_API_KEY")
    if groq_key:
        client = OpenAI(base_url="https://api.groq.com/openai/v1", api_key=groq_key)
        return client, _GROQ_MODEL_MAP.get(task_type, "llama-3.1-8b-instant")

    raise ValueError("No AI API key found. Set OPEN_ROUTER_API_KEY or GROQ_API_KEY in .env")


def get_ai_response(prompt: str, task_type: str = "simple", system_prompt: str | None = None) -> dict | None:
    """
    Gets a structured JSON response from the AI model. Returns raw dict.
    Use this for catalyst scoring and structured data — NOT for freeform chat.
    Forces json_object response format so the result is always parseable.
    """
    try:
        client, model = _get_client_and_model(task_type)

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        response = client.chat.completions.create(
            model=model,
            messages=messages,
            response_format={"type": "json_object"},
        )

        return json.loads(response.choices[0].message.content)

    except Exception as e:
        print(f"Error getting AI response: {e}")
        return None


def get_chat_response(prompt: str, task_type: str = "creative", system_prompt: str | None = None) -> str | None:
    """
    Gets a plain-text response from the AI model. Returns the raw string.
    Use this for freeform Telegram chat — NOT for structured data parsing.
    Does NOT enforce json_object format so the model can reply naturally.
    """
    try:
        client, model = _get_client_and_model(task_type)

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        response = client.chat.completions.create(
            model=model,
            messages=messages,
            # No response_format constraint — plain text allowed
        )

        return response.choices[0].message.content

    except Exception as e:
        print(f"Error getting chat response: {e}")
        return None
