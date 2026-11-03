"""Persistent usage tracking (Checkpoint 3). Providers are always fake; no real calls."""
import json
import threading
import types
from pathlib import Path

import pytest
import tenacity
from google.genai.errors import APIError
from PIL import Image

import config
import fileUtils
import generateImage
import generatePrompts
import services
import usage
from conftest import ScriptedProvider, make_http_error
from errors import UsageUnavailableError, ValidationError
from usage import SqliteUsageStore

SENTINEL_TEXT = "SENTINEL-PROMPT-TEXT-ZZZ"
FAKE_TOKEN = "hf" + "_" + "FAKE" * 4 + "1234567890"  # assembled so no key-shaped literal is in the source


@pytest.fixture
def store(tmp_path):
    return SqliteUsageStore(tmp_path / "usage_test.db")


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def img():
    return Image.new("RGB", (4, 4))


def prompt_data(n=2, text=SENTINEL_TEXT):
    return {"timestamp": "2026-01-01T00:00:00", "concept": "Cafe",
            "prompts": [{"id": i, "text": f"{text} {i}", "status": "not generated"} for i in range(1, n + 1)]}


def attempts_of(store, request_id):
    return [(a["item_ref"], a["attempt_number"], a["status"]) for a in store.list_attempts(request_id)]


def only_request(store):
    rows = store.list_requests()
    assert len(rows) == 1, rows
    return rows[0]


def total_attempts(store):
    return sum(len(store.list_attempts(r["request_id"])) for r in store.list_requests())


# ---------------------------------------------------------------- the store itself
def test_records_survive_a_restart(tmp_path):
    path = tmp_path / "u.db"
    first = SqliteUsageStore(path)
    rid = first.begin_request(usage.IMAGE_GENERATION, "huggingface/fal-ai", "m", 2)
    aid = first.start_attempt(rid, "huggingface/fal-ai", "m", item_ref=1)
    first.finish_attempt(aid, usage.ATTEMPT_SUCCEEDED)
    first.finish_request(rid, usage.SUCCEEDED, 2)
    del first
    second = SqliteUsageStore(path)  # a fresh object on the same file, as after a restart
    row = second.get_request(rid)
    assert (row["outcome"], row["items_requested"], row["items_succeeded"]) == (usage.SUCCEEDED, 2, 2)
    assert row["provider"] == "huggingface/fal-ai" and row["finished_at"]
    assert attempts_of(second, rid) == [(1, 1, "succeeded")]


def test_request_starts_pending_and_defaults_to_the_anonymous_user(store):
    rid = store.begin_request(usage.PROMPT_GENERATION, "gemini", "g", 10)
    row = store.get_request(rid)
    assert row["outcome"] == usage.PENDING and row["finished_at"] is None
    assert row["user_id"] == usage.ANONYMOUS_USER == "local"
    assert row["estimated_cost_micros"] is None and row["actual_cost_micros"] is None


def test_a_user_id_can_be_attached_for_later(store):
    rid = store.begin_request(usage.PROMPT_GENERATION, "gemini", "g", 1, user_id="user-42")
    assert store.get_request(rid)["user_id"] == "user-42"


def test_a_request_can_only_be_finished_once(store):
    rid = store.begin_request(usage.IMAGE_GENERATION, "p", None, 2)
    assert store.finish_request(rid, usage.PARTIALLY_SUCCEEDED, 1, "server_error") is True
    assert store.finish_request(rid, usage.SUCCEEDED, 2) is False
    row = store.get_request(rid)
    assert (row["outcome"], row["items_succeeded"], row["error_category"]) == (usage.PARTIALLY_SUCCEEDED, 1, "server_error")


@pytest.mark.parametrize("outcome", [usage.PENDING, usage.REJECTED])
def test_finish_request_cannot_set_pending_or_rejected(store, outcome):
    rid = store.begin_request(usage.IMAGE_GENERATION, "p", None, 1)
    with pytest.raises(ValueError):
        store.finish_request(rid, outcome, 0)


