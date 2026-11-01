"""Request validation and the global generation switch (Checkpoint 2).

Providers are faked and every test counts calls: a rejected request must reach
neither Gemini nor the image provider.
"""
import json
import types
from unittest import mock

import pytest
from PIL import Image

import fileUtils
import generateImage
import generatePrompts
import main
import services
import validation
from conftest import ScriptedProvider
from errors import AppError, GenerationDisabledError, ValidationError

GOOD_JSON = json.dumps({"prompts": [{"text": f"p{i}"} for i in range(1, 13)]})


class GeminiSpy:
    """Stands in for call_gemini and counts how often Gemini would be called."""

    def __init__(self, reply=GOOD_JSON):
        self.reply, self.calls = reply, []

    def __call__(self, prompt_idea, num_prompts=None):
        self.calls.append((prompt_idea, num_prompts))
        return types.SimpleNamespace(text=self.reply)


@pytest.fixture
def gemini(monkeypatch):
    spy = GeminiSpy()
    monkeypatch.setattr(generatePrompts, "call_gemini", spy)
    return spy


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def make_prompt_data(texts=("one", "two", "three")):
    return {"timestamp": "2026-01-01T00:00:00", "concept": "Cafe",
            "prompts": [{"id": i, "text": t, "status": "not generated"} for i, t in enumerate(texts, 1)]}


def image_provider():
    return ScriptedProvider([Image.new("RGB", (4, 4))])


# ---- validation functions
@pytest.mark.parametrize("concept", ["", "   ", "\n\t", None, 42])
def test_empty_or_non_text_concept_rejected(concept):
    with pytest.raises(ValidationError) as exc:
        validation.validate_concept(concept)
    assert exc.value.code == "empty_concept"


def test_concept_at_the_limit_is_accepted_and_one_over_is_rejected():
    validation.validate_concept("a" * 500)
    with pytest.raises(ValidationError) as exc:
        validation.validate_concept("a" * 501)
    assert exc.value.code == "concept_too_long"
    assert exc.value.details == {"length": 501, "limit": 500}
    assert "501" in exc.value.message and "500" in exc.value.message


def test_concept_length_ignores_surrounding_whitespace():
    validation.validate_concept("  " + "a" * 500 + "\n")


def test_concept_limit_follows_configuration(settings):
    settings(MAX_CONCEPT_LENGTH=10)
    validation.validate_concept("a" * 10)
    with pytest.raises(ValidationError):
        validation.validate_concept("a" * 11)


def test_image_prompt_limits():
    validation.validate_image_prompt("a" * 2000)
    with pytest.raises(ValidationError) as exc:
        validation.validate_image_prompt("a" * 2001)
    assert exc.value.code == "image_prompt_too_long"
    for bad in ("", "  ", None):
        with pytest.raises(ValidationError) as exc:
            validation.validate_image_prompt(bad)
        assert exc.value.code == "empty_image_prompt"


def test_prompt_count_defaults_to_the_limit_and_rejects_excess():
    assert validation.validate_prompt_count(None) == 10
    assert validation.validate_prompt_count(3) == 3
    with pytest.raises(ValidationError) as exc:
        validation.validate_prompt_count(11)
    assert exc.value.code == "too_many_prompts"
    for bad in (0, -1, True, 2.5, "3"):
        with pytest.raises(ValidationError) as exc:
            validation.validate_prompt_count(bad)
        assert exc.value.code == "invalid_prompt_count"


def test_image_count_limit():
    validation.validate_image_count(0)
    validation.validate_image_count(2)
    with pytest.raises(ValidationError) as exc:
        validation.validate_image_count(3)
    assert exc.value.code == "too_many_images"
    assert exc.value.details == {"requested": 3, "limit": 2}


def test_generation_switch(settings):
    validation.ensure_generation_enabled()
    settings(GENERATION_ENABLED="false")
    with pytest.raises(GenerationDisabledError):
        validation.ensure_generation_enabled()


