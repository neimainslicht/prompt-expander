"""Quotas, budgets, stale requests, bypass protection and concurrency (Checkpoint 4).

No real provider is ever called: providers are fakes, time is a fake clock, and money is a
made-up test price (HF_COST_PER_IMAGE_USD=0.05 unless a test sets another).
"""
import threading
import types
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest import mock

import pytest
import tenacity
from google.genai.errors import APIError
from PIL import Image

import admin
import config
import generateImage
import generatePrompts
import limits
import main
import pricing
import services
import usage
from conftest import ScriptedProvider, make_http_error
from errors import (AuthorizationRequiredError, LimitExceededError, PricingUnavailableError,
                    UsageUnavailableError, ValidationError)
from test_usage import FakeGemini, gemini_error, prompt_data, reply
from usage import SqliteUsageStore

HF = "huggingface/test"


class Clock:
    """A controllable clock; stores take it so tests can move time."""

    def __init__(self, start=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    return SqliteUsageStore(tmp_path / "limits.db", clock=clock, stale_after_seconds=900)


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def img():
    return Image.new("RGB", (4, 4))


def begin_images(store, count):
    """Authorise an image batch the way services does, returning its request id."""
    plan = limits.image_plan(HF, "m", count)
    return store.begin_request(usage.IMAGE_GENERATION, HF, "m", count, quotas=plan.begin_quotas,
                               reserved_images=plan.reserved_images, reserved_cost_micros=plan.reserved_cost_micros)


def attempt(store, request_id, item=1, price=50_000):
    """Start an attempt the way an image provider call would."""
    plan = limits.image_plan(HF, "m", 1)
    return store.start_attempt(request_id, HF, "m", item_ref=item, estimated_cost_micros=price,
                               quotas=plan.attempt_quotas)


def run_threads(jobs):
    """Run each callable in its own thread at the same moment; return (results, errors)."""
    results, errors, gate = [], [], threading.Barrier(len(jobs))

    def wrap(job):
        gate.wait()
        try:
            results.append(job())
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=wrap, args=(j,)) for j in jobs]
    [t.start() for t in threads]
    [t.join() for t in threads]
    return results, errors


# ------------------------------------------------------------------ configuration
def test_new_settings_defaults():
    s = config.load_settings({})
    assert s.hf_cost_per_image_usd is None and s.stale_request_seconds == 900 and s.gemini_plan == "free"
    assert s.hf_usable_budget_micros == 4_275_000


def test_cost_per_image_is_validated():
    assert config.load_settings({"HF_COST_PER_IMAGE_USD": "0.04"}).hf_cost_per_image_usd == Decimal("0.04")
    for bad in ("0", "-1", "abc", "nan", ""):
        with pytest.raises(config.ConfigError, match="HF_COST_PER_IMAGE_USD"):
            config.load_settings({"HF_COST_PER_IMAGE_USD": bad})


@pytest.mark.parametrize("name,value", [("STALE_REQUEST_SECONDS", "30"), ("STALE_REQUEST_SECONDS", "x"),
                                        ("GEMINI_PLAN", "paid")])
def test_other_new_settings_are_validated(name, value):
    with pytest.raises(config.ConfigError, match=name):
        config.load_settings({name: value})


def test_old_budget_name_is_refused_not_ignored():
    with pytest.raises(config.ConfigError, match="renamed to HF_BUDGET_USD"):
        config.load_settings({"HF_MONTHLY_BUDGET_USD": "4.75"})


# ------------------------------------------------------------------ pricing
def test_unset_image_price_refuses_paid_generation(workdir, store, monkeypatch):
    monkeypatch.delenv("HF_COST_PER_IMAGE_USD")
    config.get_settings.cache_clear()
    provider = ScriptedProvider([img()])
    with pytest.raises(PricingUnavailableError) as exc:
        services.generate_images(prompt_data(1), [1], provider=provider, store=store)
    assert "HF_COST_PER_IMAGE_USD" in exc.value.message and provider.calls == 0
    (row,) = store.list_requests()
    assert (row["outcome"], row["error_category"]) == (usage.REJECTED, "pricing_unavailable")


def test_unknown_provider_has_no_price(settings):
    with pytest.raises(PricingUnavailableError):
        pricing.call_cost_micros("somewhere-else/model", config.get_settings())
    assert pricing.call_cost_micros("gemini", config.get_settings()) == 0  # free tier


# ------------------------------------------------------------------ daily image quota
def test_quota_counts_requested_images_not_retries(workdir, store):
    # one image needs three tries; the other succeeds first time: still just 2 images
    provider = ScriptedProvider([make_http_error(503), make_http_error(503), img(), img()])
    results = services.generate_images(prompt_data(2), [1, 2], provider=provider, store=store)
    assert all(r.ok for r in results) and provider.calls == 4
    quota = limits.image_plan(HF, "m", 1).begin_quotas[1]
    assert store.measure(quota) == 2
    # 2 used, so 3 more fit and 4 do not
    store_ok = services.generate_images(prompt_data(2), [1, 2], provider=ScriptedProvider([img()]), store=store)
    assert all(r.ok for r in store_ok)
    with pytest.raises(LimitExceededError) as exc:
        services.generate_images(prompt_data(2), [1, 2], provider=ScriptedProvider([img()]), store=store)
    assert exc.value.code == "daily_quota_exceeded" and "4 of 5" in exc.value.message


