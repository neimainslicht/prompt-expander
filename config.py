"""Central configuration: usage limits, provider budgets and credentials.

Values come from environment variables, with a local .env file loaded as a
fallback. Every setting is validated, and a bad value raises ConfigError instead
of being silently replaced by a default. Importing this module never needs API
keys or valid settings; they are checked when get_settings() / require_secret()
is called.
"""
import os
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from functools import lru_cache
from typing import Mapping, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

import money

load_dotenv()


class ConfigError(RuntimeError):
    """A setting or credential is missing or invalid. Messages never contain secret values."""


# Defaults are development values; override any of them in .env.
DEFAULTS = {
    "GENERATION_ENABLED": "true",
    "MAX_PROMPTS_PER_REQUEST": "10",
    "MAX_IMAGES_PER_REQUEST": "2",
    "MAX_CONCEPT_LENGTH": "500",
    "MAX_IMAGE_PROMPT_LENGTH": "2000",
    "MAX_GENERATION_REQUESTS_PER_MINUTE": "3",
    "MAX_IMAGES_PER_DAY": "5",
    "QUOTA_TIMEZONE": "Asia/Jerusalem",
    "USAGE_DB_PATH": "usage.db",  # SQLite file holding the usage records (git-ignored)
    # Gemini (free tier): request counts only, no spending. These are conservative
    # application-side caps, not Google's published limits; check yours in AI Studio.
    "GEMINI_MAX_REQUESTS_PER_MINUTE": "5",
    "GEMINI_MAX_REQUESTS_PER_DAY": "50",
    "GEMINI_PLAN": "free",  # only the free tier is supported: Gemini is limited by request counts, not spending
    # Hugging Face (prepaid credits): application-side budget in USD, counted cumulatively (no monthly reset).
    "HF_BUDGET_USD": "4.75",
    "HF_BUDGET_SAFETY_MARGIN_PERCENT": "10",
    # A request left "pending" with no activity for this long is treated as abandoned (crashed).
    "STALE_REQUEST_SECONDS": "900",
}

# Settings with no default: image generation is refused until they are set.
# HF_COST_PER_IMAGE_USD: your estimate of what one Hugging Face image call costs (also used for retries).

RENAMED = {"HF_MONTHLY_BUDGET_USD": "HF_BUDGET_USD"}

# Longest a single provider call may take, and the longest pause between retries. The provider
# modules import these, so the timeouts they apply and the rule below cannot drift apart.
GEMINI_TIMEOUT_SECONDS = 60
HF_TIMEOUT_SECONDS = 120
RETRY_WAIT_MAX_SECONDS = 60

# A request counts as abandoned after STALE_REQUEST_SECONDS with no activity. Activity is recorded
# when a provider call starts and when it ends, so the longest legitimate silence is one call (or one
# retry pause). Requiring twice the longest one means a slow but live request is never mistaken for a
# crashed one and has its reservation released.
MIN_STALE_REQUEST_SECONDS = 2 * max(GEMINI_TIMEOUT_SECONDS, HF_TIMEOUT_SECONDS, RETRY_WAIT_MAX_SECONDS)

_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off"}


@dataclass(frozen=True)
class Settings:
    generation_enabled: bool
    max_prompts_per_request: int
    max_images_per_request: int
    max_concept_length: int
    max_image_prompt_length: int
    max_generation_requests_per_minute: int
    max_images_per_day: int
    quota_timezone: str  # IANA name; the daily quota resets at midnight in this zone
    usage_db_path: str
    gemini_max_requests_per_minute: int
    gemini_max_requests_per_day: int
    gemini_plan: str
    hf_budget_usd: Decimal  # the balance you are willing to track in total (all months), before the margin
    hf_budget_safety_margin_percent: Decimal
    hf_cost_per_image_usd: Optional[Decimal]  # None until configured; see pricing.py
    stale_request_seconds: int

    @property
    def hf_budget_micros(self) -> int:
        return money.usd_to_micros(self.hf_budget_usd)

    @property
    def hf_usable_budget_micros(self) -> int:
        """The spending ceiling in whole microdollars: the budget minus the safety margin, rounded
        down. This is the one place the margin is applied; everything else reads this value."""
        kept = (Decimal(100) - self.hf_budget_safety_margin_percent) / Decimal(100)
        return int((Decimal(self.hf_budget_micros) * kept).to_integral_value(rounding=ROUND_FLOOR))

    @property
    def hf_usable_budget_usd(self) -> Decimal:
        """Budget the app may actually plan to spend, in USD (exact)."""
        return money.micros_to_usd(self.hf_usable_budget_micros)

    @property
    def hf_cost_per_image_micros(self) -> Optional[int]:
        if self.hf_cost_per_image_usd is None:
            return None
        return money.usd_to_micros(self.hf_cost_per_image_usd)


def _parse_int(name, raw, minimum=1):
    try:
        value = int(raw.strip())
    except ValueError:
        raise ValueError(f"{name} must be a whole number, got {raw!r}.") from None
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}, got {value}.")
    return value


def _parse_bool(name, raw):
    text = raw.strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ValueError(f"{name} must be true or false, got {raw!r}.")


