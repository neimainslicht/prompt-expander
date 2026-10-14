"""Integration tests: real HuggingFaceImageProvider + real InferenceClient + real fileUtils.

Only the network call (InferenceClient.text_to_image) is replaced, except for the
opt-in live test which needs RUN_LIVE_HF=1 and a valid HF_TOKEN (spends credits).
"""
import json
import os

import pytest
from PIL import Image

import fileUtils
import generateImage as g
from conftest import make_http_error


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """fileUtils uses relative output/ paths, so run inside a temp dir."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HF_TOKEN", "hf_fake")
    return tmp_path


def fake_image():
    return Image.new("RGB", (8, 8), "red")


def run_generation_loop(prompt_json, provider=None):
    """Mirrors main.process_image_generation (main.py runs its menu on import)."""
    for prompt in prompt_json["prompts"]:
        image = g.generate_image(prompt["text"], provider)
        if image:
            fileUtils.saveImage(image, prompt_json["concept"], prompt["id"])
            fileUtils.updatePromptStatus(prompt_json, prompt["id"], "generated")
        else:
            fileUtils.updatePromptStatus(prompt_json, prompt["id"], "failed")
    fileUtils.saveJSON(prompt_json)


def make_prompt_json(n=3):
    raw = json.dumps({"prompts": [{"text": f"prompt {i}"} for i in range(n)]})
    return fileUtils.parseJSON(raw, "Test Concept")


def test_real_provider_retries_through_real_client(workdir, monkeypatch):
    calls = []

    def flaky(self, prompt, **kwargs):
        calls.append((prompt, kwargs))
        if len(calls) < 3:
            raise make_http_error(503)
        return fake_image()

    monkeypatch.setattr(g.InferenceClient, "text_to_image", flaky)
    image = g.generate_image("a cat", g.HuggingFaceImageProvider())
    assert isinstance(image, Image.Image)
    assert len(calls) == 3
    assert calls[0][1]["model"] == "black-forest-labs/FLUX.1-dev"


def test_real_client_receives_provider_key_and_timeout(workdir, monkeypatch):
    monkeypatch.setattr(g.InferenceClient, "text_to_image", lambda self, p, **kw: fake_image())
    provider = g.HuggingFaceImageProvider(provider="fal-ai", timeout=33)
    g.generate_image("a cat", provider)
    assert provider.client.provider == "fal-ai"
    assert provider.client.timeout == 33


def test_full_flow_mixed_results_persist_statuses(workdir, monkeypatch):
    outcomes = {
        "prompt 0": [fake_image()],
        "prompt 1": [make_http_error(401)],                        # non-retryable -> failed
        "prompt 2": [make_http_error(429), make_http_error(503), fake_image()],  # recovers
    }

    def scripted(self, prompt, **kwargs):
        item = outcomes[prompt].pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(g.InferenceClient, "text_to_image", scripted)
    data = make_prompt_json()
    run_generation_loop(data, g.HuggingFaceImageProvider())

    saved = list((workdir / "output").glob("*.json"))
    assert len(saved) == 1
    statuses = [p["status"] for p in json.loads(saved[0].read_text(encoding="utf-8"))["prompts"]]
    assert statuses == ["generated", "failed", "generated"]
    assert len(list((workdir / "output" / "images").glob("*.png"))) == 2


def test_loop_survives_exhausted_retries(workdir, monkeypatch):
    def always_429(self, prompt, **kwargs):
        raise make_http_error(429)

    monkeypatch.setattr(g.InferenceClient, "text_to_image", always_429)
    data = make_prompt_json(2)
    run_generation_loop(data, g.HuggingFaceImageProvider())  # must not raise
    assert [p["status"] for p in data["prompts"]] == ["failed", "failed"]
    assert not (workdir / "output" / "images").exists()


def test_missing_token_marks_failed_without_crashing(workdir, monkeypatch):
    monkeypatch.delenv("HF_TOKEN", raising=False)
    data = make_prompt_json(1)
    run_generation_loop(data, g.HuggingFaceImageProvider())
    assert data["prompts"][0]["status"] == "failed"


def test_swapping_in_another_provider(workdir, monkeypatch):
    """A new provider only needs the ImageProvider interface; retry layer is reused."""
    class OtherProvider(g.ImageProvider):
        def __init__(self):
            self.calls = 0

        def generate(self, prompt):
            self.calls += 1
            if self.calls == 1:
                raise ConnectionResetError("flaky")
            return fake_image()

        def is_retryable(self, e):
            return isinstance(e, ConnectionResetError)

    monkeypatch.setitem(g.PROVIDERS, "other", OtherProvider)
    monkeypatch.setenv("IMAGE_PROVIDER", "other")
    data = make_prompt_json(1)
    run_generation_loop(data)  # provider resolved from env
    assert data["prompts"][0]["status"] == "generated"


@pytest.mark.skipif(
    os.getenv("RUN_LIVE_HF") != "1" or not os.getenv("HF_TOKEN"),
    reason="live HF call; set RUN_LIVE_HF=1 and HF_TOKEN (spends credits)",
)
def test_live_hugging_face(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    image = g.generate_image("a small red circle on a white background")
    assert isinstance(image, Image.Image)
