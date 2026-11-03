import config
import usage
import validation
from errors import AppError
from logging_config import redact
from google import genai
from google.genai import types
from google.genai.errors import APIError
from tenacity import (
    retry, stop_after_attempt, retry_if_exception, wait_random_exponential
)
from datetime import datetime

GEMINI_MODEL = "gemini-2.5-flash"

_client = None


def get_client():
    """Create the Gemini client on first use so importing this module never needs API_KEY."""
    global _client
    if _client is None:
        # The SDK has no timeout by default. A bounded call keeps a request's activity timestamps
        # moving, so a slow call is not mistaken for a crashed request (see config.MIN_STALE_REQUEST_SECONDS).
        _client = genai.Client(
            api_key=config.require_secret("API_KEY"),
            http_options=types.HttpOptions(timeout=config.GEMINI_TIMEOUT_SECONDS * 1000))  # milliseconds
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
    wait=wait_random_exponential(multiplier=0.5, min=1, max=config.RETRY_WAIT_MAX_SECONDS),
    retry=retry_if_exception(should_retry_gemini),
    reraise=True
)

def call_gemini(prompt_idea, num_prompts=None, observer=None):
    usage.require_observer(observer)  # internal: reached only through services.expand_idea
    num_prompts = num_prompts or config.get_settings().max_prompts_per_request
    prompt = f"""
    Given the following concept, create {num_prompts} detailed image generation prompts. Each prompt should include some combination of:

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

    client = get_client()
    # the decorator above re-runs this function for every retry, so each real call is reported here
    attempt_id = observer.started()
    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config={"response_mime_type": "application/json"}
        )
    except Exception as e:
        observer.finished(attempt_id, error=e)
        raise
    meta = getattr(response, "usage_metadata", None)
    observer.finished(attempt_id,
                      input_tokens=getattr(meta, "prompt_token_count", None),
                      output_tokens=getattr(meta, "candidates_token_count", None))
    return response

def generate_prompt_text(prompt_idea, num_prompts=None, observer=None):
    """Return (json_text, error_message). Exactly one of the two is None.

    The request is validated first, so nothing invalid or disabled reaches Gemini.
    """
    try:
        validation.ensure_generation_enabled()
        validation.validate_concept(prompt_idea)
        num_prompts = validation.validate_prompt_count(num_prompts)
        usage.require_observer(observer)
    except (AppError, config.ConfigError) as e:
        return None, getattr(e, "message", None) or str(e)
    try:
        response = call_gemini(prompt_idea=prompt_idea, num_prompts=num_prompts, observer=observer)
        return response.text, None
    except AppError as e:  # a limit reached before a retry
        return None, e.message
    except APIError as e:
        if e.code == 429 and "per day" in str(e).lower():
            return None, "Daily API quota exceeded. Please wait until tomorrow or upgrade your plan."
        return None, redact(f"Gemini API failed with status {e.code}: {e}")
    except Exception as e:
        return None, redact(f"Failed to generate prompts due to an unexpected error: {e}")


def generatePrompt(prompt_idea):
    text, error = generate_prompt_text(prompt_idea)
    if error:
        print(error)
    return text
