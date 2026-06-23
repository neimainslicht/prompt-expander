import os
from dotenv import load_dotenv
from google import genai
from google.genai.errors import APIError
from tenacity import (
    retry, stop_after_attempt, retry_if_exception_type
)
from datetime import datetime

# Load variables from the .env file into the system environment
load_dotenv()

# Access the API Key
api_key = os.getenv("API_KEY")
client = genai.Client(api_key=api_key)

@retry(stop=stop_after_attempt(4),
       retry=retry_if_exception_type(APIError),
       reraise=True
)
def call_gemini(prompt_idea):
    try:
        response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=f"""
                Given the following concept, create 10 detailed image generation prompts. Each prompt should include some combination of:

                subject
                composition
                lighting
                camera angle
                mood
                style

                Output should be in JSON format with this structure:
                {{
                "timestamp": "{datetime.now().strftime("%Y%m%d_%H%M%S")}"
                "concept": "..."
                "prompts": ["...","...","...",...]
                }}

                Concept: {prompt_idea}
                """
            )
        return response
    except APIError as e:
        if e.code == 503:
            print("Gemini API overloaded. Retrying shortly...")
            raise e
        raise e

def generatePrompt(prompt_idea):
    while True:
        if not prompt_idea or prompt_idea.isspace():
                print("Empty prompt entered.")
                return None
        try:
             response = call_gemini(prompt_idea=prompt_idea)
             return response.text

        except ValueError:
            print("Invalid input. Please enter an English sentence or phrase.")
            return None
        except Exception as e:
            print(f"Failed to generate prompts after multiple retries. Error: {e}")
            return None
