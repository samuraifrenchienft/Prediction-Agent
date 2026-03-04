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
from .models import AIAnalysis

def get_ai_response(prompt, task_type="simple", system_prompt=None) -> AIAnalysis | None:
    """
    Gets a structured response from the AI model.
    """
    model = MODEL_MAP.get(task_type, "meta-llama/llama-4-maverick")
    try:
        openrouter_api_key = os.environ.get("OPEN_ROUTER_API_KEY")
        if not openrouter_api_key:
            raise ValueError("OPEN_ROUTER_API_KEY not found in .env file")

        client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=openrouter_api_key,
        )

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        response = client.chat.completions.create(
            model=model,
            messages=messages,
            response_format={"type": "json_object"},
        )
        
        response_json = json.loads(response.choices[0].message.content)
        return AIAnalysis(**response_json)

    except Exception as e:
        print(f"Error getting AI response: {e}")
        return None