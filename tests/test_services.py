"""Service-layer tests. Every API call is faked; conftest blocks real ones."""
import glob
import json
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock

import pytest
from PIL import Image

import fileUtils
import generateImage
import generatePrompts
import services

ROOT = Path(__file__).resolve().parent.parent


class FakeProvider(generateImage.ImageProvider):
    def __init__(self, fail_on=(), retryable=False):
        self.fail_on, self.retryable, self.calls = set(fail_on), retryable, []

    def generate(self, prompt):
        self.calls.append(prompt)
        if prompt in self.fail_on:
            raise RuntimeError("api down")
        return Image.new("RGB", (4, 4))

    def is_retryable(self, e):
        return self.retryable


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(generateImage, "WAIT_MAX", 0)
    monkeypatch.setattr(generateImage, "WAIT_MIN", 0)
    monkeypatch.setattr(generateImage, "WAIT_MULTIPLIER", 0)
    obj = {"timestamp": "2026-01-01T00:00:00", "concept": "Cafe", "prompts":
           [{"id": i, "text": f"t{i}", "status": "not generated"} for i in (1, 2, 3)]}
    path = fileUtils.saveJSON(obj)
    return types.SimpleNamespace(path=path, name=os.path.basename(path), obj=obj)


def reload(env):
    return json.load(open(env.path, encoding="utf-8"))


def run(env, provider, ids):
    """Load the file from disk, as a caller would, then generate."""
    data = services.load_prompt_file(env.name)
    return services.generate_images(data, ids, provider=provider)


def statuses(d):
    return {p["id"]: p["status"] for p in d["prompts"]}


def images():
    return glob.glob("output/images/*.png")


# ---- generate_images: empty selections
@pytest.mark.parametrize("ids", [[], None])
def test_empty_selection(env, ids):
    p = FakeProvider()
    assert run(env, p, ids) == []
    assert p.calls == [] and images() == []
    assert set(statuses(reload(env)).values()) == {"not generated"}


def test_unknown_id_is_a_failed_result_not_a_crash(env):
    results = run(env, FakeProvider(), [99, 1])
    assert [(r.prompt_id, r.ok) for r in results] == [(99, False), (1, True)]
    assert "99" in results[0].error
    assert statuses(reload(env)) == {1: "generated", 2: "not generated", 3: "not generated"}


# ---- generate_images: API failures through the real retry path
def test_non_retryable_failure_isolated(env):
    p = FakeProvider(fail_on={"t2"})
    results = run(env, p, [1, 2, 3])
    assert statuses(reload(env)) == {1: "generated", 2: "failed", 3: "generated"}
    assert [r.ok for r in results] == [True, False, True]
    assert results[1].error and results[1].image_path is None
    assert results[0].image_path and os.path.exists(results[0].image_path)
    assert len(images()) == 2 and p.calls.count("t2") == 1


def test_retryable_failure_exhausts_then_continues(env):
    p = FakeProvider(fail_on={"t1"}, retryable=True)
    run(env, p, [1, 2, 3])
    assert statuses(reload(env)) == {1: "failed", 2: "generated", 3: "generated"}
    assert p.calls.count("t1") == generateImage.MAX_ATTEMPTS


def test_all_fail(env):
    run(env, FakeProvider(fail_on={"t1", "t2", "t3"}), [1, 2, 3])
    assert set(statuses(reload(env)).values()) == {"failed"} and images() == []


def test_provider_init_failure(env, monkeypatch):
    def boom(name=None):
        raise RuntimeError("HF_TOKEN is not set.")
    monkeypatch.setattr(generateImage, "get_provider", boom)
    results = run(env, None, [1, 2, 3])
    assert set(statuses(reload(env)).values()) == {"failed"}
    assert all("HF_TOKEN is not set." in r.error for r in results)


def test_save_failure_isolated(env, monkeypatch):
    real = fileUtils.saveImage

    def flaky(image, concept, prompt_id):
        if prompt_id == 2:
            raise OSError("disk full")
        return real(image, concept, prompt_id)
    monkeypatch.setattr(fileUtils, "saveImage", flaky)
    results = run(env, FakeProvider(), [1, 2, 3])
    assert statuses(reload(env)) == {1: "generated", 2: "failed", 3: "generated"}
    assert "disk full" in results[1].error


def test_progress_callback_sees_start_and_result(env):
    events = []
    data = services.load_prompt_file(env.name)
    services.generate_images(data, [1], provider=FakeProvider(),
                             on_progress=lambda pid, r: events.append((pid, r is None)))
    assert events == [(1, True), (1, False)]


# ---- generate_images: reruns
def test_rerun_failed_then_success(env):
    run(env, FakeProvider(fail_on={"t2"}), [1, 2, 3])
    failed = [p["id"] for p in reload(env)["prompts"] if p["status"] == "failed"]
    run(env, FakeProvider(), failed)
    assert statuses(reload(env)) == {1: "generated", 2: "generated", 3: "generated"}
    assert len(images()) == 3


