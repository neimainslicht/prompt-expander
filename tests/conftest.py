import os
import sys

import httpx
import pytest
import tenacity
from huggingface_hub.errors import HfHubHTTPError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generateImage


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
def no_wait(monkeypatch):
    """Skip real backoff sleeps in every test."""
    monkeypatch.setattr(generateImage, "_wait_strategy", lambda provider: tenacity.wait_none())