def test_errors_are_structured_for_a_web_layer():
    err = ValidationError("too_many_images", "msg", requested=3, limit=2)
    assert err.http_status == 422
    assert err.to_dict() == {"code": "too_many_images", "message": "msg", "details": {"requested": 3, "limit": 2}}
    off = GenerationDisabledError()
    assert off.http_status == 503 and off.code == "generation_disabled"
    assert "temporarily unavailable" in off.message
    assert isinstance(off, AppError) and isinstance(err, AppError)


# ---- expanding ideas
@pytest.mark.parametrize("idea,code", [("", "empty_concept"), ("   ", "empty_concept"),
                                       ("x" * 501, "concept_too_long")])
def test_expand_idea_rejects_invalid_ideas_without_calling_gemini(workdir, gemini, idea, code):
    result = services.expand_idea(idea)
    assert not result.ok and result.error_code == code and result.error
    assert gemini.calls == []
    assert fileUtils.getOutputFiles() == []


def test_expand_idea_rejects_too_many_prompts_without_calling_gemini(workdir, gemini):
    result = services.expand_idea("a cat", num_prompts=11)
    assert result.error_code == "too_many_prompts"
    assert gemini.calls == []


def test_expand_idea_does_not_truncate_the_idea(workdir, gemini):
    idea = "a" * 500
    assert services.expand_idea(idea).ok
    assert gemini.calls[0][0] == idea


def test_expand_idea_asks_gemini_for_the_configured_number_and_keeps_that_many(workdir, gemini, settings):
    settings(MAX_PROMPTS_PER_REQUEST=4)
    result = services.expand_idea("a cat")
    assert gemini.calls == [("a cat", 4)]
    assert result.ok and [p["id"] for p in result.data["prompts"]] == [1, 2, 3, 4]


def test_gemini_prompt_text_uses_the_requested_count(monkeypatch):
    sent = []
    fake_client = types.SimpleNamespace(models=types.SimpleNamespace(
        generate_content=lambda **kw: sent.append(kw["contents"]) or types.SimpleNamespace(text="{}")))
    monkeypatch.setattr(generatePrompts, "get_client", lambda: fake_client)
    generatePrompts.call_gemini("a cat", num_prompts=3)
    assert "create 3 detailed" in sent[0]


def test_expand_idea_when_generation_disabled(workdir, gemini, settings):
    settings(GENERATION_ENABLED="false")
    result = services.expand_idea("a cat")
    assert not result.ok and result.error_code == "generation_disabled"
    assert "temporarily unavailable" in result.error
    assert gemini.calls == [] and fileUtils.getOutputFiles() == []


def test_expand_ideas_when_disabled_raises_before_any_call(workdir, gemini, settings):
    settings(GENERATION_ENABLED="false")
    with pytest.raises(GenerationDisabledError):
        services.expand_ideas(["a", "b"], delay=0)
    assert gemini.calls == []


def test_expand_ideas_still_isolates_an_invalid_idea(workdir, gemini):
    results = services.expand_ideas(["a cat", "", "x" * 501, "a dog"], delay=0)
    assert [r.ok for r in results] == [True, False, False, True]
    assert [c[0] for c in gemini.calls] == ["a cat", "a dog"]


@pytest.mark.parametrize("idea", ["", "x" * 501])
def test_generate_prompt_text_cannot_be_used_to_bypass_validation(gemini, idea):
    text, error = generatePrompts.generate_prompt_text(idea)
    assert text is None and error
    assert gemini.calls == []


def test_generate_prompt_text_cannot_bypass_the_switch(gemini, settings):
    settings(GENERATION_ENABLED="false")
    text, error = generatePrompts.generate_prompt_text("a cat")
    assert text is None and "temporarily unavailable" in error
    assert gemini.calls == []


def test_real_gemini_client_is_never_built_for_rejected_requests(monkeypatch, settings):
    """conftest blocks genai.Client; a rejected request must not even try to build it."""
    monkeypatch.setattr(generatePrompts, "_client", None)
    settings(GENERATION_ENABLED="false")
    text, error = generatePrompts.generate_prompt_text("a cat")
    assert text is None and "temporarily unavailable" in error


