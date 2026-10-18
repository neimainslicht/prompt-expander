import sys, json, types, os, glob
from unittest import mock
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import pytest, questionary
from PIL import Image
import fileUtils, generateImage

import main

class Q:
    def __init__(s, v): s.v = v
    def ask(s): return s.v

class FakeProvider(generateImage.ImageProvider):
    def __init__(s, fail_on=(), retryable=False):
        s.fail_on, s.retryable, s.calls = set(fail_on), retryable, []
    def generate(s, prompt):
        s.calls.append(prompt)
        if prompt in s.fail_on: raise RuntimeError("api down")
        return Image.new("RGB", (4, 4))
    def is_retryable(s, e): return s.retryable

@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(generateImage, "WAIT_MAX", 0)
    monkeypatch.setattr(generateImage, "WAIT_MIN", 0)
    monkeypatch.setattr(generateImage, "WAIT_MULTIPLIER", 0)
    monkeypatch.setattr("builtins.input", lambda *a: "")
    obj = {"timestamp": "2026-01-01T00:00:00", "concept": "Cafe", "prompts":
           [{"id": i, "text": f"t{i}", "status": "not generated"} for i in (1, 2, 3)]}
    fileUtils.saveJSON(obj)
    path = glob.glob("output/*.json")[0]
    return types.SimpleNamespace(path=path, obj=obj)

def run(monkeypatch, env, provider, pick, file="choose"):
    name = os.path.basename(env.path) if file == "choose" else file
    monkeypatch.setattr(main.questionary, "select", lambda *a, **k: Q(name))
    monkeypatch.setattr(main.questionary, "checkbox", lambda *a, **k: Q(pick))
    monkeypatch.setattr(generateImage, "get_provider", lambda n=None: provider)
    main.process_image_generation()
    return json.load(open(env.path, encoding="utf-8"))

def statuses(d): return {p["id"]: p["status"] for p in d["prompts"]}
def images(): return glob.glob("output/images/*.png")

# ---- empty selections
def test_empty_list_selected(monkeypatch, env):
    p = FakeProvider()
    d = run(monkeypatch, env, p, [])
    assert p.calls == [] and images() == []
    assert set(statuses(d).values()) == {"not generated"}

def test_checkbox_cancelled_none(monkeypatch, env):
    p = FakeProvider()
    d = run(monkeypatch, env, p, None)
    assert p.calls == [] and set(statuses(d).values()) == {"not generated"}

def test_file_menu_cancelled(monkeypatch, env):
    # select() returns None on Ctrl+C
    p = FakeProvider()
    run(monkeypatch, env, p, [], file=None)
    assert p.calls == []

def test_corrupt_json_file(monkeypatch, env):
    open(env.path, "w").write("{not json")
    p = FakeProvider()
    monkeypatch.setattr(main.questionary, "select", lambda *a, **k: Q(os.path.basename(env.path)))
    monkeypatch.setattr(generateImage, "get_provider", lambda n=None: p)
    main.process_image_generation()  # must return cleanly, not raise
    assert p.calls == []
    assert open(env.path).read() == "{not json"  # file left untouched

def test_no_output_files(monkeypatch, env):
    os.remove(env.path)
    monkeypatch.setattr(main.questionary, "select", lambda *a, **k: Q(None))
    main.process_image_generation()

# ---- API failures through real retry path
def test_non_retryable_failure_isolated(monkeypatch, env):
    p = FakeProvider(fail_on={"t2"})
    d = run(monkeypatch, env, p, env.obj["prompts"])
    assert statuses(d) == {1: "generated", 2: "failed", 3: "generated"}
    assert len(images()) == 2 and p.calls.count("t2") == 1

def test_retryable_failure_exhausts_then_continues(monkeypatch, env):
    p = FakeProvider(fail_on={"t1"}, retryable=True)
    d = run(monkeypatch, env, p, env.obj["prompts"])
    assert statuses(d) == {1: "failed", 2: "generated", 3: "generated"}
    assert p.calls.count("t1") == generateImage.MAX_ATTEMPTS

def test_all_fail(monkeypatch, env):
    p = FakeProvider(fail_on={"t1", "t2", "t3"})
    d = run(monkeypatch, env, p, env.obj["prompts"])
    assert set(statuses(d).values()) == {"failed"} and images() == []

def test_provider_init_failure(monkeypatch, env):
    def boom(n=None): raise RuntimeError("HF_TOKEN is not set.")
    monkeypatch.setattr(main.questionary, "select", lambda *a, **k: Q(os.path.basename(env.path)))
    monkeypatch.setattr(main.questionary, "checkbox", lambda *a, **k: Q(env.obj["prompts"]))
    monkeypatch.setattr(generateImage, "get_provider", boom)
    main.process_image_generation()
    d = json.load(open(env.path, encoding="utf-8"))
    assert set(statuses(d).values()) == {"failed"}

# ---- reruns
def test_rerun_failed_then_success(monkeypatch, env):
    run(monkeypatch, env, FakeProvider(fail_on={"t2"}), env.obj["prompts"])
    d = json.load(open(env.path, encoding="utf-8"))
    failed = [p for p in d["prompts"] if p["status"] == "failed"]
    d2 = run(monkeypatch, env, FakeProvider(), failed)
    assert statuses(d2) == {1: "generated", 2: "generated", 3: "generated"}
    assert len(images()) == 3

def test_rerun_generated_success_keeps_both_images(monkeypatch, env):
    # freeze the clock so both runs land in the same second (same timestamp)
    with mock.patch("fileUtils.datetime") as dt:
        dt.now.return_value.strftime.return_value = "20260101_000000"
        run(monkeypatch, env, FakeProvider(), [env.obj["prompts"][0]])
        d = run(monkeypatch, env, FakeProvider(), [env.obj["prompts"][0]])
        run(monkeypatch, env, FakeProvider(), [env.obj["prompts"][0]])
    names = sorted(os.path.basename(f) for f in images())
    assert len(names) == 3 and len(set(names)) == 3
    assert statuses(d)[1] == "generated"

def test_rerun_generated_then_api_fails_keeps_status(monkeypatch, env):
    run(monkeypatch, env, FakeProvider(), [env.obj["prompts"][0]])
    d = run(monkeypatch, env, FakeProvider(fail_on={"t1"}), [env.obj["prompts"][0]])
    # image from first run still exists, so status shouldn't regress
    assert len(images()) == 1
    assert statuses(d)[1] == "generated"
