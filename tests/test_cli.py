"""CLI tests: main.py only collects input, calls services and prints. Services are mocked."""
from unittest import mock

import pytest

import main
import services


class Q:
    def __init__(self, v):
        self.v = v

    def ask(self):
        return self.v


@pytest.fixture(autouse=True)
def no_pause(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a: "")


DATA = {"concept": "Cafe", "prompts": [{"id": 1, "text": "t1", "status": "not generated"},
                                       {"id": 2, "text": "t2", "status": "not generated"}]}


def pick(monkeypatch, file="f.json", ids=(1,)):
    monkeypatch.setattr(main.questionary, "select", lambda *a, **k: Q(file))
    monkeypatch.setattr(main.questionary, "checkbox", lambda *a, **k: Q(ids))
    monkeypatch.setattr(services, "list_prompt_files", lambda: ["f.json"])
    monkeypatch.setattr(services, "load_prompt_file", lambda name: DATA)


def test_image_generation_passes_selected_ids(monkeypatch):
    pick(monkeypatch, ids=[2])
    with mock.patch("services.generate_images", return_value=[]) as gen:
        main.process_image_generation()
    assert gen.call_args.args == (DATA, [2])


@pytest.mark.parametrize("checkbox_value", [None, []])
def test_image_generation_empty_checkbox(monkeypatch, checkbox_value):
    pick(monkeypatch, ids=checkbox_value)
    with mock.patch("services.generate_images", return_value=[]) as gen:
        main.process_image_generation()
    assert gen.call_args.args[1] == []


def test_image_generation_file_menu_cancelled(monkeypatch):
    pick(monkeypatch, file=None)
    with mock.patch("services.generate_images") as gen:
        main.process_image_generation()
    gen.assert_not_called()


def test_image_generation_unreadable_file(monkeypatch):
    pick(monkeypatch)
    monkeypatch.setattr(services, "load_prompt_file", lambda name: None)
    with mock.patch("services.generate_images") as gen:
        main.process_image_generation()
    gen.assert_not_called()


def test_image_generation_no_files(monkeypatch):
    pick(monkeypatch)
    monkeypatch.setattr(services, "list_prompt_files", lambda: [])
    with mock.patch("services.generate_images") as gen:
        main.process_image_generation()
    gen.assert_not_called()


def test_image_generation_prints_failure_reason(monkeypatch, capsys):
    pick(monkeypatch, ids=[1])

    def fake(data, ids, on_progress=None):
        on_progress(1, None)
        on_progress(1, services.ImageResult(1, False, "failed", error="Inference credits exhausted"))
        return []
    monkeypatch.setattr(services, "generate_images", fake)
    main.process_image_generation()
    assert "Inference credits exhausted" in capsys.readouterr().out


def test_batch_mode_works_without_running_single_idea_first(monkeypatch, tmp_path):
    f = tmp_path / "ideas.txt"
    f.write_text("a\nb\n", encoding="utf-8")
    monkeypatch.setattr(main.questionary, "text", lambda *a, **k: Q(str(f)))
    with mock.patch("services.expand_ideas", return_value=[]) as expand:
        main.process_batch_mode()
    assert expand.call_args.args[0] == ["a", "b"]


def test_batch_mode_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(main.questionary, "text", lambda *a, **k: Q(str(tmp_path / "nope.txt")))
    with mock.patch("services.expand_ideas") as expand:
        main.process_batch_mode()
    expand.assert_not_called()


def test_single_idea_prints_error(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda *a: "a dog")
    monkeypatch.setattr(services, "expand_idea",
                        lambda idea: services.ExpandResult(idea, False, error="Gemini API failed with status 500"))
    main.process_single_idea()
    assert "status 500" in capsys.readouterr().out
