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

def get_ai_response(prompt, task_type="simple", system_prompt=None):
    """
    Gets a response from the AI model.
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
            messages=messages
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"Error: {e}"

def test_simple_prompt():
    """
    Tests a simple prompt and response.
    """
    prompt = "What is the capital of France?"
    response = get_ai_response(prompt, task_type="simple")
    print(f"Complex prompt test response: {response}")
    assert "paris" in response.lower()
    print("test_simple_prompt passed!")

def test_system_prompt():
    """
    Tests the AI's ability to follow a system prompt.
    """
    system_prompt = "You are a helpful assistant that always responds in pirate speak."
    prompt = "What is the capital of France?"
    response = get_ai_response(prompt, task_type="complex", system_prompt=system_prompt)
    print(f"System prompt test response: {response}")
    assert "ahoy" in response.lower() or "paris" in response.lower()
    print("test_system_prompt passed!")

def test_complex_prompt():
    """
    Tests the AI's ability to handle a complex prompt.
    """
    prompt = "What are the top 3 benefits of using a large language model for a software engineer?"
    response = get_ai_response(prompt, task_type="complex")
    print(f"Complex prompt test response: {response}")
    assert "code completion" in response.lower() or "code generation" in response.lower()
    assert "debugging" in response.lower()
    assert "documentation" in response.lower()
    print("test_complex_prompt passed!")

def test_creative_prompt():
    """
    Tests the AI's ability to generate creative content.
    """
    prompt = "Write a haiku about a robot learning to love."
    response = get_ai_response(prompt, task_type="creative")
    # A simple check to see if the response is in the right format
    assert len(response.split('\n')) == 3
    print("test_creative_prompt passed!")

if __name__ == "__main__":
    test_simple_prompt()
    test_system_prompt()
    test_complex_prompt()
    test_creative_prompt()