def test_rejected_request_is_recorded_but_has_no_attempts(store):
    rid = store.reject_request(usage.IMAGE_GENERATION, 3, "too_many_images")
    row = store.get_request(rid)
    assert row["outcome"] == usage.REJECTED and row["error_category"] == "too_many_images"
    assert row["items_requested"] == 3 and row["items_succeeded"] == 0 and row["finished_at"]
    assert store.list_attempts(rid) == []
    with pytest.raises(ValueError):
        store.start_attempt(rid, "p")  # a rejected request can never be billed
    assert store.finish_request(rid, usage.SUCCEEDED, 3) is False


def test_retries_are_numbered_attempts_of_one_request(store):
    rid = store.begin_request(usage.IMAGE_GENERATION, "p", None, 2)
    for _ in range(3):
        store.start_attempt(rid, "p", item_ref=1)
    store.start_attempt(rid, "p", item_ref=2)
    assert attempts_of(store, rid) == [(1, 1, "started"), (1, 2, "started"), (1, 3, "started"), (2, 1, "started")]
    assert len(store.list_requests()) == 1


def test_attempts_need_a_known_pending_request(store):
    with pytest.raises(ValueError):
        store.start_attempt("no-such-request", "p")
    rid = store.begin_request(usage.IMAGE_GENERATION, "p", None, 1)
    store.finish_request(rid, usage.FAILED, 0)
    with pytest.raises(ValueError):
        store.start_attempt(rid, "p")


def test_finish_attempt_stores_the_result_once(store):
    rid = store.begin_request(usage.PROMPT_GENERATION, "gemini", "g", 10)
    aid = store.start_attempt(rid, "gemini", "g")
    store.finish_attempt(aid, usage.ATTEMPT_SUCCEEDED, input_tokens=11, output_tokens=22)
    store.finish_attempt(aid, usage.ATTEMPT_FAILED, error_category="unknown")  # ignored: already finished
    (a,) = store.list_attempts(rid)
    assert (a["status"], a["input_tokens"], a["output_tokens"], a["error_category"]) == ("succeeded", 11, 22, None)
    assert a["started_at"] and a["finished_at"]