def test_daily_quota_survives_restart_and_resets_at_local_midnight(workdir, tmp_path, clock, settings):
    settings(MAX_IMAGES_PER_DAY=2)
    path = tmp_path / "persist.db"
    clock.now = datetime(2026, 1, 15, 21, 30, tzinfo=timezone.utc)  # 23:30 in Asia/Jerusalem (UTC+2)
    first = SqliteUsageStore(path, clock=clock)
    services.generate_images(prompt_data(2), [1, 2], provider=ScriptedProvider([img()]), store=first)
    restarted = SqliteUsageStore(path, clock=clock)  # a new object on the same file
    with pytest.raises(LimitExceededError) as exc:
        services.generate_images(prompt_data(1), [1], provider=ScriptedProvider([img()]), store=restarted)
    assert exc.value.code == "daily_quota_exceeded"
    assert 0 < exc.value.retry_after_seconds <= 30 * 60  # about half an hour to midnight
    clock.advance(60 * 60)  # 00:30 local time: a new day
    ok = services.generate_images(prompt_data(1), [1], provider=ScriptedProvider([img()]), store=restarted)
    assert ok[0].ok


def test_active_request_holds_its_reservation_and_unattempted_images_are_released(store):
    rid = begin_images(store, 2)
    quota = limits.image_plan(HF, "m", 1).begin_quotas[1]
    assert store.measure(quota) == 2  # reserved while active, before any attempt
    attempt(store, rid, item=1)
    attempt(store, rid, item=1)  # a retry of the same image
    assert store.measure(quota) == 2  # still the reservation, not 2 + attempts
    store.finish_request(rid, usage.PARTIALLY_SUCCEEDED, 1)
    assert store.measure(quota) == 1  # image 2 was never attempted: released


def test_rejected_requests_do_not_use_quota(workdir, store):
    quota = limits.image_plan(HF, "m", 1).begin_quotas[1]
    with pytest.raises(ValidationError):
        services.generate_images(prompt_data(3), [1, 2, 3], provider=ScriptedProvider([img()]), store=store)
    assert store.measure(quota) == 0


# ------------------------------------------------------------------ request rate
def test_request_rate_limit_and_recovery(workdir, store, clock, monkeypatch):
    client = FakeGemini([reply()])
    monkeypatch.setattr(generatePrompts, "get_client", lambda: client)
    monkeypatch.setattr(generatePrompts.call_gemini.retry, "wait", tenacity.wait_none())
    for _ in range(3):
        assert services.expand_idea("a cat", store=store).ok
        clock.advance(10)
    blocked = services.expand_idea("a cat", store=store)
    assert not blocked.ok and blocked.error_code == "rate_limited"
    assert 1 <= blocked.retry_after_seconds <= 30
    assert client.calls == 3  # the fourth never reached Gemini
    clock.advance(blocked.retry_after_seconds)
    assert services.expand_idea("a cat", store=store).ok


def test_batch_of_ideas_waits_out_the_rate_limit(workdir, store, clock, monkeypatch):
    client = FakeGemini([reply()])
    monkeypatch.setattr(generatePrompts, "get_client", lambda: client)
    sleeps = []
    monkeypatch.setattr(services.time, "sleep", lambda s: (sleeps.append(s), clock.advance(s)))
    results = services.expand_ideas(["a", "b", "c", "d", "e"], delay=0, store=store)
    assert all(r.ok for r in results) and client.calls == 5
    assert sleeps and all(0 < s <= 60 for s in sleeps)


# ------------------------------------------------------------------ Gemini request limits
def test_gemini_retries_count_against_the_provider_limit(workdir, store, settings, monkeypatch):
    settings(GEMINI_MAX_REQUESTS_PER_MINUTE=2, GEMINI_MAX_REQUESTS_PER_DAY=50)
    client = FakeGemini([gemini_error(503), gemini_error(503), reply()])
    monkeypatch.setattr(generatePrompts, "get_client", lambda: client)
    monkeypatch.setattr(generatePrompts.call_gemini.retry, "wait", tenacity.wait_none())
    result = services.expand_idea("a cat", store=store)
    assert not result.ok and "request limit" in result.error
    assert client.calls == 2  # the third try was refused before it was made
    (row,) = store.list_requests()
    assert (row["outcome"], row["error_category"]) == (usage.FAILED, "provider_rate_limited")
    assert len(store.list_attempts(row["request_id"])) == 2


def test_gemini_daily_limit_rejects_before_calling(workdir, store, clock, settings, monkeypatch):
    settings(GEMINI_MAX_REQUESTS_PER_MINUTE=2, GEMINI_MAX_REQUESTS_PER_DAY=2, MAX_GENERATION_REQUESTS_PER_MINUTE=10)
    client = FakeGemini([reply()])
    monkeypatch.setattr(generatePrompts, "get_client", lambda: client)
    assert services.expand_idea("a", store=store).ok and services.expand_idea("b", store=store).ok
    clock.advance(120)  # the per-minute window clears; the daily one does not
    third = services.expand_idea("c", store=store)
    assert third.error_code == "provider_daily_limit" and client.calls == 2
    assert "midnight" in third.error


def test_gemini_calls_cost_nothing(workdir, store, monkeypatch):
    monkeypatch.setattr(generatePrompts, "get_client", lambda: FakeGemini([reply()]))
    services.expand_idea("a cat", store=store)
    assert store.cost_summary("gemini")["committed"] == 0 and store.cost_summary(HF)["committed"] == 0


# ------------------------------------------------------------------ budget
def test_budget_is_cumulative_across_months_and_margin_applied_once(workdir, store, clock, settings):
    settings(HF_BUDGET_USD=1.0, HF_BUDGET_SAFETY_MARGIN_PERCENT=10, HF_COST_PER_IMAGE_USD=0.3,
             MAX_IMAGES_PER_DAY=50)
    ceiling = config.get_settings().hf_usable_budget_usd
    assert ceiling == Decimal("0.9") and config.get_settings().hf_usable_budget_micros == 900_000
    for month in range(3):  # three requests in three different months fill the budget: 0.9
        services.generate_images(prompt_data(1), [1], provider=ScriptedProvider([img()]), store=store)
        clock.advance(40 * 24 * 3600)
    assert store.cost_summary("huggingface")["committed"] == 900_000
    with pytest.raises(LimitExceededError) as exc:  # no new month, no reset
        services.generate_images(prompt_data(1), [1], provider=ScriptedProvider([img()]), store=store)
    assert exc.value.code == "budget_exceeded" and "$0.9000" in exc.value.message


