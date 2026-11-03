"""Turns the configured limits into the quotas that usage.py checks atomically.

A Plan says what one request needs: which quotas to check before it starts, what to
reserve for it, and which quotas to re-check before every single provider call (so
retries are limited too).

What each limit counts:

  MAX_GENERATION_REQUESTS_PER_MINUTE   authorised requests started in the last 60 s
                                       (an idea, or an image batch). Retries are not new requests.
  MAX_IMAGES_PER_DAY                   images, per calendar day in QUOTA_TIMEZONE. An active
                                       request counts the images it reserved; a finished one counts
                                       the images that got a provider attempt. Retries of one image
                                       are never extra images.
  GEMINI_MAX_REQUESTS_PER_MINUTE / _PER_DAY
                                       real Gemini calls, every attempt including retries.
  HF_BUDGET_USD (with margin)          Hugging Face spend in USD; every attempt costs the configured
                                       per-call price, even if it failed. HF_BUDGET_MODE=cumulative counts
                                       all recorded history; HF_BUDGET_MODE=monthly counts the current
                                       calendar month in QUOTA_TIMEZONE (see budget_quota).

For an image batch the whole batch is authorised and reserved before the first call:
daily images, and the cost of exactly one attempt per image. Each image's reservation
covers only that image's first attempt. Every retry needs additional budget, checked
against the remaining budget just before it is made, so retries can neither pass the
budget nor use up another image's reservation. A batch that cannot be fully authorised
never starts.
"""
from dataclasses import dataclass
from typing import Optional

import config
import money
import pricing
import usage
from usage import Quota


@dataclass(frozen=True)
class Plan:
    provider: str
    model: Optional[str]
    items: int
    begin_quotas: tuple         # checked once, atomically with creating the request
    attempt_quotas: tuple       # checked again before every real provider call
    reserved_images: int = 0
    reserved_cost_micros: Optional[int] = None   # money is whole microdollars throughout
    attempt_cost_micros: Optional[int] = None


def _request_rate_quota(settings) -> Quota:
    return Quota(
        code="rate_limited", metric=usage.REQUESTS, adding=1,
        ceiling=settings.max_generation_requests_per_minute, window_seconds=60,
        message="Too many generation requests: {current} in the last minute (limit {ceiling}). "
                "Please wait and try again.")


def prompt_plan(provider_label, model, settings=None) -> Plan:
    """Plan for one Gemini prompt-generation request."""
    settings = settings or config.get_settings()
    family = pricing.provider_family(provider_label)
    per_minute = Quota(
        code="provider_rate_limited", metric=usage.ATTEMPTS, adding=1, provider_prefix=family,
        ceiling=settings.gemini_max_requests_per_minute, window_seconds=60,
        message="The Gemini request limit for this minute has been reached ({current} of {ceiling}). "
                "Please wait a moment.")
    per_day = Quota(
        code="provider_daily_limit", metric=usage.ATTEMPTS, adding=1, provider_prefix=family,
        ceiling=settings.gemini_max_requests_per_day, day_timezone=settings.quota_timezone,
        message="The Gemini daily request limit has been reached ({current} of {ceiling}). "
                f"It resets at midnight ({settings.quota_timezone}).")
    return Plan(
        provider=provider_label, model=model, items=1,
        begin_quotas=(_request_rate_quota(settings), per_minute, per_day),
        attempt_quotas=(per_minute, per_day),
        attempt_cost_micros=pricing.call_cost_micros(provider_label, settings))


def budget_quota(settings, family, adding=0) -> Quota:
    """The image budget as a Quota. This is the single definition of what the budget counts:
    cumulative mode counts all recorded history (no time window); monthly mode counts only the
    current calendar month in QUOTA_TIMEZONE. The ceiling is always settings.hf_usable_budget_micros
    (the safety margin is applied there, once, and not again here)."""
    monthly = settings.hf_budget_is_monthly
    review = "Run 'python admin.py status' to review (see README, 'Restoring generation')."
    if monthly:
        message = (f"The {family} monthly budget would be exceeded: {{current}} of {{ceiling}} is already used or "
                   f"reserved this month, and this request needs about {money.format_usd(adding)}. "
                   f"It resets on the 1st of next month ({settings.quota_timezone}). {review}")
    else:
        message = (f"The {family} budget would be exceeded: {{current}} of {{ceiling}} is already used or reserved "
                   f"(cumulative, all months), and this request needs about {money.format_usd(adding)}. {review}")
    return Quota(
        code="budget_exceeded", metric=usage.COST_MICROS, adding=adding, provider_prefix=family,
        ceiling=settings.hf_usable_budget_micros,
        month_timezone=settings.quota_timezone if monthly else None, message=message)


def image_plan(provider_label, model, count, settings=None) -> Plan:
    """Plan for a batch of `count` images. Raises PricingUnavailableError if the cost is unknown."""
    settings = settings or config.get_settings()
    family = pricing.provider_family(provider_label)
    call_cost = pricing.call_cost_micros(provider_label, settings)
    daily = Quota(
        code="daily_quota_exceeded", metric=usage.IMAGES, adding=count,
        ceiling=settings.max_images_per_day, day_timezone=settings.quota_timezone,
        message="Daily image limit reached: {current} of {ceiling} images already used or reserved today, "
                f"and this request needs {count}. It resets at midnight ({settings.quota_timezone}).")
    reserved = call_cost * count  # one first attempt per image
    budget = budget_quota(settings, family, adding=reserved)
    return Plan(
        provider=provider_label, model=model, items=count,
        begin_quotas=(_request_rate_quota(settings), daily, budget),
        attempt_quotas=(budget,),
        reserved_images=count, reserved_cost_micros=reserved, attempt_cost_micros=call_cost)
