"""Configuration, credential and secret-redaction tests. No network, no real keys."""
import io
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import config
import generateImage
import generatePrompts
import logging_config
from conftest import ScriptedProvider

ROOT = Path(__file__).resolve().parent.parent
FAKE_GEMINI = "AIzaSyFAKEFAKEFAKEFAKEFAKEFAKEFAKE12345"
FAKE_HF = "hf_FAKEFAKEFAKEFAKE1234567890"


# ---- defaults and parsing
def test_defaults_match_the_agreed_development_values():
    s = config.load_settings({})
    assert s.generation_enabled is True
    assert (s.max_prompts_per_request, s.max_images_per_request) == (10, 2)
    assert (s.max_concept_length, s.max_image_prompt_length) == (500, 2000)
    assert (s.max_generation_requests_per_minute, s.max_images_per_day) == (3, 5)
    assert s.quota_timezone == "Asia/Jerusalem"
    assert s.hf_monthly_budget_usd == 4.75


def test_values_can_be_overridden():
    s = config.load_settings({"MAX_IMAGES_PER_REQUEST": "3", "MAX_IMAGES_PER_DAY": "9",
                              "GENERATION_ENABLED": "No", "QUOTA_TIMEZONE": "UTC"})
    assert (s.max_images_per_request, s.max_images_per_day) == (3, 9)
    assert s.generation_enabled is False
    assert s.quota_timezone == "UTC"


def test_safety_margin_reduces_usable_hf_budget():
    s = config.load_settings({"HF_MONTHLY_BUDGET_USD": "4.75", "HF_BUDGET_SAFETY_MARGIN_PERCENT": "10"})
    assert s.hf_usable_budget_usd == pytest.approx(4.275)
    assert config.load_settings({"HF_BUDGET_SAFETY_MARGIN_PERCENT": "0"}).hf_usable_budget_usd == 4.75


@pytest.mark.parametrize("name,value", [
    ("MAX_PROMPTS_PER_REQUEST", "0"), ("MAX_PROMPTS_PER_REQUEST", "-1"), ("MAX_PROMPTS_PER_REQUEST", "abc"),
    ("MAX_IMAGES_PER_REQUEST", "2.5"), ("MAX_CONCEPT_LENGTH", ""), ("MAX_IMAGE_PROMPT_LENGTH", "0"),
    ("MAX_GENERATION_REQUESTS_PER_MINUTE", "0"), ("MAX_IMAGES_PER_DAY", "-5"),
    ("GENERATION_ENABLED", "maybe"), ("GENERATION_ENABLED", ""),
    ("QUOTA_TIMEZONE", "Mars/Olympus"), ("QUOTA_TIMEZONE", ""),
    ("HF_MONTHLY_BUDGET_USD", "0"), ("HF_MONTHLY_BUDGET_USD", "-1"), ("HF_MONTHLY_BUDGET_USD", "nan"),
    ("HF_MONTHLY_BUDGET_USD", "inf"), ("HF_MONTHLY_BUDGET_USD", "free"),
    ("HF_BUDGET_SAFETY_MARGIN_PERCENT", "100"), ("HF_BUDGET_SAFETY_MARGIN_PERCENT", "-1"),
    ("GEMINI_MAX_REQUESTS_PER_DAY", "0"),
])
def test_invalid_values_are_rejected_not_defaulted(name, value):
    with pytest.raises(config.ConfigError, match=name):
        config.load_settings({name: value})


def test_inconsistent_limits_are_rejected():
    with pytest.raises(config.ConfigError, match="cannot exceed MAX_IMAGES_PER_DAY"):
        config.load_settings({"MAX_IMAGES_PER_REQUEST": "6", "MAX_IMAGES_PER_DAY": "5"})
    with pytest.raises(config.ConfigError, match="GEMINI_MAX_REQUESTS_PER_MINUTE"):
        config.load_settings({"GEMINI_MAX_REQUESTS_PER_MINUTE": "100", "GEMINI_MAX_REQUESTS_PER_DAY": "50"})


def test_all_problems_are_reported_together():
    with pytest.raises(config.ConfigError) as exc:
        config.load_settings({"MAX_CONCEPT_LENGTH": "x", "HF_MONTHLY_BUDGET_USD": "-2"})
    assert "MAX_CONCEPT_LENGTH" in str(exc.value) and "HF_MONTHLY_BUDGET_USD" in str(exc.value)


def test_get_settings_reads_environment_and_caches(monkeypatch):
    config.get_settings.cache_clear()
    monkeypatch.setenv("MAX_IMAGES_PER_REQUEST", "1")
    try:
        assert config.get_settings().max_images_per_request == 1
        assert config.get_settings() is config.get_settings()
    finally:
        config.get_settings.cache_clear()


def test_env_example_has_placeholder_credentials_and_valid_settings():
    values = {}
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            values[key] = value
    assert values["API_KEY"].startswith("your-") and values["HF_TOKEN"].startswith("your-")
    limits = {k: v for k, v in values.items() if k not in ("API_KEY", "HF_TOKEN")}
    assert config.load_settings(limits) == config.load_settings({})