def test_oversized_batch_is_refused_whole(workdir, store, settings):
    settings(HF_BUDGET_USD=0.5, HF_BUDGET_SAFETY_MARGIN_PERCENT=0, HF_COST_PER_IMAGE_USD=0.3)
    provider = ScriptedProvider([img()])
    with pytest.raises(LimitExceededError):
        services.generate_images(prompt_data(2), [1, 2], provider=provider, store=store)  # needs 0.6
    assert provider.calls == 0
    (row,) = store.list_requests()
    assert (row["outcome"], row["error_category"]) == (usage.REJECTED, "budget_exceeded")
    assert services.generate_images(prompt_data(1), [1], provider=provider, store=store)[0].ok  # 0.3 fits


def test_failed_and_retried_attempts_are_charged(workdir, store):
    provider = ScriptedProvider([make_http_error(503), make_http_error(503), img(), make_http_error(400)])
    results = services.generate_images(prompt_data(2), [1, 2], provider=provider, store=store)
    assert [r.ok for r in results] == [True, False]
    (row,) = store.list_requests()
    assert len(store.list_attempts(row["request_id"])) == 4
    summary = store.cost_summary("huggingface")
    assert summary["estimated_consumed"] == 200_000  # every attempt, failed ones too
    assert summary["reserved_remaining"] == 0 and summary["confirmed_actual"] == 0
    assert row["estimated_cost_micros"] == 200_000


def test_retry_that_would_pass_the_budget_is_not_made(workdir, store, settings):
    settings(HF_BUDGET_USD=0.35, HF_BUDGET_SAFETY_MARGIN_PERCENT=0, HF_COST_PER_IMAGE_USD=0.3)
    provider = ScriptedProvider([make_http_error(503), img()])
    (result,) = services.generate_images(prompt_data(1), [1], provider=provider, store=store)
    assert not result.ok and provider.calls == 1  # the retry was refused
    (row,) = store.list_requests()
    assert row["error_category"] == "budget_exceeded"
    assert store.cost_summary("huggingface")["committed"] == 300_000


def test_reservation_is_used_up_by_attempts_not_added_to_them(store):
    rid = begin_images(store, 2)  # reserves 0.10
    summary = store.cost_summary("huggingface")
    assert summary["reserved_remaining"] == 100_000 and summary["committed"] == 100_000
    attempt(store, rid, item=1)
    summary = store.cost_summary("huggingface")
    assert summary["committed"] == 100_000  # 0.05 spent + 0.05 still reserved: not 0.15
    assert summary["estimated_consumed"] == 50_000
    store.finish_request(rid, usage.PARTIALLY_SUCCEEDED, 1)
    summary = store.cost_summary("huggingface")
    assert summary["committed"] == 50_000 and summary["reserved_remaining"] == 0  # unused part released


def test_actual_cost_replaces_the_estimate_and_is_reported_separately(store):
    rid = begin_images(store, 1)
    aid = attempt(store, rid)
    store.finish_attempt(aid, usage.ATTEMPT_SUCCEEDED)
    store.finish_request(rid, usage.SUCCEEDED, 1)
    assert store.record_actual_cost(aid, 20_000) is True
    summary = store.cost_summary("huggingface")
    assert summary["confirmed_actual"] == 20_000 and summary["estimated_consumed"] == 0
    assert summary["committed"] == 20_000
    assert store.get_request(rid)["actual_cost_micros"] == 20_000
    assert store.record_actual_cost(99999, 20_000) is False


# ------------------------------------------------------------------ stale requests
def test_active_request_is_left_alone_and_stale_one_is_closed_conservatively(store, clock):
    rid = begin_images(store, 2)
    attempt(store, rid, item=1)  # never finished: the app "crashed" during this call
    clock.advance(800)  # recent enough: still treated as running
    other = begin_images(store, 1)
    assert store.get_request(rid)["outcome"] == usage.PENDING
    assert store.cost_summary("huggingface")["committed"] == 150_000  # max(0.10, 0.05) + other
    store.finish_request(other, usage.FAILED, 0)
    clock.advance(200)  # now 1000 s without activity (> 900)
    begin_images(store, 1)  # authorisation sweeps first
    swept = store.get_request(rid)
    assert (swept["outcome"], swept["error_category"]) == (usage.FAILED, "abandoned")
    (a,) = store.list_attempts(rid)
    assert a["status"] == "started"  # unknown result: kept, and still charged
    summary = store.cost_summary("huggingface")
    assert summary["estimated_consumed"] == 50_000  # the interrupted call still counts once
    assert summary["reserved_remaining"] == 50_000  # only the new request's reservation


def test_a_swept_request_cannot_be_counted_twice_or_revived(store, clock):
    rid = begin_images(store, 1)
    attempt(store, rid)
    clock.advance(1000)
    assert store.sweep_stale() == 1
    assert store.sweep_stale() == 0
    assert store.finish_request(rid, usage.SUCCEEDED, 1) is False  # too late: already closed
    with pytest.raises(ValueError):
        store.start_attempt(rid, HF)
    assert store.cost_summary("huggingface")["estimated_consumed"] == 50_000


def test_activity_keeps_a_long_request_alive(store, clock):
    rid = begin_images(store, 1)
    for _ in range(4):  # 4 x 600 s = 40 minutes, but never 900 s without activity
        clock.advance(600)
        aid = attempt(store, rid)
        store.finish_attempt(aid, usage.ATTEMPT_FAILED, error_category="server_error")
        assert store.sweep_stale() == 0
    assert store.get_request(rid)["outcome"] == usage.PENDING


