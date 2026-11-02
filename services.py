"""Application workflows, independent of any user interface.

The CLI (main.py) and any future web backend should call these functions and
never talk to the API providers directly. Nothing here prompts, prints or reads
API keys: keys and provider-specific code stay in generatePrompts.py and
generateImage.py.

Failures of a single idea or image come back as results. A request that is refused
as a whole (generation switched off, too many images, an over-long image prompt)
raises an errors.AppError before any provider is called; it has a `code`,
`message` and `http_status` a web layer can use.

Every generation request is recorded in the usage store (usage.py) and every real
provider call, retries included, is recorded as an attempt. If the request cannot be
recorded, generation does not start.

`store` is the object that saves these usage records (a usage.UsageStore, by default the
SQLite file from USAGE_DB_PATH). Pass `store=` to use a different one; tests do this.
"""
import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

import config
import fileUtils
import generateImage
import generatePrompts
import usage
import validation
from errors import AppError, UsageUnavailableError

logger = logging.getLogger(__name__)


@dataclass
class ExpandResult:
    idea: str
    ok: bool
    prompt_file: Optional[str] = None  # path of the saved JSON
    data: Optional[dict] = None
    error: Optional[str] = None
    error_code: Optional[str] = None  # set when the request was refused by validation


@dataclass
class ImageResult:
    prompt_id: int
    ok: bool
    status: str  # status stored in the prompt file after this attempt
    image_path: Optional[str] = None
    error: Optional[str] = None
    error_code: Optional[str] = None


def _record_rejection(store, operation, items_requested, code, provider=None, model=None):
    """Keep a diagnostic record of a refused request. Never hides the refusal itself."""
    try:
        store.reject_request(operation, items_requested, code, provider=provider, model=model)
    except Exception:
        logger.error("Could not record a rejected request (%s)", code)


def _finish_request(store, request_id, outcome, items_succeeded, category):
    """Close a pending usage record with its final outcome, counts and error category.

    Called from `finally` blocks, so it runs even when a request fails or is interrupted.
    The provider calls have already happened by then, so if the record can't be updated
    we only log it: raising here would hide the real result (or the real error) from the
    caller. The affected record stays "pending".
    """
    try:
        store.finish_request(request_id, outcome, items_succeeded, error_category=category)
    except Exception:
        logger.error("Could not close usage record %s", request_id)


def expand_idea(idea, num_prompts=None, store=None) -> ExpandResult:
    """Turn one idea into a saved file of detailed image prompts.

    Invalid or refused requests return a failed result (with error_code) without
    calling Gemini; they are recorded as rejected. num_prompts defaults to
    MAX_PROMPTS_PER_REQUEST.
    """
    try:
        store = store or usage.get_store()
    except Exception:
        return ExpandResult(idea=idea, ok=False, error=UsageUnavailableError().message,
                            error_code=UsageUnavailableError.code)
    try:
        validation.ensure_generation_enabled()
        validation.validate_concept(idea)
        num_prompts = validation.validate_prompt_count(num_prompts)
    except AppError as e:
        requested = num_prompts if isinstance(num_prompts, int) and not isinstance(num_prompts, bool) else 0
        _record_rejection(store, usage.PROMPT_GENERATION, requested, e.code, "gemini", generatePrompts.GEMINI_MODEL)
        return ExpandResult(idea=idea, ok=False, error=e.message, error_code=e.code)
    except config.ConfigError as e:
        return ExpandResult(idea=idea, ok=False, error=str(e), error_code="config_error")

    try:
        request_id = store.begin_request(usage.PROMPT_GENERATION, "gemini", generatePrompts.GEMINI_MODEL, num_prompts)
    except Exception:
        return ExpandResult(idea=idea, ok=False, error=UsageUnavailableError().message,
                            error_code=UsageUnavailableError.code)
    observer = usage.AttemptObserver(store, request_id, "gemini", generatePrompts.GEMINI_MODEL)

    succeeded, category = 0, "interrupted"  # stays "interrupted" only if we are cut off mid-request
    try:
        text, error = generatePrompts.generate_prompt_text(idea, num_prompts, observer=observer)
        if error:
            category = observer.last_error_category or "not_attempted"
            return ExpandResult(idea=idea, ok=False, error=error)
        try:
            data = fileUtils.parseJSON(text, idea)
            # the model may return more prompts than asked for; keep only what was requested
            data["prompts"] = data["prompts"][:num_prompts]
            path = fileUtils.saveJSON(data)
        except Exception as e:
            category = "invalid_response"
            return ExpandResult(idea=idea, ok=False, error=f"Could not process the model response: {e}")
        succeeded = len(data["prompts"])
        category = None if succeeded >= num_prompts else "fewer_prompts_than_requested"
        return ExpandResult(idea=idea, ok=True, prompt_file=path, data=data)
    finally:
        _finish_request(store, request_id, usage.outcome_for(num_prompts, succeeded), succeeded, category)


def expand_ideas(ideas, delay=1.5, on_progress: Optional[Callable] = None, store=None) -> list:
    """Expand each idea as its own concept. One failure never stops the rest.

    on_progress(idea, None) is called before each idea and on_progress(idea, result) after.
    Raises GenerationDisabledError up front if generation is switched off.
    """
    validation.ensure_generation_enabled()
    results = []
    for n, idea in enumerate(ideas):
        if n and delay:
            time.sleep(delay)  # space out API calls
        if on_progress:
            on_progress(idea, None)
        result = expand_idea(idea, store=store)
        results.append(result)
        if on_progress:
            on_progress(idea, result)
    return results