def test_concurrent_attempts_get_unique_numbers(store):
    rid = store.begin_request(usage.IMAGE_GENERATION, "p", None, 1)
    errors = []

    def work():
        try:
            for _ in range(5):
                store.start_attempt(rid, "p", item_ref=1)
        except Exception as e:  # pragma: no cover - failure path
            errors.append(e)

    threads = [threading.Thread(target=work) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
    assert sorted(a["attempt_number"] for a in store.list_attempts(rid)) == list(range(1, 41))


def test_outcome_rules():
    assert usage.outcome_for(3, 3) == usage.SUCCEEDED
    assert usage.outcome_for(3, 1) == usage.PARTIALLY_SUCCEEDED
    assert usage.outcome_for(3, 0) == usage.FAILED


def test_store_from_configuration_shares_one_file(settings, tmp_path):
    settings(USAGE_DB_PATH=tmp_path / "configured.db")
    rid = usage.get_store().begin_request(usage.PROMPT_GENERATION, "gemini", "g", 1)
    assert usage.get_store().get_request(rid) is not None  # a new store object, same file
    assert (tmp_path / "configured.db").exists()


def test_blank_usage_db_path_is_rejected():
    with pytest.raises(config.ConfigError, match="USAGE_DB_PATH"):
        config.load_settings({"USAGE_DB_PATH": "  "})


@pytest.mark.parametrize("make_error,expected", [
    (lambda: make_http_error(401), "auth"), (lambda: make_http_error(403), "auth"),
    (lambda: make_http_error(402), "credits_exhausted"), (lambda: make_http_error(429), "rate_limited"),
    (lambda: make_http_error(503), "server_error"), (lambda: make_http_error(400), "client_error"),
    (lambda: APIError(503, {"error": {"message": "x"}}), "server_error"),
    (lambda: TimeoutError("slow"), "timeout_or_network"), (lambda: RuntimeError("boom"), "unknown"),
])
def test_error_categories(make_error, expected):
    assert usage.categorize_error(make_error()) == expected


# ------------------------------------------------------- image batches through services
def test_successful_batch_is_one_request_with_one_attempt_per_image(workdir, store):
    provider = ScriptedProvider([img()])
    results = services.generate_images(prompt_data(2), [1, 2], provider=provider, store=store)
    assert all(r.ok for r in results)
    row = only_request(store)
    assert (row["operation"], row["outcome"], row["items_requested"], row["items_succeeded"]) == \
        (usage.IMAGE_GENERATION, usage.SUCCEEDED, 2, 2)
    assert row["error_category"] is None and row["user_id"] == "local"
    assert attempts_of(store, row["request_id"]) == [(1, 1, "succeeded"), (2, 1, "succeeded")]


def test_retries_are_recorded_as_attempts_of_the_same_request(workdir, store):
    provider = ScriptedProvider([make_http_error(503), make_http_error(503), img()])
    (result,) = services.generate_images(prompt_data(1), [1], provider=provider, store=store)
    assert result.ok and provider.calls == 3
    row = only_request(store)
    assert row["outcome"] == usage.SUCCEEDED
    attempts = store.list_attempts(row["request_id"])
    assert [(a["attempt_number"], a["status"], a["error_category"], a["http_status"]) for a in attempts] == [
        (1, "failed", "server_error", 503), (2, "failed", "server_error", 503), (3, "succeeded", None, None)]


def test_exhausted_retries_are_all_counted(workdir, store):
    provider = ScriptedProvider([make_http_error(503)])
    (result,) = services.generate_images(prompt_data(1), [1], provider=provider, store=store)
    assert not result.ok
    assert provider.calls == generateImage.MAX_ATTEMPTS
    row = only_request(store)
    assert (row["outcome"], row["items_succeeded"], row["error_category"]) == (usage.FAILED, 0, "server_error")
    assert len(store.list_attempts(row["request_id"])) == generateImage.MAX_ATTEMPTS


def test_partial_batch_records_each_outcome(workdir, store):
    provider = ScriptedProvider([make_http_error(400), img()])
    results = services.generate_images(prompt_data(2), [1, 2], provider=provider, store=store)
    assert [r.ok for r in results] == [False, True]
    row = only_request(store)
    assert (row["outcome"], row["items_requested"], row["items_succeeded"], row["error_category"]) == \
        (usage.PARTIALLY_SUCCEEDED, 2, 1, "client_error")
    assert attempts_of(store, row["request_id"]) == [(1, 1, "failed"), (2, 1, "succeeded")]


def test_a_failed_batch_is_recorded_as_failed(workdir, store):
    provider = ScriptedProvider([make_http_error(400)])
    services.generate_images(prompt_data(2), [1, 2], provider=provider, store=store)
    row = only_request(store)
    assert (row["outcome"], row["items_succeeded"]) == (usage.FAILED, 0)
    assert len(store.list_attempts(row["request_id"])) == 2


def test_mixed_failure_reasons_are_summarised(workdir, store):
    provider = ScriptedProvider([make_http_error(400), make_http_error(401)])
    services.generate_images(prompt_data(2), [1, 2], provider=provider, store=store)
    assert only_request(store)["error_category"] == "multiple"


def test_a_save_failure_still_counts_the_billable_call(workdir, store, monkeypatch):
    def broken_save(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(fileUtils, "saveImage", broken_save)
    (result,) = services.generate_images(prompt_data(1), [1], provider=ScriptedProvider([img()]), store=store)
    assert not result.ok
    row = only_request(store)
    assert (row["outcome"], row["items_succeeded"], row["error_category"]) == (usage.FAILED, 0, "save_failed")
    assert attempts_of(store, row["request_id"]) == [(1, 1, "succeeded")]  # the provider call happened


def test_provider_setup_failure_is_a_failed_request_without_attempts(workdir, store, monkeypatch):
    def boom(name=None):
        raise RuntimeError("HF_TOKEN is not set.")
    monkeypatch.setattr(generateImage, "get_provider", boom)
    results = services.generate_images(prompt_data(2), [1, 2], store=store)
    assert not any(r.ok for r in results)
    row = only_request(store)
    assert (row["outcome"], row["provider"], row["error_category"]) == (usage.FAILED, "unresolved", "provider_unavailable")
    assert total_attempts(store) == 0


def test_unknown_prompt_ids_are_failures_without_attempts(workdir, store):
    provider = ScriptedProvider([img()])
    services.generate_images(prompt_data(1), [99, 1], provider=provider, store=store)
    row = only_request(store)
    assert (row["outcome"], row["items_requested"], row["items_succeeded"]) == (usage.PARTIALLY_SUCCEEDED, 2, 1)
    assert attempts_of(store, row["request_id"]) == [(1, 1, "succeeded")]


class InterruptingProvider(ScriptedProvider):
    """Returns an image for the first call, then behaves as if the user pressed Ctrl+C."""

    def generate(self, prompt):
        self.calls += 1
        if self.calls > 1:
            raise KeyboardInterrupt()
        return img()


def test_interrupted_batch_keeps_the_unfinished_attempt(workdir, store):
    provider = InterruptingProvider([img()])
    with pytest.raises(KeyboardInterrupt):
        services.generate_images(prompt_data(2), [1, 2], provider=provider, store=store)
    row = only_request(store)
    assert (row["outcome"], row["items_succeeded"], row["error_category"]) == (usage.PARTIALLY_SUCCEEDED, 1, "interrupted")
    # the second call may have been billed, and its result is unknown, so it stays "started"
    assert attempts_of(store, row["request_id"]) == [(1, 1, "succeeded"), (2, 1, "started")]


def test_empty_selection_creates_no_record(workdir, store):
    assert services.generate_images(prompt_data(1), [], provider=ScriptedProvider([img()]), store=store) == []
    assert store.list_requests() == []


# --------------------------------------------------- rejected requests (audit only)
def test_rejected_image_requests_are_recorded_and_use_nothing(workdir, store, settings):
    provider = ScriptedProvider([img()])
    with pytest.raises(ValidationError):
        services.generate_images(prompt_data(3), [1, 2, 3], provider=provider, store=store)
    settings(GENERATION_ENABLED="false")
    with pytest.raises(Exception):
        services.generate_images(prompt_data(1), [1], provider=provider, store=store)
    rows = store.list_requests()
    assert sorted((r["outcome"], r["error_category"], r["items_requested"]) for r in rows) == [
        ("rejected", "generation_disabled", 1), ("rejected", "too_many_images", 3)]
    assert provider.calls == 0 and total_attempts(store) == 0


def test_over_long_image_prompt_is_recorded_as_rejected(workdir, store):
    data = prompt_data(1, text="x" * 2001)
    with pytest.raises(ValidationError):
        services.generate_images(data, [1], provider=ScriptedProvider([img()]), store=store)
    assert only_request(store)["error_category"] == "image_prompt_too_long"


# ------------------------------------------------------------------ fails closed
class BrokenStore(SqliteUsageStore):
    def __init__(self, path, fail="begin_request"):
        super().__init__(path)
        self.fail = fail

    def begin_request(self, *a, **k):
        if self.fail == "begin_request":
            raise OSError("disk unavailable")
        return super().begin_request(*a, **k)

    def start_attempt(self, *a, **k):
        if self.fail == "start_attempt":
            raise OSError("disk unavailable")
        return super().start_attempt(*a, **k)


def test_images_are_not_generated_when_the_request_cannot_be_recorded(workdir, tmp_path):
    provider = ScriptedProvider([img()])
    with pytest.raises(UsageUnavailableError) as exc:
        services.generate_images(prompt_data(1), [1], provider=provider, store=BrokenStore(tmp_path / "b.db"))
    assert exc.value.http_status == 503 and provider.calls == 0


def test_a_call_that_cannot_be_recorded_is_not_made(workdir, tmp_path):
    provider = ScriptedProvider([img()])
    store = BrokenStore(tmp_path / "b.db", fail="start_attempt")
    (result,) = services.generate_images(prompt_data(1), [1], provider=provider, store=store)
    assert not result.ok and provider.calls == 0


def test_unreachable_store_blocks_prompt_generation(workdir, tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(generatePrompts, "get_client", lambda: called.append(1))
    result = services.expand_idea("a cat", store=BrokenStore(tmp_path / "b.db"))
    assert not result.ok and result.error_code == "usage_unavailable" and called == []


# --------------------------------------------------------- Gemini through services
GOOD_REPLY = json.dumps({"prompts": [{"text": f"p{i}"} for i in range(1, 13)]})


class FakeGemini:
    """Plays back replies or exceptions as the Gemini client; counts real calls."""

    def __init__(self, script):
        self.script, self.calls = list(script), 0
        self.models = self

    def generate_content(self, **kwargs):
        self.calls += 1
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        return item


def reply(text=GOOD_REPLY):
    return types.SimpleNamespace(text=text, usage_metadata=types.SimpleNamespace(
        prompt_token_count=11, candidates_token_count=22))


def gemini_error(code):
    return APIError(code, {"error": {"message": "fake", "status": "FAKE"}})


@pytest.fixture
def fake_gemini(monkeypatch):
    def install(*script):
        client = FakeGemini(script)
        monkeypatch.setattr(generatePrompts, "get_client", lambda: client)
        return client
    monkeypatch.setattr(generatePrompts.call_gemini.retry, "wait", tenacity.wait_none())
    return install


def test_prompt_generation_is_recorded_with_token_counts(workdir, store, fake_gemini):
    fake_gemini(reply())
    result = services.expand_idea("a cat", store=store)
    assert result.ok
    row = only_request(store)
    assert (row["operation"], row["provider"], row["model"]) == (usage.PROMPT_GENERATION, "gemini", generatePrompts.GEMINI_MODEL)
    assert (row["outcome"], row["items_requested"], row["items_succeeded"]) == (usage.SUCCEEDED, 10, 10)
    (a,) = store.list_attempts(row["request_id"])
    assert (a["status"], a["input_tokens"], a["output_tokens"]) == ("succeeded", 11, 22)


def test_gemini_retries_are_counted_under_one_request(workdir, store, fake_gemini):
    client = fake_gemini(gemini_error(503), gemini_error(503), reply())
    assert services.expand_idea("a cat", store=store).ok
    assert client.calls == 3
    row = only_request(store)
    assert attempts_of(store, row["request_id"]) == [(0, 1, "failed"), (0, 2, "failed"), (0, 3, "succeeded")]
    assert row["outcome"] == usage.SUCCEEDED


def test_gemini_failure_is_recorded(workdir, store, fake_gemini):
    fake_gemini(gemini_error(400))
    result = services.expand_idea("a cat", store=store)
    assert not result.ok
    row = only_request(store)
    assert (row["outcome"], row["error_category"]) == (usage.FAILED, "client_error")
    (a,) = store.list_attempts(row["request_id"])
    assert (a["status"], a["http_status"]) == ("failed", 400)


def test_unusable_gemini_reply_still_counts_the_call(workdir, store, fake_gemini):
    fake_gemini(reply("not json"))
    assert not services.expand_idea("a cat", store=store).ok
    row = only_request(store)
    assert (row["outcome"], row["error_category"]) == (usage.FAILED, "invalid_response")
    assert attempts_of(store, row["request_id"]) == [(0, 1, "succeeded")]


def test_fewer_prompts_than_requested_is_a_partial_success(workdir, store, fake_gemini):
    fake_gemini(reply(json.dumps({"prompts": [{"text": "a"}, {"text": "b"}, {"text": "c"}]})))
    assert services.expand_idea("a cat", store=store).ok
    row = only_request(store)
    assert (row["outcome"], row["items_succeeded"], row["items_requested"]) == (usage.PARTIALLY_SUCCEEDED, 3, 10)


def test_missing_gemini_key_is_a_failure_without_an_attempt(workdir, store, monkeypatch):
    monkeypatch.setattr(generatePrompts, "_client", None)
    monkeypatch.delenv("API_KEY", raising=False)
    result = services.expand_idea("a cat", store=store)
    assert not result.ok and "API_KEY" in result.error
    row = only_request(store)
    assert (row["outcome"], row["error_category"]) == (usage.FAILED, "not_attempted")
    assert total_attempts(store) == 0


@pytest.mark.parametrize("idea,code", [("", "empty_concept"), ("x" * 501, "concept_too_long")])
def test_rejected_ideas_are_recorded_and_never_reach_gemini(workdir, store, fake_gemini, idea, code):
    client = fake_gemini(reply())
    services.expand_idea(idea, store=store)
    row = only_request(store)
    assert (row["outcome"], row["error_category"]) == (usage.REJECTED, code)
    assert client.calls == 0 and total_attempts(store) == 0


def test_too_many_prompts_and_disabled_switch_are_recorded_as_rejections(workdir, store, fake_gemini, settings):
    client = fake_gemini(reply())
    services.expand_idea("a cat", num_prompts=11, store=store)
    settings(GENERATION_ENABLED="false")
    services.expand_idea("a cat", store=store)
    assert sorted(r["error_category"] for r in store.list_requests()) == ["generation_disabled", "too_many_prompts"]
    assert all(r["outcome"] == usage.REJECTED for r in store.list_requests())
    assert client.calls == 0 and total_attempts(store) == 0


def test_each_idea_in_a_batch_is_its_own_request(workdir, store, fake_gemini):
    fake_gemini(reply())
    results = services.expand_ideas(["a cat", "", "a dog"], delay=0, store=store)
    assert [r.ok for r in results] == [True, False, True]
    assert sorted(r["outcome"] for r in store.list_requests()) == ["rejected", "succeeded", "succeeded"]


# ------------------------------------------------------------------ nothing sensitive
def test_database_holds_no_prompt_text_or_credentials(workdir, tmp_path, monkeypatch, fake_gemini):
    monkeypatch.setenv("HF_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("API_KEY", FAKE_TOKEN[::-1])
    store = SqliteUsageStore(tmp_path / "secret_check.db")
    fake_gemini(gemini_error(503), reply())
    services.expand_idea(f"{SENTINEL_TEXT} idea", store=store)
    services.expand_idea("", store=store)
    provider = ScriptedProvider([make_http_error(503), img()])
    services.generate_images(prompt_data(1), [1], provider=provider, store=store)
    bad = ScriptedProvider([RuntimeError(f"Authorization: Bearer {FAKE_TOKEN} {SENTINEL_TEXT}")])
    services.generate_images(prompt_data(1), [1], provider=bad, store=store)
    data = b"".join(p.read_bytes() for p in tmp_path.glob("secret_check.db*"))
    assert data  # we really did read the database
    for forbidden in (SENTINEL_TEXT, FAKE_TOKEN, FAKE_TOKEN[::-1], "Bearer"):
        assert forbidden.encode() not in data, forbidden
    assert len(store.list_requests()) == 4