def test_stale_images_count_only_the_attempted_ones(store, clock):
    rid = begin_images(store, 2)
    attempt(store, rid, item=1)
    quota = limits.image_plan(HF, "m", 1).begin_quotas[1]
    assert store.measure(quota) == 2
    clock.advance(1000)
    store.sweep_stale()
    assert store.measure(quota) == 1


# ------------------------------------------------------------------ no bypass of the service boundary
def test_low_level_image_functions_refuse_to_run_without_authorisation():
    provider = ScriptedProvider([img()])
    image, error = generateImage.generate_image_with_error("a cat", provider)
    assert image is None and "service layer" in error
    with pytest.raises(AuthorizationRequiredError):
        generateImage.call_image_api(provider, "a cat", None)
    with pytest.raises(AuthorizationRequiredError):
        generateImage.call_image_api(provider, "a cat", mock.Mock())  # a stand-in is not a permit
    assert provider.calls == 0


def test_low_level_gemini_functions_refuse_to_run_without_authorisation(monkeypatch):
    client = FakeGemini([reply()])
    monkeypatch.setattr(generatePrompts, "get_client", lambda: client)
    text, error = generatePrompts.generate_prompt_text("a cat")
    assert text is None and "service layer" in error
    with pytest.raises(AuthorizationRequiredError):
        generatePrompts.call_gemini("a cat")
    assert client.calls == 0


def test_a_permit_cannot_outlive_its_request(store):
    rid = begin_images(store, 1)
    observer = usage.AttemptObserver(store, rid, HF)
    store.finish_request(rid, usage.FAILED, 0)
    provider = ScriptedProvider([img()])
    image, error = generateImage.generate_image_with_error("a cat", provider, observer)
    assert image is None and provider.calls == 0
    assert observer.last_error_category == "request_closed"


def test_every_documented_workflow_goes_through_authorisation(workdir, tmp_path):
    """Unreachable store: neither workflow can reach a provider."""
    class Broken(SqliteUsageStore):
        def begin_request(self, *a, **k):
            raise OSError("down")
    broken = Broken(tmp_path / "x.db")
    provider = ScriptedProvider([img()])
    with pytest.raises(UsageUnavailableError):
        services.generate_images(prompt_data(1), [1], provider=provider, store=broken)
    assert services.expand_idea("a cat", store=broken).error_code == "usage_unavailable"
    assert provider.calls == 0


# ------------------------------------------------------------------ concurrency (atomic reservations)
def test_concurrent_requests_cannot_reserve_the_same_budget(tmp_path, settings):
    settings(HF_BUDGET_USD=1.0, HF_BUDGET_SAFETY_MARGIN_PERCENT=0, HF_COST_PER_IMAGE_USD=0.3,
             MAX_IMAGES_PER_DAY=50, MAX_GENERATION_REQUESTS_PER_MINUTE=50)
    path = tmp_path / "race.db"
    results, errors = run_threads([lambda: begin_images(SqliteUsageStore(path), 1) for _ in range(12)])
    assert len(results) == 3  # 3 x 0.3 = 0.9 fits in 1.0; a 4th would not
    assert len(errors) == 9 and all(isinstance(e, LimitExceededError) and e.code == "budget_exceeded" for e in errors)
    assert SqliteUsageStore(path).cost_summary("huggingface")["committed"] == 900_000


def test_concurrent_requests_cannot_claim_the_same_daily_images(tmp_path, settings):
    settings(MAX_IMAGES_PER_DAY=5, MAX_GENERATION_REQUESTS_PER_MINUTE=50)
    path = tmp_path / "race.db"
    results, errors = run_threads([lambda: begin_images(SqliteUsageStore(path), 2) for _ in range(8)])
    assert len(results) == 2  # 4 of 5 images; a third batch of 2 would make 6
    assert {e.code for e in errors} == {"daily_quota_exceeded"} and len(errors) == 6


def test_concurrent_requests_respect_the_per_minute_request_limit(tmp_path, settings):
    settings(MAX_IMAGES_PER_DAY=50, MAX_IMAGES_PER_REQUEST=2)
    path = tmp_path / "race.db"
    results, errors = run_threads([lambda: begin_images(SqliteUsageStore(path), 1) for _ in range(9)])
    assert len(results) == 3 and {e.code for e in errors} == {"rate_limited"}


def test_concurrent_attempts_respect_the_provider_request_limit(tmp_path, settings):
    settings(GEMINI_MAX_REQUESTS_PER_MINUTE=4, GEMINI_MAX_REQUESTS_PER_DAY=50)
    path = tmp_path / "race.db"
    plan = limits.prompt_plan("gemini", "g")
    store = SqliteUsageStore(path)
    ids = [store.begin_request(usage.PROMPT_GENERATION, "gemini", "g", 1) for _ in range(10)]
    jobs = [lambda rid=rid: SqliteUsageStore(path).start_attempt(rid, "gemini", "g", quotas=plan.attempt_quotas)
            for rid in ids]
    results, errors = run_threads(jobs)
    assert len(results) == 4 and {e.code for e in errors} == {"provider_rate_limited"}


def test_concurrent_retries_cannot_share_the_same_remaining_budget(tmp_path, settings):
    settings(HF_BUDGET_USD=0.6, HF_BUDGET_SAFETY_MARGIN_PERCENT=0, HF_COST_PER_IMAGE_USD=0.3)
    path = tmp_path / "race.db"
    store = SqliteUsageStore(path)
    rid = begin_images(store, 1)  # reserves 0.3 of 0.6
    plan = limits.image_plan(HF, "m", 1)
    jobs = [lambda: SqliteUsageStore(path).start_attempt(rid, HF, "m", estimated_cost_micros=300_000,
                                                         quotas=plan.attempt_quotas) for _ in range(8)]
    results, errors = run_threads(jobs)
    assert len(results) == 2  # the reserved call plus exactly one more fits in 0.6
    assert {e.code for e in errors} == {"budget_exceeded"}
    assert store.cost_summary("huggingface")["committed"] == 600_000