def parse_ideas(text) -> list:
    """Split text into one idea per non-empty line."""
    return [line.strip() for line in text.splitlines() if line.strip()]


def read_ideas_file(path) -> list:
    """Read ideas from a text file. Raises OSError if the file can't be read."""
    with open(path, "r", encoding="utf-8") as f:
        return parse_ideas(f.read())


def ensure_generation_enabled():
    """Raise GenerationDisabledError if generation is switched off (lets a UI check early)."""
    validation.ensure_generation_enabled()


def max_images_per_request() -> int:
    return config.get_settings().max_images_per_request


def list_prompt_files() -> list:
    return fileUtils.getOutputFiles()


def load_prompt_file(name) -> Optional[dict]:
    """Load a prompt file by name, or None if it doesn't exist or is unreadable.

    Only names from list_prompt_files() are accepted, so callers can't reach other paths.
    """
    if name not in fileUtils.getOutputFiles():
        return None
    return fileUtils.getJSONObject(f"output/{name}")


def _stored_status(prompt_data, prompt_id):
    for p in prompt_data["prompts"]:
        if p["id"] == prompt_id:
            return p.get("status")
    return None


def generate_images(prompt_data, prompt_ids, provider=None, on_progress: Optional[Callable] = None,
                    store=None) -> list:
    """Generate an image for each chosen prompt id and record the outcome in prompt_data.

    One prompt failing (generation or saving) never affects the others. A failed
    rerun leaves a prompt that is already "generated" as "generated", since its
    earlier image still exists. The prompt file is saved even if interrupted.
    on_progress(prompt_id, None) is called before each prompt and on_progress(prompt_id, result) after.

    Raises an AppError before anything is generated if generation is switched off,
    more images are chosen than allowed, a chosen prompt is empty or too long, or the
    request cannot be recorded. Refused requests are recorded as rejected. Otherwise
    the batch is one usage record, with one attempt per real provider call.
    """
    results = []
    prompts = {p["id"]: p for p in prompt_data["prompts"]}
    prompt_ids = list(prompt_ids or [])
    request_id = None
    if prompt_ids:
        try:
            store = store or usage.get_store()
        except Exception:
            raise UsageUnavailableError() from None
        try:
            validation.ensure_generation_enabled()
            validation.validate_image_count(len(prompt_ids))
            for prompt_id in prompt_ids:
                if prompt_id in prompts:
                    validation.validate_image_prompt(prompts[prompt_id]["text"], label=f"Prompt {prompt_id}")
        except AppError as e:
            _record_rejection(store, usage.IMAGE_GENERATION, len(prompt_ids), e.code)
            raise

    provider_error = None
    try:
        if not isinstance(provider, generateImage.ImageProvider):
            provider = generateImage.get_provider(provider)
    except Exception as e:
        provider_error = str(e)

    provider_label, model = "unresolved", None
    if provider_error is None:
        provider_label, model = provider.usage_identity()
    if prompt_ids:
        try:
            request_id = store.begin_request(usage.IMAGE_GENERATION, provider_label, model, len(prompt_ids))
        except Exception:
            raise UsageUnavailableError() from None

    succeeded, processed, item_categories = 0, 0, []
    try:
        for prompt_id in prompt_ids:
            if on_progress:
                on_progress(prompt_id, None)
            failed_status = "generated" if _stored_status(prompt_data, prompt_id) == "generated" else "failed"
            category = "unexpected"
            try:
                if prompt_id not in prompts:
                    category = "unknown_prompt"
                    result = ImageResult(prompt_id, False, failed_status, error=f"Unknown prompt id {prompt_id}.")
                else:
                    observer = None
                    if provider_error:
                        image, error = None, provider_error
                        category = "provider_unavailable"
                    else:
                        observer = usage.AttemptObserver(store, request_id, provider_label, model, item_ref=prompt_id)
                        image, error = generateImage.generate_image_with_error(
                            prompts[prompt_id]["text"], provider, observer=observer)
                        category = observer.last_error_category or "not_attempted"
                    if image:
                        category = "save_failed"  # the provider call succeeded; saving may still fail
                        path = fileUtils.saveImage(image, prompt_data["concept"], prompt_id)
                        fileUtils.updatePromptStatus(prompt_data, prompt_id, "generated")
                        result = ImageResult(prompt_id, True, "generated", image_path=path)
                        category = None
                    else:
                        fileUtils.updatePromptStatus(prompt_data, prompt_id, failed_status)
                        result = ImageResult(prompt_id, False, failed_status, error=error or "Image generation failed.")
            except Exception as e:
                try:
                    fileUtils.updatePromptStatus(prompt_data, prompt_id, failed_status)
                except Exception:
                    pass
                result = ImageResult(prompt_id, False, failed_status, error=f"Unexpected error: {e}")
            processed += 1
            if result.ok:
                succeeded += 1
            else:
                item_categories.append(category)
            results.append(result)
            if on_progress:
                on_progress(prompt_id, result)
    finally:
        # persist statuses even if the batch is interrupted (e.g. Ctrl+C)
        fileUtils.saveJSON(prompt_data)
        if request_id is not None:
            if processed < len(prompt_ids):
                item_categories.append("interrupted")
            failures = set(item_categories)
            category = None if not failures else (failures.pop() if len(failures) == 1 else "multiple")
            _finish_request(store, request_id, usage.outcome_for(len(prompt_ids), succeeded), succeeded, category)
    return results
