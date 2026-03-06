import json
import os

from dotenv import find_dotenv, load_dotenv
from openai import OpenAI

from .models import AIAnalysis

# Search current dir and all parent dirs for .env
load_dotenv(find_dotenv(usecwd=True) or find_dotenv())

# Groq free tier — https://console.groq.com/keys
# Models: llama-3.3-70b-versatile, llama-3.1-8b-instant, gemma2-9b-it
MODEL_MAP = {
    "simple": "llama-3.1-8b-instant",
    "complex": "llama-3.3-70b-versatile",
    "creative": "llama-3.3-70b-versatile",
}


def _get_client() -> OpenAI:
    groq_key = os.environ.get("GROQ_API_KEY")
    if groq_key:
        return OpenAI(base_url="https://api.groq.com/openai/v1", api_key=groq_key)

    # Fallback: OpenRouter if Groq key not set
    openrouter_key = os.environ.get("OPEN_ROUTER_API_KEY")
    if openrouter_key:
        return OpenAI(base_url="https://openrouter.ai/api/v1", api_key=openrouter_key)

    raise ValueError("No AI API key found. Set GROQ_API_KEY in .env (free at console.groq.com)")


def get_ai_response(prompt: str, task_type: str = "simple", system_prompt: str | None = None) -> dict | None:
    """Gets a structured JSON response from the AI model. Returns raw dict."""
    model = MODEL_MAP.get(task_type, MODEL_MAP["simple"])
    try:
        client = _get_client()

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
