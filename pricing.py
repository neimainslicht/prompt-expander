"""The only place that decides what a provider call is assumed to cost.

Nothing here is a published price. Hugging Face does not return the cost of an image
call, so the cost per call is whatever you configure in HF_COST_PER_IMAGE_USD (look it
up on your provider's pricing page and keep it up to date; it can change). Until it is
set, paid image generation is refused instead of running unmetered.

Gemini is only supported on the free tier (config.GEMINI_PLAN), so its calls cost
nothing and are limited by request counts instead (see limits.py).

Costs are per real provider call, retries included: a failed or interrupted call is
assumed to cost the same as a successful one, because providers may bill for it.

Amounts are whole microdollars (1 USD = 1,000,000), so budgets are compared exactly.
"""
from errors import PricingUnavailableError


def provider_family(provider_label: str) -> str:
    """'huggingface/fal-ai' -> 'huggingface'."""
    return provider_label.split("/", 1)[0]


def call_cost_micros(provider_label: str, settings) -> int:
    """Assumed cost in microdollars of one real call to this provider. Raises PricingUnavailableError."""
    family = provider_family(provider_label)
    if family == "gemini":
        return 0  # free tier only (validated in config)
    if family == "huggingface":
        if settings.hf_cost_per_image_micros is None:
            raise PricingUnavailableError(
                f"The cost of an image from {provider_label} is not configured, so generation was not started. "
                "Set HF_COST_PER_IMAGE_USD in your .env file (see README, 'Costs and budget').")
        return settings.hf_cost_per_image_micros
    raise PricingUnavailableError(
        f"No pricing is configured for provider '{provider_label}', so generation was not started.")