def test_concurrent_image_batches_through_services(workdir, tmp_path, settings):
    settings(MAX_IMAGES_PER_DAY=5, MAX_GENERATION_REQUESTS_PER_MINUTE=50)
    path = tmp_path / "service_race.db"
    SqliteUsageStore(path)  # create it once up front
    provider = ScriptedProvider([img()])

    def batch(i):
        data = prompt_data(2)
        data["concept"] = f"concept {i}"
        return lambda: services.generate_images(data, [1, 2], provider=provider, store=SqliteUsageStore(path))

    results, errors = run_threads([batch(i) for i in range(5)])
    assert len(results) == 2 and all(isinstance(e, LimitExceededError) for e in errors) and len(errors) == 3
    store = SqliteUsageStore(path)
    assert store.measure(limits.image_plan(HF, "m", 1).begin_quotas[1]) == 4
    assert sum(len(store.list_attempts(r["request_id"])) for r in store.list_requests()) == 4


# ------------------------------------------------------------------ administrator tools
def test_status_report_shows_budget_and_quotas(workdir, store, capsys):
    services.generate_images(prompt_data(1), [1], provider=ScriptedProvider([img()]), store=store)
    assert admin.main(["status"], store=store) == 0
    out = capsys.readouterr().out
    assert "usable ceiling       $4.2750" in out and "estimated spent    $0.0500" in out
    assert "Images today (Asia/Jerusalem):" in out and "1 of 5" in out
    assert "image_generation" in out


def test_adjustment_restores_capacity_and_is_audited(workdir, store, settings, capsys):
    settings(HF_BUDGET_USD=0.5, HF_BUDGET_SAFETY_MARGIN_PERCENT=0, HF_COST_PER_IMAGE_USD=0.3)
    services.generate_images(prompt_data(1), [1], provider=ScriptedProvider([img()]), store=store)
    with pytest.raises(LimitExceededError):
        services.generate_images(prompt_data(1), [1], provider=ScriptedProvider([img()]), store=store)
    assert admin.main(["adjust", "--amount=-0.3", "--note", "dashboard shows less spend"], store=store) == 0
    assert "Recorded adjustment" in capsys.readouterr().out
    assert services.generate_images(prompt_data(1), [1], provider=ScriptedProvider([img()]), store=store)[0].ok
    assert store.cost_summary("huggingface")["adjustments"] == -300_000


def test_adjustment_needs_a_note(store, capsys):
    assert admin.main(["adjust", "--amount=1", "--note", "  "], store=store) == 1
    assert "note" in capsys.readouterr().out


def test_admin_can_record_actual_cost_and_sweep(store, clock, capsys):
    rid = begin_images(store, 1)
    aid = attempt(store, rid)
    clock.advance(1000)
    assert admin.main(["sweep-stale"], store=store) == 0
    assert "Closed 1" in capsys.readouterr().out
    assert admin.main(["record-actual", "--attempt-id", str(aid), "--usd", "0.01"], store=store) == 0
    assert store.cost_summary("huggingface")["confirmed_actual"] == 10_000
    assert admin.main(["record-actual", "--attempt-id", "999", "--usd", "0.01"], store=store) == 1


