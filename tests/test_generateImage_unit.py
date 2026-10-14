import httpx
import pytest
import tenacity
from huggingface_hub.errors import InferenceTimeoutError

import generateImage as g
from conftest import ScriptedProvider, make_http_error


# ---- HuggingFaceImageProvider.is_retryable ----

@pytest.mark.parametrize("code", [429, 500, 502, 503, 504])
def test_retryable_status_codes(code):
    assert g.HuggingFaceImageProvider().is_retryable(make_http_error(code))


@pytest.mark.parametrize("code", [400, 401, 402, 403, 404, 422])
def test_non_retryable_status_codes(code):
    assert not g.HuggingFaceImageProvider().is_retryable(make_http_error(code))


@pytest.mark.parametrize("exc", [
    InferenceTimeoutError("timeout"),
    httpx.ConnectError("boom"),
    httpx.ReadTimeout("slow"),
])
def test_transport_and_timeout_errors_retryable(exc):
    assert g.HuggingFaceImageProvider().is_retryable(exc)


@pytest.mark.parametrize("exc", [ValueError("x"), RuntimeError("x"), KeyError("x")])
def test_unrelated_exceptions_not_retryable(exc):
    assert not g.HuggingFaceImageProvider().is_retryable(exc)


# ---- retry_after / describe_error ----

def test_retry_after_parsed():
    p = g.HuggingFaceImageProvider()
    assert p.retry_after(make_http_error(429, {"retry-after": "7"})) == 7.0


def test_retry_after_missing_or_invalid():
    p = g.HuggingFaceImageProvider()
    assert p.retry_after(make_http_error(429)) is None
    assert p.retry_after(make_http_error(429, {"retry-after": "Wed, 21 Oct"})) is None
    assert p.retry_after(ValueError("no response")) is None


@pytest.mark.parametrize("code,fragment", [
    (401, "HF_TOKEN"), (402, "credits"), (403, "permission"), (418, "418"),
])
def test_describe_error(code, fragment):
    assert fragment in g.HuggingFaceImageProvider().describe_error(make_http_error(code))


def test_describe_error_without_status():
    assert g.HuggingFaceImageProvider().describe_error(ValueError("plain")) == "plain"


# ---- wait strategy ----

class _State:
    def __init__(self, exc, attempt=1):
        self.attempt_number = attempt
        self.outcome = type("O", (), {"exception": lambda s: exc})()
        self.idle_for = 0


def test_wait_honors_retry_after(monkeypatch):
    monkeypatch.undo()  # restore the real _wait_strategy
    p = g.HuggingFaceImageProvider()
    wait = g._wait_strategy(p)
    assert wait(_State(make_http_error(429, {"retry-after": "30"}))) >= 30


def test_wait_caps_retry_after(monkeypatch):
    monkeypatch.undo()
    wait = g._wait_strategy(g.HuggingFaceImageProvider())
    assert wait(_State(make_http_error(429, {"retry-after": "9999"}))) <= g.WAIT_MAX


def test_wait_without_retry_after_within_bounds(monkeypatch):
    monkeypatch.undo()
    wait = g._wait_strategy(g.HuggingFaceImageProvider())
    assert 0 <= wait(_State(make_http_error(503))) <= g.WAIT_MAX


# ---- provider registry ----

def test_get_provider_default(monkeypatch):
    monkeypatch.delenv("IMAGE_PROVIDER", raising=False)
    assert isinstance(g.get_provider(), g.HuggingFaceImageProvider)


def test_get_provider_from_env(monkeypatch):
    monkeypatch.setitem(g.PROVIDERS, "fake", lambda: ScriptedProvider(["IMG"]))
    monkeypatch.setenv("IMAGE_PROVIDER", "FAKE")
    assert isinstance(g.get_provider(), ScriptedProvider)


def test_get_provider_unknown():
    with pytest.raises(ValueError, match="Unknown image provider"):
        g.get_provider("nope")


def test_missing_token_raises(monkeypatch):
    monkeypatch.delenv("HF_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="HF_TOKEN"):
        g.HuggingFaceImageProvider().client


def test_client_built_once_with_timeout(monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "hf_fake")
    created = []

    class FakeClient:
        def __init__(self, **kw):
            created.append(kw)

    monkeypatch.setattr(g, "InferenceClient", FakeClient)
    p = g.HuggingFaceImageProvider(provider="auto", timeout=42)
    assert p.client is p.client
    assert created == [{"provider": "auto", "api_key": "hf_fake", "timeout": 42}]


# ---- call_image_api / generate_image with a fake provider ----

def test_success_first_try():
    p = ScriptedProvider(["IMG"])
    assert g.generate_image("x", p) == "IMG"
    assert p.calls == 1


def test_retries_then_succeeds():
    p = ScriptedProvider([make_http_error(503), make_http_error(429), "IMG"])
    assert g.generate_image("x", p) == "IMG"
    assert p.calls == 3


def test_gives_up_after_max_attempts():
    p = ScriptedProvider([make_http_error(429)])
    assert g.generate_image("x", p) is None
    assert p.calls == g.MAX_ATTEMPTS


def test_non_retryable_not_retried():
    p = ScriptedProvider([make_http_error(401)])
    assert g.generate_image("x", p) is None
    assert p.calls == 1


def test_call_image_api_reraises_original_error():
    p = ScriptedProvider([make_http_error(503)])
    with pytest.raises(type(make_http_error(503))):  # reraise=True, not RetryError
        g.call_image_api(p, "x")
    assert p.calls == g.MAX_ATTEMPTS


def test_unexpected_exception_returns_none():
    p = ScriptedProvider([RuntimeError("bad")])
    assert g.generate_image("x", p) is None
    assert p.calls == 1


def test_generate_image_accepts_provider_name(monkeypatch):
    fake = ScriptedProvider(["IMG"])
    monkeypatch.setitem(g.PROVIDERS, "fake", lambda: fake)
    assert g.generate_image("x", "fake") == "IMG"


def test_generate_image_unknown_provider_name_returns_none():
    assert g.generate_image("x", "nope") is None


def test_failure_messages_printed(capsys):
    g.generate_image("x", g.HuggingFaceImageProvider.__new__(g.HuggingFaceImageProvider))
    # a bare, uninitialised provider fails inside generate; must be caught
    assert "Failed to generate image" in capsys.readouterr().out