def _parse_usd(name, raw):
    """A positive dollar amount with at most 6 decimal places, kept exact as a Decimal."""
    try:
        micros = money.usd_to_micros(raw)
    except ValueError:
        raise ValueError(f"{name} must be a dollar amount with at most 6 decimal places, got {raw!r}.") from None
    if micros <= 0:
        raise ValueError(f"{name} must be greater than 0, got {raw!r}.")
    return money.micros_to_usd(micros)


def _parse_percent(name, raw):
    """A percentage from 0 up to (not including) 100, kept exact as a Decimal."""
    try:
        value = Decimal(raw.strip())
    except InvalidOperation:
        raise ValueError(f"{name} must be a number, got {raw!r}.") from None
    if not value.is_finite() or value < 0 or value >= 100:
        raise ValueError(f"{name} must be at least 0 and less than 100, got {raw!r}.")
    return value


def _parse_timezone(name, raw):
    key = raw.strip()
    try:
        ZoneInfo(key)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ValueError(f"{name} must be a valid IANA time zone such as 'Asia/Jerusalem', got {raw!r}.") from None
    return key


def _parse_path(name, raw):
    path = raw.strip()
    if not path:
        raise ValueError(f"{name} must not be blank.")
    return path


def _parse_gemini_plan(name, raw):
    plan = raw.strip().lower()
    if plan != "free":
        raise ValueError(
            f"{name} must be 'free'. Spending limits for paid Gemini plans are not supported yet, "
            f"so the app refuses to run on one by mistake; got {raw!r}.")
    return plan


def load_settings(env: Optional[Mapping[str, str]] = None) -> Settings:
    """Build Settings from env (default: os.environ), checking every value.

    All problems are collected and reported together in one ConfigError.
    """
    env = os.environ if env is None else env
    errors = []

    def read_value(name, raw, parser, *args, **kwargs):
        try:
            return parser(name, raw, *args, **kwargs)
        except ValueError as e:
            errors.append(str(e))
            return None

    def read(name, parser, *args, **kwargs):
        raw = env.get(name)
        if raw is None:
            raw = DEFAULTS[name]
        return read_value(name, raw, parser, *args, **kwargs)

    for old, new in RENAMED.items():
        if old in env:
            errors.append(f"{old} was renamed to {new}. Rename it in your .env file.")

    optional_cost = None
    if "HF_COST_PER_IMAGE_USD" in env:
        optional_cost = read_value("HF_COST_PER_IMAGE_USD", env["HF_COST_PER_IMAGE_USD"], _parse_usd)

    values = dict(
        generation_enabled=read("GENERATION_ENABLED", _parse_bool),
        max_prompts_per_request=read("MAX_PROMPTS_PER_REQUEST", _parse_int),
        max_images_per_request=read("MAX_IMAGES_PER_REQUEST", _parse_int),
        max_concept_length=read("MAX_CONCEPT_LENGTH", _parse_int),
        max_image_prompt_length=read("MAX_IMAGE_PROMPT_LENGTH", _parse_int),
        max_generation_requests_per_minute=read("MAX_GENERATION_REQUESTS_PER_MINUTE", _parse_int),
        max_images_per_day=read("MAX_IMAGES_PER_DAY", _parse_int),
        quota_timezone=read("QUOTA_TIMEZONE", _parse_timezone),
        usage_db_path=read("USAGE_DB_PATH", _parse_path),
        gemini_max_requests_per_minute=read("GEMINI_MAX_REQUESTS_PER_MINUTE", _parse_int),
        gemini_max_requests_per_day=read("GEMINI_MAX_REQUESTS_PER_DAY", _parse_int),
        gemini_plan=read("GEMINI_PLAN", _parse_gemini_plan),
        hf_budget_usd=read("HF_BUDGET_USD", _parse_usd),
        hf_budget_safety_margin_percent=read("HF_BUDGET_SAFETY_MARGIN_PERCENT", _parse_percent),
        hf_cost_per_image_usd=optional_cost,
        stale_request_seconds=read("STALE_REQUEST_SECONDS", _parse_int, MIN_STALE_REQUEST_SECONDS),
    )

    # Relationships between settings: only checkable when both values parsed.
    per_request, per_day = values["max_images_per_request"], values["max_images_per_day"]
    if per_request is not None and per_day is not None and per_request > per_day:
        errors.append(
            f"MAX_IMAGES_PER_REQUEST ({per_request}) cannot exceed MAX_IMAGES_PER_DAY ({per_day})."
        )
    gem_min, gem_day = values["gemini_max_requests_per_minute"], values["gemini_max_requests_per_day"]
    if gem_min is not None and gem_day is not None and gem_min > gem_day:
        errors.append(
            f"GEMINI_MAX_REQUESTS_PER_MINUTE ({gem_min}) cannot exceed GEMINI_MAX_REQUESTS_PER_DAY ({gem_day})."
        )

    if errors:
        raise ConfigError("Invalid configuration:\n  - " + "\n  - ".join(errors))
    return Settings(**values)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Settings for the running process, loaded and validated once.

    Tests that change environment variables call get_settings.cache_clear().
    """
    return load_settings()


def require_secret(name: str) -> str:
    """Return the credential stored in environment variable `name`, or raise ConfigError.

    The error names the variable but never shows a value. Call this right before
    a provider is used, so importing the app never needs keys.
    """
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise ConfigError(f"{name} is not set. Add it to your .env file (see .env.example).")
    return value
