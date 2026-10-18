import os
from abc import ABC, abstractmethod

import httpx
from dotenv import load_dotenv
from huggingface_hub import InferenceClient
from huggingface_hub.errors import InferenceTimeoutError
from tenacity import Retrying, stop_after_attempt, retry_if_exception, wait_random_exponential

load_dotenv()

MAX_ATTEMPTS = 5
WAIT_MULTIPLIER = 0.5
WAIT_MIN = 1
WAIT_MAX = 60


class ImageProvider(ABC):
    """Interface every image API provider implements."""

    @abstractmethod
    def generate(self, prompt):
        """Return a PIL.Image for the prompt. Raise on failure."""

    @abstractmethod
    def is_retryable(self, e: Exception) -> bool:
        """True if the error is transient and the call should be retried."""

    def retry_after(self, e: Exception):
        """Seconds the provider asked us to wait, or None."""
        return None

    def describe_error(self, e: Exception) -> str:
        return str(e)


class HuggingFaceImageProvider(ImageProvider):
    RETRYABLE_STATUS = {429, 500, 502, 503, 504}

    def __init__(self, provider="fal-ai", model="black-forest-labs/FLUX.1-dev", timeout=120):
        self.provider = provider
        self.model = model
        # InferenceClient defaults to timeout=None (waits forever), so set one explicitly
        self.timeout = timeout
        self._client = None

    @property
    def client(self):
        if self._client is None:
            api_key = os.getenv("HF_TOKEN")
            if not api_key:
                raise RuntimeError("HF_TOKEN is not set.")
            self._client = InferenceClient(
                provider=self.provider,
                api_key=api_key,
                timeout=self.timeout,
            )
        return self._client

    def generate(self, prompt):
        return self.client.text_to_image(prompt, model=self.model)

    @staticmethod
    def _status(e):
        response = getattr(e, "response", None)
        return getattr(response, "status_code", None)

    def is_retryable(self, e: Exception) -> bool:
        if isinstance(e, (InferenceTimeoutError, httpx.TransportError)):
            print("Image API timed out or connection failed. Retrying shortly...")
            return True
        code = self._status(e)
        if code in self.RETRYABLE_STATUS:
            print(f"Image API returned {code}. Backing off and retrying...")
            return True
        return False

    def retry_after(self, e: Exception):
        response = getattr(e, "response", None)
        value = getattr(response, "headers", {}).get("retry-after") if response is not None else None
        try:
            return float(value) if value is not None else None
        except ValueError:
            return None

    def describe_error(self, e: Exception) -> str:
        code = self._status(e)
        if code == 401:
            return "Authentication failed. Check HF_TOKEN."
        if code == 402:
            return "Inference credits exhausted for this account."
        if code == 403:
            return "HF_TOKEN lacks permission to call Inference Providers."
        if code is not None:
            return f"Image API failed with status {code}: {e}"
        return str(e)


PROVIDERS = {
    "huggingface": HuggingFaceImageProvider,
}


def get_provider(name=None) -> ImageProvider:
    name = (name or os.getenv("IMAGE_PROVIDER") or "huggingface").lower()
    if name not in PROVIDERS:
        raise ValueError(f"Unknown image provider '{name}'. Available: {', '.join(PROVIDERS)}")
    return PROVIDERS[name]()


def _wait_strategy(provider):
    exponential = wait_random_exponential(multiplier=WAIT_MULTIPLIER, min=WAIT_MIN, max=WAIT_MAX)

    def wait(retry_state):
        delay = exponential(retry_state)
        asked = provider.retry_after(retry_state.outcome.exception())
        if asked:
            delay = max(delay, min(asked, WAIT_MAX))
        return delay

    return wait


def call_image_api(provider: ImageProvider, prompt):
    retrying = Retrying(
        stop=stop_after_attempt(MAX_ATTEMPTS),
        wait=_wait_strategy(provider),
        retry=retry_if_exception(provider.is_retryable),
        reraise=True,
    )
    return retrying(provider.generate, prompt)


def generate_image_with_error(prompt, provider=None):
    """Return (image, error_message). Exactly one of the two is None."""
    try:
        if not isinstance(provider, ImageProvider):
            provider = get_provider(provider)
        return call_image_api(provider, prompt), None
    except Exception as e:
        message = provider.describe_error(e) if isinstance(provider, ImageProvider) else str(e)
        return None, message


def generate_image(prompt, provider=None):
    image, error = generate_image_with_error(prompt, provider)
    if error:
        print(f"Failed to generate image: {error}")
    return image