# ------------------------------------------------------------------ database upgrade and CLI
def test_version_1_database_is_upgraded_in_place(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE requests (request_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, created_at TEXT NOT NULL,
            finished_at TEXT, operation TEXT NOT NULL, provider TEXT, model TEXT, items_requested INTEGER NOT NULL,
            items_succeeded INTEGER NOT NULL DEFAULT 0, outcome TEXT NOT NULL, estimated_cost_usd REAL,
            actual_cost_usd REAL, error_category TEXT);
        INSERT INTO requests (request_id, user_id, created_at, operation, items_requested, outcome)
            VALUES ('old1', 'local', '2026-01-01T00:00:00.000+00:00', 'image_generation', 2, 'pending');
        PRAGMA user_version = 1;
    """)
    conn.commit()
    conn.close()
    store = SqliteUsageStore(path)
    assert store.sweep_stale() == 1  # the old pending request is recognised as abandoned
    assert store.get_request("old1")["error_category"] == "abandoned"
    assert begin_images(store, 1)


def test_cli_shows_limit_messages(monkeypatch, capsys):
    class Q:
        def __init__(self, v):
            self.v = v

        def ask(self):
            return self.v

    data = {"concept": "Cafe", "prompts": [{"id": 1, "text": "t", "status": "not generated"}]}
    monkeypatch.setattr("builtins.input", lambda *a: "")
    monkeypatch.setattr(main.questionary, "select", lambda *a, **k: Q("f.json"))
    monkeypatch.setattr(main.questionary, "checkbox", lambda *a, **k: Q([1]))
    monkeypatch.setattr(services, "list_prompt_files", lambda: ["f.json"])
    monkeypatch.setattr(services, "load_prompt_file", lambda name: data)

    def refuse(*a, **k):
        raise LimitExceededError("daily_quota_exceeded", "Daily image limit reached: 5 of 5.")
    monkeypatch.setattr(services, "generate_images", refuse)
    main.process_image_generation()
    assert "Daily image limit reached" in capsys.readouterr().out


# =====================================================================================
# Follow-up review of Checkpoint 4: provider timeouts vs stale requests, exact money,
# per-image retry accounting, and a visible wait in batch mode.
# =====================================================================================
import sqlite3  # noqa: E402

import money  # noqa: E402


# ------------------------------------------------------------------ timeouts and stale requests
def test_gemini_client_is_built_with_an_explicit_timeout(monkeypatch):
    built = []
    monkeypatch.setattr(generatePrompts.genai, "Client", lambda **kwargs: built.append(kwargs) or object())
    monkeypatch.setattr(generatePrompts, "_client", None)
    monkeypatch.setenv("API_KEY", "placeholder-not-a-real-key")
    generatePrompts.get_client()
    (kwargs,) = built
    assert kwargs["http_options"].timeout == config.GEMINI_TIMEOUT_SECONDS * 1000  # the SDK takes milliseconds


def test_every_provider_wait_is_far_shorter_than_the_stale_threshold():
    for seconds in (config.GEMINI_TIMEOUT_SECONDS, config.HF_TIMEOUT_SECONDS, config.RETRY_WAIT_MAX_SECONDS):
        assert config.MIN_STALE_REQUEST_SECONDS >= 2 * seconds  # a safety margin of at least double
    assert config.load_settings({}).stale_request_seconds >= config.MIN_STALE_REQUEST_SECONDS
    assert generateImage.HuggingFaceImageProvider().timeout == config.HF_TIMEOUT_SECONDS
    assert generateImage.WAIT_MAX == config.RETRY_WAIT_MAX_SECONDS


def test_stale_threshold_below_the_safe_minimum_is_refused():
    minimum = config.MIN_STALE_REQUEST_SECONDS
    assert config.load_settings({"STALE_REQUEST_SECONDS": str(minimum)}).stale_request_seconds == minimum
    with pytest.raises(config.ConfigError, match="STALE_REQUEST_SECONDS"):
        config.load_settings({"STALE_REQUEST_SECONDS": str(minimum - 1)})


def test_slow_but_live_image_call_is_not_marked_abandoned(workdir, tmp_path, clock):
    store = SqliteUsageStore(tmp_path / "slow.db", clock=clock, stale_after_seconds=config.MIN_STALE_REQUEST_SECONDS)
    seen = {}

    class SlowProvider(ScriptedProvider):
        def generate(self, prompt):
            clock.advance(config.HF_TIMEOUT_SECONDS)  # the slowest call the client allows
            seen["swept_during_call"] = store.sweep_stale()  # another request authorising at that moment
            return img()

    (result,) = services.generate_images(prompt_data(1), [1], provider=SlowProvider([img()]), store=store)
    assert result.ok and seen["swept_during_call"] == 0
    (row,) = store.list_requests()
    assert row["outcome"] == usage.SUCCEEDED and row["error_category"] is None


def test_slow_but_live_gemini_call_is_not_marked_abandoned(workdir, tmp_path, clock, monkeypatch):
    store = SqliteUsageStore(tmp_path / "slow.db", clock=clock, stale_after_seconds=config.MIN_STALE_REQUEST_SECONDS)
    seen = []

    class SlowGemini(FakeGemini):
        def generate_content(self, **kwargs):
            clock.advance(config.GEMINI_TIMEOUT_SECONDS)
            seen.append(store.sweep_stale())
            return super().generate_content(**kwargs)

    monkeypatch.setattr(generatePrompts, "get_client", lambda: SlowGemini([reply()]))
    assert services.expand_idea("a cat", store=store).ok
    assert seen == [0]
    assert store.list_requests()[0]["outcome"] == usage.SUCCEEDED


def test_retry_pauses_keep_a_request_alive_but_real_silence_does_not(tmp_path, clock):
    store = SqliteUsageStore(tmp_path / "slow.db", clock=clock, stale_after_seconds=config.MIN_STALE_REQUEST_SECONDS)
    rid = begin_images(store, 1)
    for _ in range(5):  # call, then the longest retry pause, repeated: far more than the threshold in total
        aid = attempt(store, rid)
        clock.advance(config.HF_TIMEOUT_SECONDS)
        store.finish_attempt(aid, usage.ATTEMPT_FAILED, error_category="timeout_or_network")
        clock.advance(config.RETRY_WAIT_MAX_SECONDS)
        assert store.sweep_stale() == 0
    clock.advance(config.MIN_STALE_REQUEST_SECONDS + 1)  # genuinely nothing for longer than the threshold
    assert store.sweep_stale() == 1
    assert store.get_request(rid)["error_category"] == "abandoned"


# ------------------------------------------------------------------ exact money
def test_money_conversion_is_exact_and_strict():
    assert money.usd_to_micros("0.1") == 100_000 and money.usd_to_micros(Decimal("4.275")) == 4_275_000
    assert money.usd_to_micros(2) == 2_000_000 and money.usd_to_micros("-1.25") == -1_250_000
    assert money.micros_to_usd(4_275_000) == Decimal("4.275")
    assert money.format_usd(900_000) == "$0.9000" and money.format_usd(-300_000) == "-$0.3000"
    for bad in ("0.0000001", "abc", "nan", "inf", "", 0.1, True):  # sub-microdollar, junk, and floats
        with pytest.raises(ValueError):
            money.usd_to_micros(bad)


def test_budget_is_enforced_exactly_with_no_tolerance(workdir, store, settings):
    # 0.1 + 0.1 + 0.1 is 0.30000000000000004 in floating point; here it must fit a 0.3 budget exactly
    settings(HF_BUDGET_USD="0.3", HF_BUDGET_SAFETY_MARGIN_PERCENT=0, HF_COST_PER_IMAGE_USD="0.1",
             MAX_IMAGES_PER_DAY=50, MAX_GENERATION_REQUESTS_PER_MINUTE=50)
    for _ in range(3):
        begin_images(store, 1)
    assert store.cost_summary("huggingface")["committed"] == 300_000
    with pytest.raises(LimitExceededError) as exc:
        begin_images(store, 1)
    assert exc.value.code == "budget_exceeded"


def test_one_microdollar_over_the_ceiling_is_refused(store, settings):
    settings(HF_BUDGET_USD="0.299999", HF_BUDGET_SAFETY_MARGIN_PERCENT=0, HF_COST_PER_IMAGE_USD="0.1",
             MAX_IMAGES_PER_DAY=50, MAX_GENERATION_REQUESTS_PER_MINUTE=50)
    begin_images(store, 1)
    begin_images(store, 1)
    with pytest.raises(LimitExceededError):
        begin_images(store, 1)  # needs 300,000 but only 299,999 is allowed


def test_usable_budget_is_rounded_down_never_up(settings):
    s = config.load_settings({"HF_BUDGET_USD": "1", "HF_BUDGET_SAFETY_MARGIN_PERCENT": "33.3333333"})
    assert s.hf_usable_budget_micros == 666_666  # 666,666.6667 floored: the margin is only ever generous
    assert config.load_settings({}).hf_usable_budget_micros == 4_275_000  # margin applied exactly once


def test_money_settings_with_sub_microdollar_precision_are_refused():
    for name in ("HF_BUDGET_USD", "HF_COST_PER_IMAGE_USD"):
        with pytest.raises(config.ConfigError, match=name):
            config.load_settings({name: "0.0000001"})
    for bad in ("100", "-1", "x"):
        with pytest.raises(config.ConfigError, match="HF_BUDGET_SAFETY_MARGIN_PERCENT"):
            config.load_settings({"HF_BUDGET_SAFETY_MARGIN_PERCENT": bad})


def test_money_is_stored_as_integers_and_floats_are_refused(workdir, tmp_path, store):
    services.generate_images(prompt_data(1), [1], provider=ScriptedProvider([make_http_error(503), img()]),
                             store=store)
    rid = begin_images(store, 1)
    aid = attempt(store, rid)
    store.record_actual_cost(aid, 12_345)
    store.add_cost_adjustment("huggingface", -1, "test")
    with closing_connection(store.path) as conn:
        kinds = {row[0] for row in conn.execute(
            "SELECT typeof(estimated_cost_micros) FROM provider_attempts UNION "
            "SELECT typeof(actual_cost_micros) FROM provider_attempts WHERE actual_cost_micros IS NOT NULL UNION "
            "SELECT typeof(reserved_cost_micros) FROM requests WHERE reserved_cost_micros IS NOT NULL UNION "
            "SELECT typeof(estimated_cost_micros) FROM requests WHERE estimated_cost_micros IS NOT NULL UNION "
            "SELECT typeof(amount_micros) FROM cost_adjustments")}
    assert kinds == {"integer"}
    for call in (lambda: store.start_attempt(rid, HF, "m", estimated_cost_micros=0.05),
                 lambda: store.record_actual_cost(aid, 0.02),
                 lambda: store.add_cost_adjustment("huggingface", -0.3, "float"),
                 lambda: store.begin_request(usage.IMAGE_GENERATION, HF, "m", 1, reserved_images=1,
                                             reserved_cost_micros=50_000.0)):
        with pytest.raises(ValueError):
            call()


def closing_connection(path):
    from contextlib import closing
    return closing(sqlite3.connect(path))


def test_version_2_database_with_floating_point_costs_is_converted(tmp_path):
    path = tmp_path / "v2.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE requests (request_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, created_at TEXT NOT NULL,
            finished_at TEXT, operation TEXT NOT NULL, provider TEXT, model TEXT, items_requested INTEGER NOT NULL,
            items_succeeded INTEGER NOT NULL DEFAULT 0, outcome TEXT NOT NULL, estimated_cost_usd REAL,
            actual_cost_usd REAL, error_category TEXT, reserved_images INTEGER NOT NULL DEFAULT 0,
            reserved_cost_usd REAL, last_activity_at TEXT);
        CREATE TABLE provider_attempts (attempt_id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL,
            item_ref INTEGER NOT NULL DEFAULT 0, attempt_number INTEGER NOT NULL, provider TEXT NOT NULL, model TEXT,
            started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL, error_category TEXT, http_status INTEGER,
            input_tokens INTEGER, output_tokens INTEGER, estimated_cost_usd REAL, actual_cost_usd REAL);
        CREATE TABLE cost_adjustments (adjustment_id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL,
            amount_usd REAL NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL);
        INSERT INTO requests VALUES ('r1', 'local', '2026-01-01T00:00:00.000+00:00', '2026-01-01T00:00:01.000+00:00',
            'image_generation', 'huggingface/fal-ai', 'm', 1, 1, 'succeeded', 0.05, 0.02, NULL, 1, 0.05, NULL);
        INSERT INTO provider_attempts VALUES (1, 'r1', 1, 1, 'huggingface/fal-ai', 'm', '2026-01-01T00:00:00.100+00:00',
            NULL, 'succeeded', NULL, NULL, NULL, NULL, 0.05, 0.02);
        INSERT INTO cost_adjustments VALUES (1, 'huggingface', -0.25, 'old note', '2026-01-02T00:00:00.000+00:00');
        PRAGMA user_version = 2;
    """)
    conn.commit()
    conn.close()
    store = SqliteUsageStore(path)
    (request,) = store.list_requests()
    assert (request["estimated_cost_micros"], request["actual_cost_micros"], request["reserved_cost_micros"]) == \
        (50_000, 20_000, 50_000)
    summary = store.cost_summary("huggingface")
    assert (summary["confirmed_actual"], summary["adjustments"]) == (20_000, -250_000)
    assert store.add_cost_adjustment("huggingface", 1, "after the upgrade")  # the new table accepts writes