def test_rerun_generated_success_keeps_all_images(env):
    # freeze the clock so every run lands in the same second (same timestamp)
    with mock.patch("fileUtils.datetime") as dt:
        dt.now.return_value.strftime.return_value = "20260101_000000"
        for _ in range(3):
            run(env, FakeProvider(), [1])
    names = [os.path.basename(f) for f in images()]
    assert len(names) == 3 and len(set(names)) == 3
    assert statuses(reload(env))[1] == "generated"


def test_rerun_generated_then_api_fails_keeps_status(env):
    run(env, FakeProvider(), [1])
    results = run(env, FakeProvider(fail_on={"t1"}), [1])
    assert len(images()) == 1  # earlier image still on disk
    assert statuses(reload(env))[1] == "generated"
    assert results[0].ok is False and results[0].status == "generated"


# ---- prompt files
def test_list_and_load_prompt_files(env):
    assert services.list_prompt_files() == [env.name]
    assert services.load_prompt_file(env.name)["concept"] == "Cafe"


def test_load_prompt_file_missing_or_corrupt(env):
    assert services.load_prompt_file("nope.json") is None
    open(env.path, "w").write("{not json")
    assert services.load_prompt_file(env.name) is None
    assert open(env.path).read() == "{not json"


def test_load_prompt_file_rejects_paths_outside_output(env):
    Path("secret.json").write_text("{}")
    assert services.load_prompt_file("../secret.json") is None


# ---- ideas
def test_parse_ideas_skips_blank_lines():
    assert services.parse_ideas("a cat\n\n  \n  a dog  \n") == ["a cat", "a dog"]


def test_read_ideas_file(tmp_path):
    f = tmp_path / "ideas.txt"
    f.write_text("one\n\ntwo\n", encoding="utf-8")
    assert services.read_ideas_file(f) == ["one", "two"]


GOOD = json.dumps({"prompts": [{"text": "p1"}, {"text": "p2"}]})


def stub_gemini(monkeypatch, replies):
    """replies maps idea -> (text, error)."""
    monkeypatch.setattr(generatePrompts, "generate_prompt_text",
                        lambda idea: replies.get(idea, (None, "Empty prompt entered.")))


def test_expand_idea_success_saves_file(env, monkeypatch):
    stub_gemini(monkeypatch, {"a dog": (GOOD, None)})
    r = services.expand_idea("a dog")
    assert r.ok and r.error is None
    assert os.path.exists(r.prompt_file)
    assert [p["text"] for p in r.data["prompts"]] == ["p1", "p2"]
    assert r.data["concept"] == "a dog"


def test_expand_idea_api_error(env, monkeypatch):
    stub_gemini(monkeypatch, {"x": (None, "Gemini API failed with status 500")})
    before = glob.glob("output/*.json")
    r = services.expand_idea("x")
    assert not r.ok and "500" in r.error and r.prompt_file is None
    assert glob.glob("output/*.json") == before


def test_expand_idea_malformed_response(env, monkeypatch):
    stub_gemini(monkeypatch, {"x": ("not json", None)})
    r = services.expand_idea("x")
    assert not r.ok and r.error


def test_expand_ideas_failure_isolated_and_each_line_is_its_own_concept(env, monkeypatch):
    stub_gemini(monkeypatch, {"a": (GOOD, None), "c": (GOOD, None), "b": (None, "boom")})
    seen = []
    results = services.expand_ideas(["a", "b", "c"], delay=0,
                                    on_progress=lambda i, r: seen.append((i, r is None)))
    assert [r.ok for r in results] == [True, False, True]
    assert [r.data["concept"] for r in results if r.ok] == ["a", "c"]
    assert seen == [("a", True), ("a", False), ("b", True), ("b", False), ("c", True), ("c", False)]


def test_expand_ideas_delays_between_calls_only(env, monkeypatch):
    stub_gemini(monkeypatch, {"a": (GOOD, None), "b": (GOOD, None)})
    with mock.patch("services.time.sleep") as sleep:
        services.expand_ideas(["a", "b"], delay=1.5)
    assert sleep.call_args_list == [mock.call(1.5)]


# ---- keys and imports
def test_importing_modules_does_not_need_api_keys():
    clean_env = {k: v for k, v in os.environ.items() if k not in ("API_KEY", "HF_TOKEN")}
    clean_env["PYTHONPATH"] = str(ROOT)
    # run from a dir without a .env so load_dotenv can't supply a key
    with tempfile.TemporaryDirectory() as d:
        r = subprocess.run([sys.executable, "-c", "import services, generatePrompts, generateImage, main"],
                           cwd=d, env=clean_env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_missing_api_key_is_an_error_result_not_an_exception(monkeypatch):
    monkeypatch.setattr(generatePrompts, "_client", None)
    monkeypatch.delenv("API_KEY", raising=False)
    text, error = generatePrompts.generate_prompt_text("a cat")
    assert text is None and "API_KEY" in error


def test_services_module_is_ui_and_key_free():
    src = (ROOT / "services.py").read_text(encoding="utf-8")
    for banned in ("questionary", "input(", "print(", "getenv", "environ", "API_KEY", "HF_TOKEN"):
        assert banned not in src, banned
