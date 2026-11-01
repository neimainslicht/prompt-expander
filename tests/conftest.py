import os
import sys

import httpx
import pytest
import tenacity
from huggingface_hub.errors import HfHubHTTPError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import generateImage
import generatePrompts


def make_http_error(code, headers=None):
    request = httpx.Request("POST", "https://example.invalid")
    response = httpx.Response(code, request=request, headers=headers or {})
    return HfHubHTTPError(f"HTTP {code}", response=response)


class ScriptedProvider(generateImage.ImageProvider):
    """Provider-agnostic fake: plays back a list of results/exceptions."""

    def __init__(self, script, retryable_codes=(429, 503)):
        self.script = list(script)
        self.calls = 0
        self.retryable_codes = retryable_codes

    def generate(self, prompt):
        self.calls += 1
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        return item

    def is_retryable(self, e):
        response = getattr(e, "response", None)
        return getattr(response, "status_code", None) in self.retryable_codes


@pytest.fixture(autouse=True)
def block_real_api_calls(monkeypatch):
    """Fail loudly if a test would spend Hugging Face or Gemini credits.

    Tests that need a client behaviour override these with their own monkeypatch.
    Set RUN_LIVE_HF=1 to deliberately allow the opt-in live test.
    """
    if os.getenv("RUN_LIVE_HF") == "1":
        return

    def blocked(*args, **kwargs):
        raise AssertionError("Real API call attempted in a test; mock it.")

    monkeypatch.setattr(generateImage.InferenceClient, "text_to_image", blocked)
    monkeypatch.setattr(generatePrompts.genai, "Client", blocked)


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    """Skip real backoff sleeps in every test."""
    monkeypatch.setattr(generateImage, "_wait_strategy", lambda provider: tenacity.wait_none())


@pytest.fixture(autouse=True)
def default_settings(monkeypatch):
    """Run every test with the documented default limits, whatever the developer's .env says."""
    for name, value in config.DEFAULTS.items():
        monkeypatch.setenv(name, value)
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


@pytest.fixture
def settings(monkeypatch):
    """Override settings inside a test: settings(MAX_IMAGES_PER_REQUEST="3")."""
    def apply(**overrides):
        for name, value in overrides.items():
            monkeypatch.setenv(name, str(value))
        config.get_settings.cache_clear()
    return apply
