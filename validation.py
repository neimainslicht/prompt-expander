"""Request validation and the global generation switch.

These checks run before any provider is called and never change the input:
a request that is too long or too large is rejected, not truncated. Limits
come from config.get_settings(). Each function raises an errors.AppError.

They are called from services.py and again from the lowest-level provider
functions (generatePrompts / generateImage), so calling those directly cannot
skip them.
"""
import config
from errors import GenerationDisabledError, ValidationError


def ensure_generation_enabled():
    """Raise GenerationDisabledError if new generation requests are switched off."""
    if not config.get_settings().generation_enabled:
        raise GenerationDisabledError()


def validate_concept(concept):
    """Reject a missing, blank or over-long concept. Length ignores surrounding whitespace."""
    if not isinstance(concept, str) or not concept.strip():
        raise ValidationError("empty_concept", "Please enter an idea. It can't be empty.")
    limit = config.get_settings().max_concept_length
    length = len(concept.strip())
    if length > limit:
        raise ValidationError(
            "concept_too_long",
            f"The idea is {length} characters long, but the limit is {limit}. Please shorten it.",
            length=length, limit=limit)


def validate_prompt_count(count=None):
    """Return the number of prompts to request (default: the configured maximum)."""
    limit = config.get_settings().max_prompts_per_request
    if count is None:
        return limit
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValidationError("invalid_prompt_count", "The number of prompts must be a whole number of at least 1.")
    if count > limit:
        raise ValidationError(
            "too_many_prompts",
            f"You asked for {count} prompts, but the limit is {limit} per request.",
            requested=count, limit=limit)
    return count


def validate_image_prompt(prompt, label="The image prompt"):
    """Reject a missing, blank or over-long image prompt."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValidationError("empty_image_prompt", f"{label} is empty.")
    limit = config.get_settings().max_image_prompt_length
    length = len(prompt.strip())
    if length > limit:
        raise ValidationError(
            "image_prompt_too_long",
            f"{label} is {length} characters long, but the limit is {limit}.",
            length=length, limit=limit)


def validate_image_count(count):
    """Reject a request for more images than allowed in one request."""
    limit = config.get_settings().max_images_per_request
    if count > limit:
        raise ValidationError(
            "too_many_images",
            f"You selected {count} images, but the limit is {limit} per request. Select {limit} or fewer.",
            requested=count, limit=limit)