# ---- credentials
def test_missing_credential_error_names_the_variable(monkeypatch):
    monkeypatch.delenv("HF_TOKEN", raising=False)
    with pytest.raises(config.ConfigError, match="HF_TOKEN is not set"):
        config.require_secret("HF_TOKEN")


def test_blank_credential_counts_as_missing(monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "   ")
    with pytest.raises(config.ConfigError):
        config.require_secret("HF_TOKEN")


def test_require_secret_returns_value(monkeypatch):
    monkeypatch.setenv("API_KEY", FAKE_GEMINI)
    assert config.require_secret("API_KEY") == FAKE_GEMINI


def test_config_errors_never_contain_secret_values(monkeypatch):
    monkeypatch.setenv("HF_TOKEN", FAKE_HF)
    monkeypatch.setenv("API_KEY", FAKE_GEMINI)
    with pytest.raises(config.ConfigError) as exc:
        config.load_settings({"MAX_IMAGES_PER_DAY": "oops"})
    assert FAKE_HF not in str(exc.value) and FAKE_GEMINI not in str(exc.value)


# ---- redaction
@pytest.mark.parametrize("text", [
    f"key={FAKE_GEMINI}",
    f"Authorization: Bearer {FAKE_HF}",
    f"token {FAKE_HF} rejected",
    '{"x-goog-api-key": "' + FAKE_GEMINI + '"}',
    f"https://x.invalid/v1?api_key={FAKE_GEMINI}&a=1",
])
def test_redact_removes_key_shaped_strings(text):
    out = logging_config.redact(text)
    assert FAKE_GEMINI not in out and FAKE_HF not in out
    assert logging_config.REDACTED in out


def test_redact_removes_the_configured_secret_even_if_oddly_shaped(monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "custom-secret-value-123")
    assert "custom-secret-value-123" not in logging_config.redact("failed with custom-secret-value-123 here")


def test_redact_leaves_ordinary_text_alone():
    assert logging_config.redact("Image API failed with status 503") == "Image API failed with status 503"


def test_logs_do_not_expose_secrets(monkeypatch):
    monkeypatch.setenv("HF_TOKEN", FAKE_HF)
    logging_config.configure_logging()
    root = logging.getLogger()
    handler = next(h for h in root.handlers if getattr(h, "_prompt_expander", False))
    stream = io.StringIO()
    handler.setStream(stream)
    try:
        logging.getLogger("test").error("call failed: %s / %s", FAKE_HF, f"Bearer {FAKE_GEMINI}")
        logging.getLogger("test").info("header Authorization: Bearer %s", FAKE_HF)
    finally:
        root.removeHandler(handler)
    out = stream.getvalue()
    assert out and FAKE_HF not in out and FAKE_GEMINI not in out


def test_configure_logging_twice_adds_one_handler():
    logging_config.configure_logging()
    logging_config.configure_logging()
    root = logging.getLogger()
    mine = [h for h in root.handlers if getattr(h, "_prompt_expander", False)]
    try:
        assert len(mine) == 1
    finally:
        for h in mine:
            root.removeHandler(h)


# ---- user-visible errors from providers
def test_image_error_message_is_redacted(monkeypatch):
    monkeypatch.setenv("HF_TOKEN", FAKE_HF)
    provider = ScriptedProvider([RuntimeError(f"401 for Authorization: Bearer {FAKE_HF}")])
    image, error = generateImage.generate_image_with_error("p", provider)
    assert image is None and FAKE_HF not in error


def test_gemini_error_message_is_redacted(monkeypatch):
    monkeypatch.setenv("API_KEY", FAKE_GEMINI)

    def boom(prompt_idea):
        raise RuntimeError(f"bad request x-goog-api-key: {FAKE_GEMINI}")

    monkeypatch.setattr(generatePrompts, "call_gemini", boom)
    text, error = generatePrompts.generate_prompt_text("a cat")
    assert text is None and FAKE_GEMINI not in error


def test_missing_hf_token_error_is_clear(monkeypatch):
    monkeypatch.delenv("HF_TOKEN", raising=False)
    image, error = generateImage.generate_image_with_error("p", generateImage.HuggingFaceImageProvider())
    assert image is None and "HF_TOKEN is not set" in error


# ---- startup
def test_main_exits_cleanly_on_invalid_config():
    """Run main.py as a script with a bad setting: it must print the problem and exit 1."""
    env = {**os.environ, "MAX_IMAGES_PER_DAY": "-1", "PYTHONPATH": str(ROOT)}
    with tempfile.TemporaryDirectory() as d:
        r = subprocess.run([sys.executable, str(ROOT / "main.py")], cwd=d, env=env,
                           capture_output=True, text=True, input="")
    assert r.returncode == 1
    assert "MAX_IMAGES_PER_DAY" in r.stdout
