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

# Access the API Key
api_key = os.getenv("API_KEY")
client = genai.Client(api_key=api_key)

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
    "timestamp": "{datetime.now().strftime("%Y%m%d_%H%M%S")}",
    "concept": "...",
    "prompts": ["...","...","...",...]
    }}

    Concept: {prompt_idea}
    """

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt, 
        config={"response_mime_type": "application/json"} 
    )
    return response

def generatePrompt(prompt_idea):
    if not prompt_idea or prompt_idea.isspace():
        print("Empty prompt entered.")
        return None
    try:
        response = call_gemini(prompt_idea=prompt_idea)
        return response.text

    except APIError as e:
        if e.code == 429 and "per day" in str(e).lower():
            print("Daily API quota exceeded. Please wait until tomorrow or upgrade your plan.")

        else:
            print(f"Gemini API failed with status {e.code}: {e}")
        return None
    except Exception as e:
        print(f"Failed to generate prompts due to an unexpected error: {e}")
        return None
