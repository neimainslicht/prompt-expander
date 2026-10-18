"""Application workflows, independent of any user interface.

The CLI (main.py) and any future web backend should call these functions and
never talk to the API providers directly. Nothing here prompts, prints or reads
API keys: keys and provider-specific code stay in generatePrompts.py and
generateImage.py, and expected failures come back as results, not exceptions.
"""
import time
from dataclasses import dataclass
from typing import Callable, Optional

import fileUtils
import generateImage
import generatePrompts


@dataclass
class ExpandResult:
    idea: str
    ok: bool
    prompt_file: Optional[str] = None  # path of the saved JSON
    data: Optional[dict] = None
    error: Optional[str] = None


@dataclass
class ImageResult:
    prompt_id: int
    ok: bool
    status: str  # status stored in the prompt file after this attempt
    image_path: Optional[str] = None
    error: Optional[str] = None


def expand_idea(idea) -> ExpandResult:
    """Turn one idea into a saved file of detailed image prompts."""
    text, error = generatePrompts.generate_prompt_text(idea)
    if error:
        return ExpandResult(idea=idea, ok=False, error=error)
    try:
        data = fileUtils.parseJSON(text, idea)
        path = fileUtils.saveJSON(data)
    except Exception as e:
        return ExpandResult(idea=idea, ok=False, error=f"Could not process the model response: {e}")
    return ExpandResult(idea=idea, ok=True, prompt_file=path, data=data)


def expand_ideas(ideas, delay=1.5, on_progress: Optional[Callable] = None) -> list:
    """Expand each idea as its own concept. One failure never stops the rest.

    on_progress(idea, None) is called before each idea and on_progress(idea, result) after.
    """
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
    """
    results = []
    prompts = {p["id"]: p for p in prompt_data["prompts"]}
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
