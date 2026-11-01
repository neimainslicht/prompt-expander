"""Application workflows, independent of any user interface.

The CLI (main.py) and any future web backend should call these functions and
never talk to the API providers directly. Nothing here prompts, prints or reads
API keys: keys and provider-specific code stay in generatePrompts.py and
generateImage.py.

Failures of a single idea or image come back as results. A request that is refused
as a whole (generation switched off, too many images, an over-long image prompt)
raises an errors.AppError before any provider is called; it has a `code`,
`message` and `http_status` a web layer can use.
"""
import time
from dataclasses import dataclass
from typing import Callable, Optional

import config
import fileUtils
import generateImage
import generatePrompts
import validation
from errors import AppError


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


def expand_idea(idea, num_prompts=None) -> ExpandResult:
    """Turn one idea into a saved file of detailed image prompts.

    Invalid or refused requests return a failed result (with error_code) without
    calling Gemini. num_prompts defaults to MAX_PROMPTS_PER_REQUEST.
    """
    try:
        validation.ensure_generation_enabled()
        validation.validate_concept(idea)
        num_prompts = validation.validate_prompt_count(num_prompts)
    except AppError as e:
        return ExpandResult(idea=idea, ok=False, error=e.message, error_code=e.code)
    except config.ConfigError as e:
        return ExpandResult(idea=idea, ok=False, error=str(e), error_code="config_error")
    text, error = generatePrompts.generate_prompt_text(idea, num_prompts)
    if error:
        return ExpandResult(idea=idea, ok=False, error=error)
    try:
        data = fileUtils.parseJSON(text, idea)
        # the model may return more prompts than asked for; keep only what was requested
        data["prompts"] = data["prompts"][:num_prompts]
        path = fileUtils.saveJSON(data)
    except Exception as e:
        return ExpandResult(idea=idea, ok=False, error=f"Could not process the model response: {e}")
    return ExpandResult(idea=idea, ok=True, prompt_file=path, data=data)


def expand_ideas(ideas, delay=1.5, on_progress: Optional[Callable] = None) -> list:
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
        result = expand_idea(idea)
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


def generate_images(prompt_data, prompt_ids, provider=None, on_progress: Optional[Callable] = None) -> list:
    """Generate an image for each chosen prompt id and record the outcome in prompt_data.

    One prompt failing (generation or saving) never affects the others. A failed
    rerun leaves a prompt that is already "generated" as "generated", since its
    earlier image still exists. The prompt file is saved even if interrupted.
    on_progress(prompt_id, None) is called before each prompt and on_progress(prompt_id, result) after.

    Raises an AppError before anything is generated if generation is switched off,
    more images are chosen than allowed, or a chosen prompt is empty or too long.
    """
    results = []
    prompts = {p["id"]: p for p in prompt_data["prompts"]}
    if prompt_ids:
        validation.ensure_generation_enabled()
        validation.validate_image_count(len(prompt_ids))
        for prompt_id in prompt_ids:
            if prompt_id in prompts:
                validation.validate_image_prompt(prompts[prompt_id]["text"], label=f"Prompt {prompt_id}")
    provider_error = None
    try:
        if not isinstance(provider, generateImage.ImageProvider):
            provider = generateImage.get_provider(provider)
    except Exception as e:
        provider_error = str(e)

    try:
        for prompt_id in prompt_ids or []:
            if on_progress:
                on_progress(prompt_id, None)
            failed_status = "generated" if _stored_status(prompt_data, prompt_id) == "generated" else "failed"
            try:
                if prompt_id not in prompts:
                    result = ImageResult(prompt_id, False, failed_status, error=f"Unknown prompt id {prompt_id}.")
                else:
                    if provider_error:
                        image, error = None, provider_error
                    else:
                        image, error = generateImage.generate_image_with_error(prompts[prompt_id]["text"], provider)
                    if image:
                        path = fileUtils.saveImage(image, prompt_data["concept"], prompt_id)
                        fileUtils.updatePromptStatus(prompt_data, prompt_id, "generated")
                        result = ImageResult(prompt_id, True, "generated", image_path=path)
                    else:
                        fileUtils.updatePromptStatus(prompt_data, prompt_id, failed_status)
                        result = ImageResult(prompt_id, False, failed_status, error=error or "Image generation failed.")
            except Exception as e:
                try:
                    fileUtils.updatePromptStatus(prompt_data, prompt_id, failed_status)
                except Exception:
                    pass
                result = ImageResult(prompt_id, False, failed_status, error=f"Unexpected error: {e}")
            results.append(result)
            if on_progress:
                on_progress(prompt_id, result)
    finally:
        # persist statuses even if the batch is interrupted (e.g. Ctrl+C)
        fileUtils.saveJSON(prompt_data)
    return results
