import os
from dotenv import load_dotenv
from google import genai

# Load variables from the .env file into the system environment
load_dotenv()

# Access the API Key
api_key = os.getenv("API_KEY")

prompt_idea = input("Enter a short idea for an image, and the Prompt Expander will transform it into multiple detailed text to image prompts.")

while True:
    try:
        if prompt_idea.isspace():
            print("Empty prompt entered.")
            continue

        client = genai.Client(api_key=api_key)
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
            "concept": "..."
            "prompts": ["...","...","...",...]
            }}

            Concept: {prompt_idea}
            """
        )
        break
    except ValueError:
        print("Invalid input. Please enter an English sentence or phrase.")



print(response.text)