# ---- generating images
def test_too_many_images_raises_before_any_provider_call(workdir):
    data = make_prompt_data()
    provider = image_provider()
    with pytest.raises(ValidationError) as exc:
        services.generate_images(data, [1, 2, 3], provider=provider)
    assert exc.value.code == "too_many_images"
    assert provider.calls == 0
    assert [p["status"] for p in data["prompts"]] == ["not generated"] * 3
    assert not (workdir / "output").exists()


def test_selection_is_not_silently_truncated(workdir):
    provider = image_provider()
    with pytest.raises(ValidationError):
        services.generate_images(make_prompt_data(), [1, 2, 3], provider=provider)
    assert provider.calls == 0  # not even the first two were generated


def test_selection_at_the_limit_is_generated(workdir):
    provider = image_provider()
    results = services.generate_images(make_prompt_data(), [1, 2], provider=provider)
    assert [r.ok for r in results] == [True, True] and provider.calls == 2


def test_duplicate_ids_count_toward_the_limit(workdir):
    provider = image_provider()
    with pytest.raises(ValidationError):
        services.generate_images(make_prompt_data(), [1, 1, 1], provider=provider)
    assert provider.calls == 0


def test_over_long_image_prompt_rejects_the_whole_request(workdir):
    data = make_prompt_data(("short", "x" * 2001))
    provider = image_provider()
    with pytest.raises(ValidationError) as exc:
        services.generate_images(data, [1, 2], provider=provider)
    assert exc.value.code == "image_prompt_too_long" and "Prompt 2" in exc.value.message
    assert provider.calls == 0  # prompt 1 was valid but nothing runs


def test_prompts_that_were_not_selected_are_not_checked(workdir):
    provider = image_provider()
    results = services.generate_images(make_prompt_data(("short", "x" * 2001)), [1], provider=provider)
    assert results[0].ok and provider.calls == 1


def test_image_generation_when_disabled_raises_and_changes_nothing(workdir, settings):
    settings(GENERATION_ENABLED="false")
    data = make_prompt_data()
    provider = image_provider()
    with pytest.raises(GenerationDisabledError):
        services.generate_images(data, [1], provider=provider)
    assert provider.calls == 0
    assert data["prompts"][0]["status"] == "not generated"


def test_empty_selection_is_allowed_even_when_disabled(workdir, settings):
    settings(GENERATION_ENABLED="false")
    assert services.generate_images(make_prompt_data(), [], provider=image_provider()) == []


def test_unknown_prompt_id_is_still_a_per_item_error(workdir):
    provider = image_provider()
    results = services.generate_images(make_prompt_data(), [99, 1], provider=provider)
    assert [r.ok for r in results] == [False, True] and provider.calls == 1


def test_generate_image_cannot_be_used_to_bypass_validation(settings):
    provider = image_provider()
    image, error = generateImage.generate_image_with_error("x" * 2001, provider)
    assert image is None and "2001" in error
    image, error = generateImage.generate_image_with_error("  ", provider)
    assert image is None and error
    settings(GENERATION_ENABLED="false")
    image, error = generateImage.generate_image_with_error("fine", provider)
    assert image is None and "temporarily unavailable" in error
    assert provider.calls == 0


def test_real_hugging_face_client_is_never_touched_when_rejected(settings, monkeypatch):
    monkeypatch.delenv("HF_TOKEN", raising=False)
    settings(GENERATION_ENABLED="false")
    provider = generateImage.HuggingFaceImageProvider()
    image, error = generateImage.generate_image_with_error("fine", provider)
    assert image is None and "temporarily unavailable" in error
    assert provider._client is None  # never even built, so the missing token was never noticed


# ---- viewing still works while generation is off
def test_viewing_existing_files_works_when_disabled(workdir, settings):
    path = fileUtils.saveJSON(make_prompt_data())
    settings(GENERATION_ENABLED="false")
    name = path.split("/")[-1]
    assert services.list_prompt_files() == [name]
    assert services.load_prompt_file(name)["concept"] == "Cafe"


