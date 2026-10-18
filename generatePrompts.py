import os
from dotenv import load_dotenv
from google import genai
from google.genai.errors import APIError
from tenacity import (
    retry, stop_after_attempt, retry_if_exception, wait_random_exponential
)
from datetime import datetime

# Load variables from the .env file into the system environment
load_dotenv()

_client = None


def get_client():
    """Create the Gemini client on first use so importing this module never needs API_KEY."""
    global _client
    if _client is None:
        api_key = os.getenv("API_KEY")
        if not api_key:
            raise RuntimeError("API_KEY is not set.")
        _client = genai.Client(api_key=api_key)
    return _client

def should_retry_gemini(e: Exception) -> bool:
    if not isinstance(e, APIError):
        return False
    
    code = getattr(e, 'code', None)
    # Retry on 503 (Overloaded) or temporary 429 (Minute limits)
    if code == 503:
        print("Gemini API overloaded. Retrying shortly...")
        return True
    if code == 429 and "per day" not in str(e).lower():
        print("Temporary Rate Limit hit (429 RPM/TPM). Backing off and retrying...")
        return True
        
    return False

@retry(
    stop=stop_after_attempt(5),
    wait=wait_random_exponential(multiplier=0.5, min=1, max=60),
    retry=retry_if_exception(should_retry_gemini),
    reraise=True
)

def call_gemini(prompt_idea):
    prompt = f"""
    Given the following concept, create 10 detailed image generation prompts. Each prompt should include some combination of:

    subject
    composition
    lighting
    camera angle
    mood
    style

    Output should be in JSON format with this structure:
    {{
    "prompts": [
    {{
      "text": "...",
    }},
    {{
      "text": "...",
    }}
    ...
    ]
    }}

    Concept: {prompt_idea}
    """

    response = get_client().models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt, 
        config={"response_mime_type": "application/json"} 
    )
    return response

def generate_prompt_text(prompt_idea):
    """Return (json_text, error_message). Exactly one of the two is None."""
    if not prompt_idea or prompt_idea.isspace():
        return None, "Empty prompt entered."
    try:
        response = call_gemini(prompt_idea=prompt_idea)
        return response.text, None
    except APIError as e:
        if e.code == 429 and "per day" in str(e).lower():
            return None, "Daily API quota exceeded. Please wait until tomorrow or upgrade your plan."
        return None, f"Gemini API failed with status {e.code}: {e}"
    except Exception as e:
        return None, f"Failed to generate prompts due to an unexpected error: {e}"


def generatePrompt(prompt_idea):
    text, error = generate_prompt_text(prompt_idea)
    if error:
        print(error)
    return text
