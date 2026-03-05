import os
from dotenv import load_dotenv
from openai import OpenAI

# Load environment variables from .env file
load_dotenv()

MODEL_MAP = {
    "simple": "meta-llama/llama-4-maverick",
    "complex": "meta-llama/llama-4-maverick",
    "creative": "meta-llama/llama-4-maverick",
}

import json

def _make_client() -> OpenAI:
    api_key = os.environ.get("OPEN_ROUTER_API_KEY")
    if not api_key:
        raise ValueError("OPEN_ROUTER_API_KEY not found in .env file")
    return OpenAI(base_url="https://openrouter.ai/api/v1", api_key=api_key)


def get_ai_response(prompt, task_type="simple", system_prompt=None) -> dict | None:
    """Gets a structured JSON response from the AI model (used by probability_node)."""
    model = MODEL_MAP.get(task_type, "meta-llama/llama-4-maverick")
    try:
        client = _make_client()
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


def get_ai_chat_response(
    user_message: str,
    context: str = "",
    conversation_history: list[dict] | None = None,
) -> str:
    """Gets a plain-text conversational response for the Telegram chat interface.

    Unlike get_ai_response(), this does NOT enforce JSON output — it returns
    natural language so users can ask follow-up questions about markets,
    signals, and recommendations.

    Args:
        user_message: The user's question or message.
        context: Optional market context (recent scan results, tracking list, etc.)
        conversation_history: Prior messages in [{role, content}] format for multi-turn chat.
    """
    model = MODEL_MAP.get("complex", "meta-llama/llama-4-maverick")
    try:
        client = _make_client()

        system_prompt = (
            "You are EDGE, an AI prediction market analyst agent. "
            "You help users understand market signals, explain trading opportunities, "
            "and answer questions about prediction markets (Kalshi, Polymarket, Jupiter). "
            "Be concise, direct, and specific. Avoid generic advice. "
            "When referencing probabilities, use percentages. "
            "If you don't have enough data to answer, say so clearly."
        )

        messages: list[dict] = [{"role": "system", "content": system_prompt}]

        if context:
            messages.append({
                "role": "system",
                "content": f"Current market context:\n{context}",
            })

        if conversation_history:
            messages.extend(conversation_history)

        messages.append({"role": "user", "content": user_message})

        response = client.chat.completions.create(
            model=model,
            messages=messages,
        )
        return response.choices[0].message.content.strip()

    except Exception as e:
        print(f"Error getting AI chat response: {e}")
        return f"Sorry, I couldn't reach the AI right now. Error: {e}"