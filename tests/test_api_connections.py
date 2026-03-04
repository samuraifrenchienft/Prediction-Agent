
import os
from dotenv import load_dotenv
from openai import OpenAI

# Load environment variables from .env file
load_dotenv()

def test_openrouter_connection():
    """
    Tests the connection to the OpenRouter API with a variety of models.
    """
    print("Testing OpenRouter API connection...")

    models_to_test = [
        "meta-llama/llama-4-maverick",
    ]

    for model in models_to_test:
        try:
            openrouter_api_key = os.environ.get("OPEN_ROUTER_API_KEY")
            if not openrouter_api_key:
                raise ValueError("OPEN_ROUTER_API_KEY not found in .env file")

            client = OpenAI(
                base_url="https://openrouter.ai/api/v1",
                api_key=openrouter_api_key,
            )

            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "user", "content": "What is the capital of France?"}
                ]
            )
            print(f"Successfully connected to {model} on OpenRouter!")
            # print(response)
        except Exception as e:
            print(f"Error connecting to {model} on OpenRouter: {e}")


if __name__ == "__main__":
    test_openrouter_connection()
