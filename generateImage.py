import os
from huggingface_hub import InferenceClient
from dotenv import load_dotenv
from PIL import Image

load_dotenv()

def call_image_api(prompt):
    # Access the API Key
    api_key = os.getenv("HF_TOKEN")
    client = InferenceClient(
        provider="fal-ai",
        api_key=api_key,
    )
    image = client.text_to_image(
        prompt,
        model="black-forest-labs/FLUX.1-dev",
    )
    return image
    


def generate_image(prompt):
    image = call_image_api(prompt)
    return image