# ------------------------------------------------------------------ HF retry accounting
def test_reservation_covers_one_attempt_per_image_and_every_retry_needs_more(store, settings):
    settings(HF_BUDGET_USD="1.2", HF_BUDGET_SAFETY_MARGIN_PERCENT=0, HF_COST_PER_IMAGE_USD="0.3",
             MAX_IMAGES_PER_DAY=50, MAX_GENERATION_REQUESTS_PER_MINUTE=50)
    price = 300_000

    def committed():
        return store.cost_summary("huggingface")["committed"]

    rid = begin_images(store, 2)
    assert store.cost_summary("huggingface")["reserved_remaining"] == 600_000  # one attempt per image
    attempt(store, rid, item=1, price=price)
    assert committed() == 600_000  # image 1's first attempt took over image 1's own reservation: not added on top
    attempt(store, rid, item=1, price=price)  # first retry: fresh budget
    assert committed() == 900_000
    attempt(store, rid, item=1, price=price)  # second retry: fresh budget, now at the ceiling
    assert committed() == 1_200_000
    with pytest.raises(LimitExceededError) as exc:
        attempt(store, rid, item=1, price=price)  # a third retry would pass the ceiling
    assert exc.value.code == "budget_exceeded" and committed() == 1_200_000  # refused, nothing recorded
    attempt(store, rid, item=2, price=price)  # image 2's reservation was untouched by image 1's retries
    assert committed() == 1_200_000
    assert [a["attempt_number"] for a in store.list_attempts(rid)] == [1, 2, 3, 1]