# ---- CLI
class Q:
    def __init__(self, v):
        self.v = v

    def ask(self):
        return self.v


@pytest.fixture
def no_pause(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a: "")


def test_cli_checkbox_rejects_more_than_the_limit_with_a_clear_message(monkeypatch, no_pause):
    captured = {}

    def checkbox(*args, **kwargs):
        captured.update(kwargs)
        return Q([1])

    monkeypatch.setattr(main.questionary, "select", lambda *a, **k: Q("f.json"))
    monkeypatch.setattr(main.questionary, "checkbox", checkbox)
    monkeypatch.setattr(services, "list_prompt_files", lambda: ["f.json"])
    monkeypatch.setattr(services, "load_prompt_file", lambda name: make_prompt_data())
    with mock.patch("services.generate_images", return_value=[]):
        main.process_image_generation()
    validate = captured["validate"]
    assert validate([1, 2]) is True
    message = validate([1, 2, 3])
    assert "3" in message and "limit is 2" in message


def test_cli_shows_service_rejection_instead_of_crashing(monkeypatch, no_pause, capsys):
    monkeypatch.setattr(main.questionary, "select", lambda *a, **k: Q("f.json"))
    monkeypatch.setattr(main.questionary, "checkbox", lambda *a, **k: Q([1, 2, 3]))
    monkeypatch.setattr(services, "list_prompt_files", lambda: ["f.json"])
    monkeypatch.setattr(services, "load_prompt_file", lambda name: make_prompt_data())
    provider = image_provider()
    monkeypatch.setattr(generateImage, "get_provider", lambda name=None: provider)
    main.process_image_generation()
    out = capsys.readouterr().out
    assert "limit is 2" in out and provider.calls == 0


def test_cli_image_generation_when_disabled_stops_before_asking_anything(monkeypatch, no_pause, capsys, settings):
    settings(GENERATION_ENABLED="false")
    asked = []
    monkeypatch.setattr(main.questionary, "select", lambda *a, **k: asked.append("select") or Q(None))
    main.process_image_generation()
    assert asked == []
    assert "temporarily unavailable" in capsys.readouterr().out


def test_cli_single_idea_shows_validation_message(monkeypatch, no_pause, capsys, gemini):
    monkeypatch.setattr("builtins.input", lambda *a: "x" * 501)
    main.process_single_idea()
    assert "limit is 500" in capsys.readouterr().out
    assert gemini.calls == []


def test_cli_single_idea_when_disabled(monkeypatch, no_pause, capsys, gemini, settings):
    settings(GENERATION_ENABLED="false")
    monkeypatch.setattr("builtins.input", lambda *a: "a cat")
    main.process_single_idea()
    assert "temporarily unavailable" in capsys.readouterr().out
    assert gemini.calls == []


def test_cli_batch_mode_when_disabled(monkeypatch, no_pause, capsys, gemini, settings, tmp_path):
    f = tmp_path / "ideas.txt"
    f.write_text("a\nb\n", encoding="utf-8")
    settings(GENERATION_ENABLED="false")
    monkeypatch.setattr(main.questionary, "text", lambda *a, **k: Q(str(f)))
    main.process_batch_mode()
    assert "temporarily unavailable" in capsys.readouterr().out
    assert gemini.calls == []


# ---- a maximum-length idea must still be savable (file names are shortened, the concept is not)
def test_long_concept_gives_a_short_file_name_but_keeps_the_full_concept(workdir):
    concept = "a" * 500
    data = make_prompt_data()
    data["concept"] = concept
    path = fileUtils.saveJSON(data)
    assert len(path.split("/")[-1]) < 120
    assert fileUtils.getJSONObject(path)["concept"] == concept
    image_path = fileUtils.saveImage(Image.new("RGB", (4, 4)), concept, 1)
    assert len(image_path.split("/")[-1]) < 140