def test_no_attempt_is_charged_twice(store):
    rid = begin_images(store, 2)
    first = attempt(store, rid, item=1)
    retry = attempt(store, rid, item=1)
    before = store.cost_summary("huggingface")
    store.finish_attempt(first, usage.ATTEMPT_FAILED, error_category="server_error")
    store.finish_attempt(first, usage.ATTEMPT_SUCCEEDED)  # a repeated report changes nothing
    store.finish_attempt(retry, usage.ATTEMPT_SUCCEEDED)
    assert store.cost_summary("huggingface") == before  # finishing an attempt never adds cost
    assert before["estimated_consumed"] == 2 * 50_000
    store.finish_request(rid, usage.PARTIALLY_SUCCEEDED, 1)
    after = store.cost_summary("huggingface")
    assert after["estimated_consumed"] == 2 * 50_000 and after["reserved_remaining"] == 0
    assert after["committed"] == 100_000  # image 2 never ran: its share is released, the 2 attempts stay once each
    assert store.get_request(rid)["estimated_cost_micros"] == 100_000  # the request total matches the attempts
    with pytest.raises(sqlite3.IntegrityError):  # the attempt number itself is unique per image
        with closing_connection(store.path) as conn:
            conn.execute("INSERT INTO provider_attempts (request_id, item_ref, attempt_number, provider, started_at,"
                         " status) VALUES (?, 1, 1, 'x', 'now', 'started')", (rid,))


def test_interrupted_attempts_stay_charged_when_the_request_ends(store, clock):
    rid = begin_images(store, 1)
    attempt(store, rid)  # never finished: the app died inside the call
    store.finish_request(rid, usage.FAILED, 0, "interrupted")
    (a,) = store.list_attempts(rid)
    assert a["status"] == "started"
    assert store.cost_summary("huggingface")["estimated_consumed"] == 50_000
    other = begin_images(store, 1)
    attempt(store, other)
    clock.advance(config.MIN_STALE_REQUEST_SECONDS + 1000)
    store.sweep_stale()
    assert store.cost_summary("huggingface")["estimated_consumed"] == 100_000  # both interrupted calls are counted


def test_a_prompt_cannot_be_selected_twice_in_one_request(workdir, store):
    provider = ScriptedProvider([img()])
    with pytest.raises(ValidationError) as exc:
        services.generate_images(prompt_data(2), [1, 1], provider=provider, store=store)
    assert exc.value.code == "duplicate_prompt_ids" and provider.calls == 0
    (row,) = store.list_requests()
    assert (row["outcome"], row["error_category"]) == (usage.REJECTED, "duplicate_prompt_ids")


# ------------------------------------------------------------------ visible wait in batch mode
def test_batch_reports_each_wait_before_sleeping(workdir, store, clock, monkeypatch):
    monkeypatch.setattr(generatePrompts, "get_client", lambda: FakeGemini([reply()]))
    events = []
    monkeypatch.setattr(services.time, "sleep", lambda s: (events.append(("sleep", s)), clock.advance(s)))
    services.expand_ideas(["a", "b", "c", "d"], delay=0, store=store,
                          on_wait=lambda idea, seconds: events.append(("wait", idea, seconds)))
    waits = [e for e in events if e[0] == "wait"]
    assert waits and waits[0][1] == "d"  # the fourth idea hits the limit of 3 per minute
    assert events[events.index(waits[0]) + 1] == ("sleep", waits[0][2])  # the message comes first, then the pause


def test_cli_shows_a_waiting_message_instead_of_looking_frozen(workdir, store, clock, monkeypatch, capsys):
    class Q:
        def __init__(self, v):
            self.v = v

        def ask(self):
            return self.v

    ideas = workdir / "ideas.txt"
    ideas.write_text("one\ntwo\nthree\nfour\nfive\n", encoding="utf-8")
    monkeypatch.setattr(usage, "get_store", lambda: store)
    monkeypatch.setattr(generatePrompts, "get_client", lambda: FakeGemini([reply()]))
    monkeypatch.setattr(services.time, "sleep", lambda s: clock.advance(s))
    monkeypatch.setattr(main.questionary, "text", lambda *a, **k: Q(str(ideas)))
    monkeypatch.setattr("builtins.input", lambda *a: "")
    main.process_batch_mode()
    out = capsys.readouterr().out
    assert "[Waiting] Request limit reached. Pausing about" in out and "'four'" in out
    assert "Processing complete: 5 saved, 0 failed." in out
